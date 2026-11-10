"""Measure NGX updater descendants of real ProcessLive sessions on Windows.

Uses existing native runtimes and synthetic frames; never edits app settings.
The supervisor records process identities before/after worker replacement and
application exit. Cleanup only targets observed descendants created by this run,
with creation-time verification. No image-name-wide termination is performed.
"""
import argparse
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Processes:
    class Entry(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD),
                    ('heap', ctypes.c_size_t), ('module', W.DWORD), ('threads', W.DWORD),
                    ('parent', W.DWORD), ('priority', W.LONG), ('flags', W.DWORD),
                    ('name', W.WCHAR * 260)]

    def __init__(self):
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE),
            ('Process32FirstW', [W.HANDLE, ctypes.POINTER(self.Entry)], W.BOOL),
            ('Process32NextW', [W.HANDLE, ctypes.POINTER(self.Entry)], W.BOOL),
            ('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ('GetProcessTimes', [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4, W.BOOL),
            ('QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL),
            ('CloseHandle', [W.HANDLE], W.BOOL),
            ('TerminateProcess', [W.HANDLE, W.UINT], W.BOOL),
            ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        ):
            fn = getattr(self.k, name)
            fn.argtypes, fn.restype = args, result

    def snapshot(self):
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        rows = []
        try:
            entry = self.Entry()
            entry.size = ctypes.sizeof(entry)
            ok = self.k.Process32FirstW(handle, ctypes.byref(entry))
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
            while ok:
                rows.append(dict(pid=int(entry.pid), parent=int(entry.parent), name=entry.name))
                ok = self.k.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.k.CloseHandle(handle)
        return rows

    def identity(self, row):
        result = dict(row)
        handle = self.k.OpenProcess(0x1000, False, row['pid'])
        if not handle:
            result['query_error'] = ctypes.get_last_error()
            return result
        try:
            times = [W.FILETIME() for _ in range(4)]
            if self.k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                result['created'] = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            length = W.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            if self.k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
                result['path'] = path.value
        finally:
            self.k.CloseHandle(handle)
        return result

    def terminate_owned(self, row):
        handle = self.k.OpenProcess(0x1000 | 1 | 0x100000, False, row['pid'])
        if not handle:
            return dict(pid=row['pid'], error=ctypes.get_last_error())
        try:
            times = [W.FILETIME() for _ in range(4)]
            if not self.k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return dict(pid=row['pid'], error='cannot verify creation time')
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            if created != row.get('created'):
                return dict(pid=row['pid'], skipped='identity changed or unavailable')
            ok = self.k.TerminateProcess(handle, 0)
            error = 0 if ok else ctypes.get_last_error()
            self.k.WaitForSingleObject(handle, 2000)
            return dict(pid=row['pid'], terminated=bool(ok), error=error)
        finally:
            self.k.CloseHandle(handle)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def workload(args):
    import numpy as np
    from dlss5tool import dlss_engine
    from dlss5tool import dlss_host_process as host
    host._CREATE_TIMEOUT = host._COMMAND_TIMEOUT = 30.0
    settings = dict(host_backend='v2', host_auto_fallback=False, guidance_mode=0,
                    dlss_runtime=str(args.runtime.resolve()), host_submission='compatibility',
                    host_in_flight=1, host_persistent_buffers=True, host_zero_fast_path=True,
                    style=0, intensity=1.0)
    records, live = [], None

    def record(label, **data):
        row = dict(label=label, wall=time.time(), **data)
        records.append(row)
        save(args.work / 'operations.json', records)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def frame(w, h):
        image = np.empty((h, w, 4), dtype=np.uint8)
        image[..., 0] = np.arange(w, dtype=np.uint16)[None, :] % 256
        image[..., 1] = np.arange(h, dtype=np.uint16)[:, None] % 256
        image[..., 2] = 96
        image[..., 3] = 255
        for index in range(3):
            out = live.process(image, reset=index == 0)
            if out is None or out.shape != image.shape or out.dtype != image.dtype:
                raise RuntimeError('Unexpected DLSS frame output')
        record('processed', width=w, height=h, pid=live._session._process.pid,
               backend=live.backend, adapter=live.adapter_info,
               output_sha256=hashlib.sha256(memoryview(out)).hexdigest())

    def preserve(label):
        if live and Path(live._session.log_path).is_file():
            try:
                (args.work / (label + '-host.log')).write_bytes(Path(live._session.log_path).read_bytes())
            except PermissionError:
                # The native CRT can hold this log exclusively while alive.
                # Measurement does not depend on reading an active host log.
                record('host-log-unavailable', stage=label, reason='native file sharing lock')

    try:
        record('starting', app_pid=os.getpid(), settings=settings,
               host_dll=dlss_engine.HOST_DLL_V2)
        live = host.ProcessLive(640, 360, settings)
        record('initialized', pid=live._session._process.pid)
        frame(640, 360)
        time.sleep(2)
        for index in range(args.cycles):
            preserve('before-resize-%d' % index)
            w, h = (800, 448) if index % 2 == 0 else (640, 360)
            old_pid = live._session._process.pid
            live.resize(w, h, settings=settings)
            record('resized', old_pid=old_pid, pid=live._session._process.pid, width=w, height=h)
            frame(w, h)
            time.sleep(1)
        # A style-only edit should reuse the session rather than initialize NGX.
        old_pid = live._session._process.pid
        live.update({'style': 1})
        record('style-update', old_pid=old_pid, pid=live._session._process.pid)
        preserve('before-close')
        live.close()
        live = None
        record('closed')
        time.sleep(3)
        live = host.ProcessLive(640, 360, settings)
        record('reopened', pid=live._session._process.pid)
        frame(640, 360)
        preserve('reopened')
        live.close()
        live = None
        record('final-close')
        time.sleep(10)
        record('app-exiting')
    except BaseException:
        record('error', traceback=traceback.format_exc())
        preserve('error')
        raise
    finally:
        if live:
            live.close()


def supervise(args):
    processes = Processes()
    baseline_rows = processes.snapshot()
    is_updater = lambda row: 'ngx' in row['name'].lower() and 'updat' in row['name'].lower()
    baseline = [processes.identity(r) for r in baseline_rows if is_updater(r)]
    baseline_identities = {r['pid']: processes.identity(r).get('created') for r in baseline_rows}
    seen, events, samples = {}, [], []
    known = {}
    started = time.monotonic()
    with (args.work / 'console.log').open('w', encoding='utf-8') as log:
        executable = getattr(sys, '_base_executable', sys.executable)
        environment = os.environ.copy()
        if sys.prefix != sys.base_prefix:
            environment['__PYVENV_LAUNCHER__'] = sys.executable
        child = subprocess.Popen([executable, '-B', str(Path(__file__).resolve()),
                                  '--work', str(args.work.resolve()), '--runtime', str(args.runtime.resolve()),
                                  '--cycles', str(args.cycles), '--workload'], cwd=ROOT,
                                 env=environment,
                                 stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        child_row = next(r for r in processes.snapshot() if r['pid'] == child.pid)
        known[child.pid] = processes.identity(child_row).get('created')
        if known[child.pid] is None:
            child.terminate()
            child.wait(timeout=10)
            raise RuntimeError('Cannot verify workload process identity')
        child_exit = None
        last_set = None
        try:
            while True:
                now = time.monotonic()
                rows = processes.snapshot()
                identities = {}

                def identify(row):
                    if row['pid'] not in identities:
                        identities[row['pid']] = processes.identity(row)
                    return identities[row['pid']]

                # A reused PID is a different process, including baseline PIDs.
                for row in rows:
                    if row['pid'] in known and identify(row).get('created') != known[row['pid']]:
                        del known[row['pid']]
                # Resolve multiple descendant generations present in one snapshot.
                for _ in range(4):
                    for row in rows:
                        if row['pid'] in known or row['parent'] not in known:
                            continue
                        created = identify(row).get('created')
                        if (created is not None and created >= known[row['parent']]
                                and created != baseline_identities.get(row['pid'])):
                            known[row['pid']] = created
                updaters = [r for r in rows if is_updater(r)]
                for row in updaters:
                    detail = identify(row)
                    key = (row['pid'], detail.get('created'))
                    if key not in seen:
                        detail.update(first_seen=round(now-started, 3),
                                      owned=detail.get('created') is not None and known.get(row['pid']) == detail['created'],
                                      existed_before=detail.get('created') == baseline_identities.get(row['pid']))
                        seen[key] = detail
                        events.append(dict(event='observed', **detail))
                    seen[key]['last_seen'] = round(now-started, 3)
                current = tuple(sorted(r['pid'] for r in updaters))
                if current != last_set:
                    sample = dict(seconds=round(now-started, 3), total=len(updaters),
                                  owned=sum(r['pid'] in known for r in updaters), pids=list(current))
                    samples.append(sample)
                    print(json.dumps(sample), flush=True)
                    save(args.work / 'samples.json', samples)
                    save(args.work / 'processes.json', list(seen.values()))
                    last_set = current
                if child.poll() is not None and child_exit is None:
                    child_exit = now
                    print('Workload exited: %s; observing for 20 seconds' % child.returncode, flush=True)
                if child_exit is not None and now-child_exit >= 20:
                    break
                if now-started > args.timeout or len(updaters) > 100:
                    raise RuntimeError('Probe timeout or updater count cap exceeded')
                time.sleep(.1)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            remaining = [processes.identity(r) for r in processes.snapshot() if is_updater(r)]
            owned_remaining = [r for r in remaining
                if seen.get((r['pid'], r.get('created')), {}).get('owned')]
            cleanup = [processes.terminate_owned(r) for r in owned_remaining]
            result = dict(baseline=baseline, workload_pid=child.pid, workload_exit=child.returncode,
                          cycles=args.cycles, poll_seconds=.1, app_exit_observation_seconds=20,
                          timeout_seconds=args.timeout,
                          observed=list(seen.values()), samples=samples,
                          peak_total=max([len(baseline)] + [s['total'] for s in samples]),
                          peak_owned=max([0] + [s['owned'] for s in samples]),
                          remaining_before_cleanup=remaining, cleanup=cleanup,
                          final=[processes.identity(r) for r in processes.snapshot() if is_updater(r)])
            save(args.work / 'result.json', result)
            print(json.dumps({k: v for k, v in result.items() if k not in ('observed', 'samples')}, ensure_ascii=False), flush=True)
    if child.returncode:
        raise RuntimeError('Workload failed; see console.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, default=ROOT/'runtime/nvngx_dlssnr.dll')
    parser.add_argument('--cycles', type=int, default=5)
    parser.add_argument('--timeout', type=float, default=None,
                        help='Supervisor limit in seconds (default scales with cycles)')
    parser.add_argument('--workload', action='store_true')
    args = parser.parse_args()
    if not 0 <= args.cycles <= 100:
        parser.error('--cycles must be between 0 and 100')
    if args.timeout is None:
        args.timeout = max(180, 60 + 15 * args.cycles)
    if not 30 <= args.timeout <= 1800:
        parser.error('--timeout must be between 30 and 1800 seconds')
    if not args.work.is_dir():
        parser.error('--work must be an existing registered task directory')
    if args.workload:
        workload(args)
    else:
        supervise(args)


if __name__ == '__main__':
    main()
