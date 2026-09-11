"""Small standalone Windows updater; no GUI/NumPy/Torch imports."""
from contextlib import contextmanager
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from dlss5tool import delta_update as delta


def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    return kernel


@contextmanager
def installation_lock(root, timeout_ms=0):
    """One GUI per installation; held until process teardown, not just window close."""
    if os.name != 'nt':
        raise delta.DeltaError('Automatic installation is supported on Windows only')
    kernel = _kernel()
    key = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, 'Global\\DLSS5Tool-' + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, timeout_ms)
        if result not in (0, 0x80):  # WAIT_OBJECT_0 / abandoned process mutex
            raise delta.DeltaError('Another DLSS5Tool instance or updater is using this installation')
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def notify(message, error=False):
    if os.name == 'nt':
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user.MessageBoxW(None, str(message), 'DLSS5Tool Update / 更新', 0x10 if error else 0x40)


def wait_for_parent(pid, timeout_ms=120000):
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise delta.DeltaError('Invalid parent process')
    kernel = _kernel()
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        # A parent that has already exited is OK; access denied is not.
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise delta.DeltaError('Application did not exit within 120 seconds; no files replaced')
    finally:
        kernel.CloseHandle(handle)


def launch(root, pid, language='zh_CN'):
    directory = delta.transaction_path(root)
    if (delta.status(root) or {}).get('phase') != 'ready':
        raise delta.DeltaError('Update has not been prepared')
    helper = delta.safe_path(directory, delta.HELPER)
    manifest = delta.validate_manifest(delta.read_json(directory / 'manifest.json'))
    if delta.file_record(helper) != manifest['before'][delta.HELPER]:
        raise delta.DeltaError('Update helper failed integrity check')
    return subprocess.Popen(
        [str(helper), '--root', str(Path(root).absolute()), '--parent-pid', str(pid),
         '--language', language], cwd=str(directory),
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--language', choices=('zh_CN', 'en_US'), default='zh_CN')
    parser.add_argument('--quiet', action='store_true', help='Use exit code and state.json without message boxes (verification tools)')
    args = parser.parse_args(argv)
    root = args.root.absolute()
    english = args.language == 'en_US'
    try:
        directory = delta.transaction_path(root)
        if not directory.is_dir() or not (root / 'DLSS5Tool.exe').is_file():
            raise delta.DeltaError('Expected an existing portable installation and update transaction')
        with delta.transaction_lock(root):
            if not args.recover:
                prepared_state = delta.status(root) or {}
                if prepared_state.get('phase') != 'ready':
                    raise delta.DeltaError('Update is not ready')
                delta.set_state(root, 'waiting', target=prepared_state.get('target'))
                try:
                    wait_for_parent(args.parent_pid)
                except Exception:
                    delta.set_state(root, 'ready', target=prepared_state.get('target'))
                    raise
            try:
                with installation_lock(root, timeout_ms=10000):
                    delta.apply_transaction(root, recover=args.recover)
            except Exception:
                if (delta.status(root) or {}).get('phase') == 'waiting' and not args.recover:
                    delta.set_state(root, 'ready', target=prepared_state.get('target'))
                raise
        message = ('Recovery finished. Start the old application manually.' if args.recover else
                   'Update finished. Start DLSS5Tool.exe manually. The rollback backup is retained.') if english else (
                   '回滚已完成，请手动启动旧版程序。' if args.recover else
                   '更新完成，请手动启动 DLSS5Tool.exe。旧文件备份已保留。')
        if not args.quiet:
            notify(message)
        return 0
    except Exception as ex:
        message = (f'Update stopped: {ex}\nKeep the update folder. If recovery is needed, close the app and run:\n'
                   if english else f'更新已停止：{ex}\n请保留更新目录。如需恢复，关闭程序后运行：\n')
        message += f'"{root / delta.TRANSACTION / delta.HELPER}" --root "{root}" --recover'
        if not args.quiet:
            notify(message, error=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
