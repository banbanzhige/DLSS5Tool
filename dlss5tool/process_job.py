"""Windows kill-on-close lifetime container for an already owned Popen child."""
import ctypes as C
import os


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
    def __init__(self, process):
        if os.name!='nt':
            raise OSError('Windows job required')
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
            if not self._kernel.AssignProcessToJobObject(self._handle,int(process._handle)):
                raise C.WinError(C.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._handle:
            if not self._kernel.CloseHandle(self._handle):
                raise C.WinError(C.get_last_error())
            self._handle=None


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
