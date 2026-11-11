"""Windows kill-on-close lifetime container for an already owned Popen child."""
import ctypes as C
import os
import threading
from multiprocessing.connection import wait


class _Basic(C.Structure):
    _fields_=[('process_time',C.c_int64),('job_time',C.c_int64),('flags',C.c_uint32),
              ('min_working',C.c_size_t),('max_working',C.c_size_t),('active',C.c_uint32),
              ('affinity',C.c_size_t),('priority',C.c_uint32),('scheduling',C.c_uint32)]


class _Extended(C.Structure):
    _fields_=[('basic',_Basic),('io',C.c_uint64*6),('process_memory',C.c_size_t),
              ('job_memory',C.c_size_t),('peak_process',C.c_size_t),('peak_job',C.c_size_t)]


class KillOnCloseJob:
    """The child cannot inherit this handle; owner death closes it in the kernel.

    Callers keep the Popen process handle and must gate child native work until
    assignment succeeds. This class never opens a process by name or PID.
    """
    def __init__(self, process, *, nvidia_compat=False):
        self._initialize(int(process._handle), nvidia_compat=nvidia_compat)

    @classmethod
    def from_handle(cls, handle, *, nvidia_compat=False):
        """Borrow an owned process handle, including a Windows Process.sentinel.

        The caller retains the process handle until its exit watcher has joined.
        No process is opened by PID and this job handle is never inheritable.
        """
        job = cls.__new__(cls)
        job._initialize(int(handle), nvidia_compat=nvidia_compat)
        return job

    def _initialize(self, process_handle, *, nvidia_compat=False):
        if os.name!='nt':
            raise OSError('Windows job required')
        self._lock = threading.Lock()
        self.token_compatibility = None
        self._kernel=C.WinDLL('kernel32',use_last_error=True)
        for name,args,result in (
            ('CreateJobObjectW',[C.c_void_p,C.c_wchar_p],C.c_void_p),
            ('SetInformationJobObject',[C.c_void_p,C.c_int,C.c_void_p,C.c_uint32],C.c_int),
            ('AssignProcessToJobObject',[C.c_void_p,C.c_void_p],C.c_int),
            ('CloseHandle',[C.c_void_p],C.c_int),
        ):
            function=getattr(self._kernel,name); function.argtypes=args; function.restype=result
        self._handle=self._kernel.CreateJobObjectW(None,None)
        if not self._handle: raise C.WinError(C.get_last_error())
        try:
            limits=_Extended(); limits.basic.flags=0x2000
            if not self._kernel.SetInformationJobObject(self._handle,9,C.byref(limits),C.sizeof(limits)):
                raise C.WinError(C.get_last_error())
            if not self._kernel.AssignProcessToJobObject(self._handle,process_handle):
                raise C.WinError(C.get_last_error())
            if nvidia_compat:
                from dlss5tool.nvidia_token_compat import prepare_worker_token
                from dlss5tool.memory_budget import record_memory_event
                self.token_compatibility = prepare_worker_token(process_handle)
                record_memory_event('nvidia_token_compatibility', **self.token_compatibility)
        except BaseException:
            self.close()
            raise

    def close(self):
        # The owner and its worker-exit watcher can retire the same job at once.
        with self._lock:
            if self._handle:
                if not self._kernel.CloseHandle(self._handle):
                    raise C.WinError(C.get_last_error())
                self._handle=None


class ProcessTreeJob:
    """Parent-owned lifetime job watching a borrowed process handle.

    The process must wait at a start gate until construction succeeds. Call
    close() before releasing its process handle, including after natural exit.
    """
    def __init__(self, process, *, nvidia_compat=False):
        handle = (process.sentinel if hasattr(process, 'sentinel')
                  else int(process._handle))
        self._job = KillOnCloseJob.from_handle(handle, nvidia_compat=nvidia_compat)
        self.token_compatibility = self._job.token_compatibility
        self._pid = process.pid
        try:
            self._watcher = threading.Thread(
                target=self._watch_exit, args=(handle,),
                name='ngx-job-%s' % process.pid, daemon=True,
            )
            self._watcher.start()
        except BaseException:
            self._job.close()
            raise

    def _watch_exit(self, handle):
        try:
            wait([handle])
        finally:
            try:
                self._job.close()
            except OSError as error:
                # The session retains this owner for explicit close/retry.
                from dlss5tool.memory_budget import record_memory_event
                record_memory_event('worker_job_cleanup_failed',
                                    source_pid=self._pid, error=str(error))

    def close(self, timeout=2.0):
        self._job.close()
        self._watcher.join(timeout=timeout)
        if self._watcher.is_alive():
            raise OSError('NGX worker process tree is still retiring; retry close')


def create_process_job(process, *, nvidia_compat=False):
    """Arm a Windows worker before its native initialization is released."""
    return ProcessTreeJob(process, nvidia_compat=nvidia_compat) if os.name == 'nt' else None


def process_record(pid):
    """Read one exact PID from Toolhelp, including native teardown remnants."""
    from ctypes import wintypes as W
    from dlss5tool.cuda_session import bind
    class Entry(C.Structure):
        _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                    ('heap', C.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('name', W.WCHAR*260)]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    snapshot = bind(kernel, 'CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE)(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    try:
        first = bind(kernel, 'Process32FirstW', [W.HANDLE, C.POINTER(Entry)])
        next_entry = bind(kernel, 'Process32NextW', [W.HANDLE, C.POINTER(Entry)])
        entry = Entry(); entry.size = C.sizeof(entry)
        valid = first(snapshot, C.byref(entry))
        while valid:
            if entry.pid == pid:
                return dict(present=True, pid=pid, threads=entry.threads, parent=entry.parent, name=entry.name)
            valid = next_entry(snapshot, C.byref(entry))
        return dict(present=False, pid=pid)
    finally:
        bind(kernel, 'CloseHandle', [W.HANDLE])(snapshot)


def confirm_retired(process, *, timeout=2):
    """Close our completed Popen handle, then bound the actual OS disappearance.

    A signaled process handle alone is insufficient during native driver exit.
    Never operates on another process or attempts a driver reset.
    """
    import time
    if process.poll() is None:
        raise RuntimeError('Owned process is still running')
    handle = getattr(process, '_handle', None)
    if handle is not None:
        handle.Close()
        process._handle = None
    deadline = time.monotonic()+timeout
    while True:
        record = process_record(process.pid)
        if not record['present'] or record.get('parent') != os.getpid():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Native worker PID {process.pid} still has an OS process record')
        time.sleep(.05)


def private_commit(process):
    """Read memory from our retained Popen handle, with no PID/name lookup."""
    class Counters(C.Structure):
        _fields_ = [('size', C.c_uint32), ('faults', C.c_uint32)] + [
            (name, C.c_size_t) for name in ('peak_working', 'working', 'peak_paged',
                'paged', 'peak_nonpaged', 'nonpaged', 'commit', 'peak_commit', 'private')]
    handle = getattr(process, '_handle', None)
    if handle is None:
        return None
    function = C.WinDLL('psapi').GetProcessMemoryInfo
    function.argtypes, function.restype = [C.c_void_p, C.POINTER(Counters), C.c_uint32], C.c_int
    counters = Counters(); counters.size = C.sizeof(counters)
    return counters.private if function(int(handle), C.byref(counters), counters.size) else None
