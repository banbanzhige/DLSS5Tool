"""Bounded, one-shot adapter discovery with no GPU initialization in callers.

Subprocess rather than multiprocessing.Process also works inside daemon NGX
workers. A private inherited pipe carries bounded JSON; native stdout is never
used as a protocol. Failed retirement retains the Popen owner for close/retry.
"""
import atexit
import copy
import json
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

WORKER_FLAG = '--adapter-probe'
PROTOCOL = 1
MAX_MESSAGE = 128*1024
PROBE_TIMEOUT = 20.0
_ROOT = Path(__file__).resolve().parents[1]
_ID = re.compile(r'dxgi:[0-9A-F]{4}:[0-9A-F]{4}:[0-9A-F]{8}:[0-9A-F]{8}:[0-9A-F]{16}(?::P[0-9A-F]{9}|:C[0-9]{1,2})?')
_generation_lock = threading.Lock()
_generation = 0


class AdapterProbeError(RuntimeError):
    pass


def _changed():
    global _generation
    with _generation_lock:
        _generation += 1


def cache_generation():
    with _generation_lock:
        return _generation


def _validate_inventory(value):
    if not isinstance(value, dict) or not isinstance(value.get('adapters'), list) or len(value['adapters']) > 64:
        raise AdapterProbeError('显卡探测返回了无效的列表')
    seen = set()
    integer_fields = dict(preference_index=(0,63), vendor_id=(0,65535), device_id=(0,65535),
        subsys_id=(0,2**32-1), revision=(0,2**32-1), dedicated_video_memory=(0,2**64-1),
        luid_high=(-2**31,2**31-1), luid_low=(0,2**32-1))
    for record in value['adapters']:
        if not isinstance(record, dict):
            raise AdapterProbeError('显卡探测返回了无效的设备')
        identity = record.get('id')
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in seen:
            raise AdapterProbeError('显卡探测返回了无效或重复的设备身份')
        seen.add(identity)
        if not isinstance(record.get('name'), str) or not 1 <= len(record['name']) <= 128:
            raise AdapterProbeError('显卡探测返回了无效的名称')
        for key, (low,high) in integer_fields.items():
            if type(record.get(key)) is not int or not low <= record[key] <= high:
                raise AdapterProbeError('显卡探测返回了无效的设备参数')
        if record.get('d3d12_feature_level') not in ('11_0','12_0'):
            raise AdapterProbeError('显卡探测返回了无效的能力信息')
        if record.get('cuda_index') is not None and (type(record['cuda_index']) is not int or not 0 <= record['cuda_index'] < 64):
            raise AdapterProbeError('显卡探测返回了无效的 CUDA 设备')
        if not isinstance(record.get('pci_bus_id'), str) or len(record['pci_bus_id']) > 32:
            raise AdapterProbeError('显卡探测返回了无效的 PCI 身份')
    if type(value.get('cuda_identity_available')) is not bool or not isinstance(value.get('warnings'), list):
        raise AdapterProbeError('显卡探测返回了无效的查询状态')
    if len(value['warnings']) > 8 or any(not isinstance(v,str) or len(v)>1000 for v in value['warnings']):
        raise AdapterProbeError('显卡探测返回了无效的查询状态')
    return value


def _command(handle, host_path, nonce):
    # The Windows venv redirector creates an extra interpreter process. Query
    # code needs only stdlib, so use the same-version base interpreter directly
    # with site initialization disabled and a trusted absolute source path.
    prefix = ([sys.executable] if getattr(sys, 'frozen', False) else
              [getattr(sys, '_base_executable', None) or sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve())])
    return [*prefix, WORKER_FLAG, str(handle), host_path, nonce]


def _spawn(sender, host_path, nonce):
    handle = sender.fileno()
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  cwd=str(_ROOT) if not getattr(sys, 'frozen', False) else str(Path(sys.executable).parent),
                  close_fds=True)
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [handle]}
        kwargs.update(startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
        previous = os.get_handle_inheritable(handle)
        try:
            os.set_handle_inheritable(handle, True)
            return subprocess.Popen(_command(handle, host_path, nonce), **kwargs)
        finally:
            try:
                os.set_handle_inheritable(handle, previous)
            except OSError:
                pass  # The caller closes this handle immediately after spawn.
    return subprocess.Popen(_command(handle, host_path, nonce), pass_fds=(handle,), **kwargs)


def _retire(process):
    """Operate only on the Popen object created here, retaining it on failure."""
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    if process.poll() is None:
        raise AdapterProbeError('显卡探测进程尚未退出，请重试关闭')
    process.wait(timeout=0)


def _make_job(process):
    if os.name == 'nt':
        from dlss5tool.process_job import KillOnCloseJob
        return KillOnCloseJob(process)
    return None


class AdapterProbe:
    def __init__(self, *, timeout=PROBE_TIMEOUT):
        if not isinstance(timeout, (int,float)) or not .01 <= timeout <= 60:
            raise ValueError('Finite adapter probe timeout required')
        self.timeout = timeout
        self._condition = threading.Condition()
        self._busy = False
        self._closed = False
        self._stop = threading.Event()
        self._process = None
        self._job = None
        self._connections = []
        self._cache = self._key = None
        self._serial = 0
        self._last = None
        self._epoch = 0

    def invalidate(self):
        with self._condition:
            self._cache = None
            self._epoch += 1
            _changed()
            if self._busy:
                self._stop.set()
            self._condition.notify_all()

    def _exchange(self, host_path, cancel):
        receiver, sender = multiprocessing.Pipe(duplex=True)
        self._connections = [receiver,sender]
        nonce = uuid.uuid4().hex
        try:
            self._process = _spawn(sender, host_path, nonce)
            self._job = _make_job(self._process)
            sender.close()
            self._connections.remove(sender)
            if self._stop.is_set():
                raise AdapterProbeError('显卡探测已取消')
            # The child waits for this message before loading native DLLs. Its
            # kill-on-close job is already armed, including in daemon parents.
            receiver.send_bytes(json.dumps(dict(version=PROTOCOL,nonce=nonce,operation='start')).encode('utf-8'))
            deadline = time.monotonic()+self.timeout
            while True:
                if self._stop.is_set() or cancel is not None and cancel.is_set():
                    raise AdapterProbeError('显卡探测已取消')
                if time.monotonic() >= deadline:
                    raise AdapterProbeError('显卡探测超时，请刷新后重试')
                if receiver.poll(.05):
                    data = receiver.recv_bytes(MAX_MESSAGE)
                    reply = json.loads(data.decode('utf-8'))
                    if (not isinstance(reply,dict) or type(reply.get('version')) is not int or
                            reply['version'] != PROTOCOL or reply.get('nonce') != nonce or
                            reply.get('pid') != self._process.pid or type(reply.get('ok')) is not bool):
                        raise AdapterProbeError('显卡探测返回了无效的协议消息')
                    if not reply['ok']:
                        error = reply.get('error')
                        raise AdapterProbeError(error[:2000] if isinstance(error,str) else '显卡探测失败')
                    inventory = _validate_inventory(reply.get('inventory'))
                    try:
                        code = self._process.wait(timeout=1)
                    except subprocess.TimeoutExpired as error:
                        raise AdapterProbeError('显卡探测返回结果后未正常退出') from error
                    if code != 0:
                        raise AdapterProbeError('显卡探测进程异常退出')
                    return dict(inventory, probe_pid=reply['pid'])
                if self._process.poll() is not None:
                    raise AdapterProbeError('显卡探测进程退出，未返回完整结果')
        except (OSError, EOFError, ValueError, UnicodeError) as error:
            raise AdapterProbeError('显卡探测通信失败：'+str(error)) from error

    def _retire_owned(self):
        errors = []
        for connection in tuple(self._connections):
            try:
                connection.close()
                self._connections.remove(connection)
            except Exception as error:
                errors.append(str(error))
        try:
            _retire(self._process)
            self._process = None
        except Exception as error:
            errors.append(str(error))
        if self._job is not None:
            try:
                self._job.close()
                self._job = None
            except Exception as error:
                errors.append(str(error))
        if self._process is not None and self._process.poll() is not None:
            try:
                self._process.wait(timeout=0)
                self._process = None
            except Exception as error:
                errors.append(str(error))
        if self._process is not None or self._job is not None or self._connections:
            raise AdapterProbeError('; '.join(errors) or '探测资源尚未退出，请重试关闭')

    def get(self, host_path, *, refresh=False, cancel=None):
        host_path = os.path.abspath(os.fspath(host_path))
        try:
            status = os.stat(host_path)
            key = (os.path.normcase(host_path), status.st_size, status.st_mtime_ns)
        except OSError as error:
            raise AdapterProbeError('显卡探测宿主不可用：'+str(error)) from error
        with self._condition:
            if self._closed:
                raise AdapterProbeError('显卡探测已关闭')
            if cancel is not None and cancel.is_set():
                raise AdapterProbeError('显卡探测已取消')
            if refresh and not self._busy:
                self._cache = None
                self._epoch += 1
                _changed()
            joined = self._serial if self._busy else None
            wait_deadline = time.monotonic()+self.timeout+5
            while self._busy:
                if self._closed or cancel is not None and cancel.is_set():
                    raise AdapterProbeError('显卡探测已取消')
                if time.monotonic() >= wait_deadline:
                    raise AdapterProbeError('等待显卡探测退出超时')
                self._condition.wait(.05)
            if self._closed:
                raise AdapterProbeError('显卡探测已关闭')
            if joined is not None and self._last and self._last[0] == joined and self._last[1] == key:
                if self._last[3]:
                    raise AdapterProbeError(self._last[3])
                return copy.deepcopy(self._last[2])
            if self._cache is not None and self._key == key:
                return copy.deepcopy(self._cache)
            self._busy = True
            self._serial += 1
            serial, epoch = self._serial, self._epoch
            self._stop.clear()
        value, failure, interrupted = None, None, None
        try:
            # A previous retirement failure remains owned and must be retried
            # before another child can be allocated.
            self._retire_owned()
            value = self._exchange(host_path, cancel)
        except BaseException as error:
            failure = str(error) or type(error).__name__
            if not isinstance(error, Exception):
                interrupted = error
        finally:
            try:
                self._retire_owned()
            except Exception as error:
                failure = (failure+'; ' if failure else '')+'探测清理失败：'+str(error)
            with self._condition:
                if self._closed or self._stop.is_set() or epoch != self._epoch:
                    failure = failure or '显卡探测结果已失效，请刷新后重试'
                if failure is None:
                    self._cache, self._key = value, key
                    _changed()
                self._last = (serial, key, value, failure)
                self._busy = False
                self._condition.notify_all()
        if failure:
            if interrupted is not None:
                raise interrupted
            raise AdapterProbeError(failure)
        return copy.deepcopy(value)

    def close(self, timeout=3):
        deadline = time.monotonic()+timeout
        with self._condition:
            self._closed = True
            self._stop.set()
            self._cache = None
            self._epoch += 1
            _changed()
            while self._busy:
                left = deadline-time.monotonic()
                if left <= 0:
                    raise AdapterProbeError('显卡探测尚在退出，请重试关闭')
                self._condition.wait(min(left,.05))
            # Serialize retrying close calls with get()'s retained owner.
            self._retire_owned()


_default_lock = threading.Lock()
_default = None


def get_inventory(host_path, *, refresh=False, cancel=None):
    global _default
    with _default_lock:
        if _default is None:
            _default = AdapterProbe()
        probe = _default
    return probe.get(host_path, refresh=refresh, cancel=cancel)


def invalidate():
    with _default_lock:
        probe = _default
    if probe is not None:
        probe.invalidate()


def cached_inventory():
    """Read the last inventory without starting a probe or loading any DLL."""
    with _default_lock:
        probe = _default
    if probe is None:
        return None
    with probe._condition:
        return copy.deepcopy(probe._cache)


def close():
    global _default
    with _default_lock:
        probe = _default
    if probe is not None:
        probe.close()
        with _default_lock:
            if _default is probe:
                _default = None


def _at_exit():
    try:
        close()
    except Exception:
        pass  # The explicit GUI close reports failures and retains this owner.


atexit.register(_at_exit)


def worker_main(handle, host_path, nonce):
    if os.name == 'nt':
        from multiprocessing.connection import PipeConnection as Connection
    else:
        from multiprocessing.connection import Connection
    connection = Connection(handle, readable=True, writable=True)
    try:
        try:
            if not connection.poll(PROBE_TIMEOUT):
                raise AdapterProbeError('显卡探测启动确认超时')
            message = json.loads(connection.recv_bytes(MAX_MESSAGE).decode('utf-8'))
            if message != dict(version=PROTOCOL,nonce=nonce,operation='start'):
                raise AdapterProbeError('显卡探测启动确认无效')
            if not getattr(sys, 'frozen', False):
                sys.path.insert(0, str(_ROOT))
            from dlss5tool.adapter_inventory import collect
            inventory = _validate_inventory(collect(host_path))
            reply = dict(version=PROTOCOL, nonce=nonce, pid=os.getpid(), ok=True, inventory=inventory)
        except Exception as error:
            reply = dict(version=PROTOCOL, nonce=nonce, pid=os.getpid(), ok=False,
                         error=(type(error).__name__+': '+str(error))[:2000])
        data = json.dumps(reply, ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_MESSAGE:
            raise AdapterProbeError('Adapter inventory exceeded its protocol bound')
        connection.send_bytes(data)
    finally:
        connection.close()


def dispatch(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if WORKER_FLAG not in args:
        return False
    if len(args) != 4 or args[0] != WORKER_FLAG or not args[1].isdigit() or not re.fullmatch('[0-9a-f]{32}',args[3]):
        raise SystemExit(2)
    worker_main(int(args[1]), args[2], args[3])
    return True


if __name__ == '__main__':
    raise SystemExit(0 if dispatch() else 2)
