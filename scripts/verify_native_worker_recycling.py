"""Bounded NR/VSR worker retirement and feature rebuild measurements on Windows.

Uses production owners and existing DLLs with synthetic frames. The supervisor
arms a kill-on-close Windows Job before releasing the workload's stdin gate.
CPU private commit and system commit are measured separately; optional WDDM PDH
GPU counters are read outside the workload. No nvidia-smi polling, DLL injection,
working-set trimming, real memory exhaustion, or external runtime downloads.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MIB, GIB = 1024**2, 1024**3


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


class Performance:
    class Info(C.Structure):
        _fields_ = [('size', C.c_uint32)] + [(name, C.c_size_t) for name in (
            'commit', 'limit', 'peak', 'physical', 'available', 'cache',
            'kernel', 'paged', 'nonpaged', 'page_size')] + [
            (name, C.c_uint32) for name in ('handles', 'processes', 'threads')]

    def __init__(self):
        self.api = C.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
        self.api.argtypes = [C.POINTER(self.Info), C.c_uint32]
        self.api.restype = C.c_int

    def read(self):
        value = self.Info(); value.size = C.sizeof(value)
        if not self.api(C.byref(value), value.size):
            raise C.WinError(C.get_last_error())
        return dict(commit_bytes=value.commit*value.page_size,
                    commit_limit_bytes=value.limit*value.page_size,
                    free_physical_bytes=value.available*value.page_size,
                    free_commit_bytes=(value.limit-value.commit)*value.page_size,
                    kernel_paged_bytes=value.paged*value.page_size,
                    kernel_nonpaged_bytes=value.nonpaged*value.page_size)


class GpuCounters:
    class Number(C.Union):
        _fields_ = [('large', C.c_int64), ('double', C.c_double), ('long', C.c_int32)]

    class Value(C.Structure):
        pass

    class Item(C.Structure):
        pass

    def __init__(self):
        self.api = C.WinDLL('pdh', use_last_error=True)
        for name, args in (
            ('PdhOpenQueryW', [C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]),
            ('PdhAddEnglishCounterW', [C.c_void_p, C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]),
            ('PdhCollectQueryData', [C.c_void_p]),
            ('PdhGetFormattedCounterArrayW', [C.c_void_p, C.c_uint32,
                C.POINTER(C.c_uint32), C.POINTER(C.c_uint32), C.c_void_p]),
            ('PdhCloseQuery', [C.c_void_p]),
        ):
            fn = getattr(self.api, name); fn.argtypes = args; fn.restype = C.c_uint32
        self.query = C.c_void_p()
        self._check(self.api.PdhOpenQueryW(None, 0, C.byref(self.query)))
        self.counters = {}
        try:
            for key, name in (('dedicated_bytes', 'Dedicated Usage'), ('shared_bytes', 'Shared Usage'),
                              ('gpu_committed_bytes', 'Total Committed')):
                counter = C.c_void_p()
                self._check(self.api.PdhAddEnglishCounterW(self.query,
                    '\\GPU Process Memory(*)\\'+name, 0, C.byref(counter)))
                self.counters[key] = counter
        except BaseException:
            self.close(); raise

    @staticmethod
    def _check(code):
        if code:
            raise OSError(f'PDH status 0x{code:08x}')

    def read(self, identities):
        self._check(self.api.PdhCollectQueryData(self.query))
        result = {}
        for key, counter in self.counters.items():
            size, count = C.c_uint32(), C.c_uint32()
            code = self.api.PdhGetFormattedCounterArrayW(counter, 0x400,
                C.byref(size), C.byref(count), None)
            if code != 0x800007d2:  # PDH_MORE_DATA
                self._check(code)
            if not size.value:
                continue
            buffer = C.create_string_buffer(size.value)
            self._check(self.api.PdhGetFormattedCounterArrayW(counter, 0x400,
                C.byref(size), C.byref(count), buffer))
            items = C.cast(buffer, C.POINTER(self.Item))
            for index in range(count.value):
                item = items[index]; name = item.name or ''
                match = re.match(r'pid_(\d+)_', name)
                if not match or int(match[1]) not in identities:
                    continue
                pid = int(match[1])
                row = result.setdefault(pid, dict(pid=pid, created=identities[pid], instances={}))
                instance = row['instances'].setdefault(name, {})
                instance[key] = (int(item.value.number.large) if item.value.status in (0, 1) else None)
                instance[key+'_status'] = int(item.value.status)
        for row in result.values():
            for key in self.counters:
                values = [v[key] for v in row['instances'].values() if v.get(key) is not None]
                row[key] = sum(values) if values else None
        return list(result.values())

    def close(self):
        if self.query:
            self._check(self.api.PdhCloseQuery(self.query)); self.query = None


GpuCounters.Value._fields_ = [('status', C.c_uint32), ('number', GpuCounters.Number)]
GpuCounters.Item._fields_ = [('name', C.c_wchar_p), ('value', GpuCounters.Value)]


def job_pids(job):
    api = job._kernel.QueryInformationJobObject
    api.argtypes = [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32, C.c_void_p]
    api.restype = C.c_int
    capacity = 256
    data = C.create_string_buffer(8+capacity*C.sizeof(C.c_size_t))
    if not api(job._handle, 3, data, C.sizeof(data), None):
        raise C.WinError(C.get_last_error())
    assigned, count = C.cast(data, C.POINTER(C.c_uint32))[:2]
    if count > capacity or assigned > capacity:
        raise RuntimeError('Diagnostic job exceeded 256 processes')
    return list(C.cast(C.byref(data, 8), C.POINTER(C.c_size_t))[:count])


def safety_reason(record, elapsed, timeout):
    """Bound the diagnostic, including GPU allocations that CPU commit misses."""
    if record.get('gpu_error'):
        return 'GPU measurement failed; stop rather than run without GPU accounting'
    gpu_commit = sum(row.get('gpu_committed_bytes') or 0 for row in record.get('gpu_processes', []))
    if gpu_commit > 4*GIB:
        return 'Owned GPU committed bytes exceeded 4 GiB diagnostic limit'
    if (record['private_bytes'] > 8*GIB or
        min(record['system']['free_commit_bytes'], record['system']['free_physical_bytes']) < 8*GIB):
        return 'Job private commit or system headroom diagnostic limit reached'
    if elapsed > timeout:
        return 'Elapsed-time diagnostic limit reached'
    return None


def rebuild_live(width, height, settings):
    """Exercise actual native resize in a normal, job-owned production worker.

    The diagnostic factory runs before the ready reply. Production retirement
    intentionally exits this worker without NGX shutdown (some runtimes hang
    there). This measures feature release/recreate independently of that known
    shutdown limitation; it does not change the native release implementation.
    """
    import numpy as np
    from dlss5tool.dlss_engine import Live
    work = Path(os.environ['DLSS5TOOL_REBUILD_WORKDIR'])
    cycles = int(os.environ['DLSS5TOOL_REBUILD_CYCLES'])
    settle = float(os.environ['DLSS5TOOL_REBUILD_SETTLE'])
    live = Live(width, height, settings)

    def event(kind, **data):
        row = dict(event=kind, wall=time.time(), pid=os.getpid(), **data)
        with (work/'events.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(row, ensure_ascii=False, default=str)+'\n')

    event('opened', cycle=0, worker_pid=os.getpid(), job_armed=True,
          backend=live.backend, adapter=live.adapter_info, diagnostic_factory=True)
    for cycle in range(cycles):
        if (work/'cancel.flag').exists():
            raise RuntimeError('Supervisor stopped native rebuild diagnostic')
        w, h = ((width, height) if cycle < cycles//2 or cycle % 2 else (800, 448))
        live.resize(w, h)
        event('rebuilt', cycle=cycle, worker_pid=os.getpid(), width=w, height=h)
        image = np.empty((h, w, 4), dtype=np.uint8)
        image[..., 0] = np.arange(w, dtype=np.uint16)[None, :] % 256
        image[..., 1] = np.arange(h, dtype=np.uint16)[:, None] % 256
        image[..., 2] = 96; image[..., 3] = 255
        for index in range(3):
            result = live.process(image, reset=index == 0)
            if result is None or result.shape != image.shape or result.dtype != image.dtype:
                raise RuntimeError('Unexpected native rebuild output')
        event('processed', cycle=cycle, width=w, height=h, frames=3,
              sha256=hashlib.sha256(memoryview(result)).hexdigest())
        deadline = time.monotonic()+settle
        while time.monotonic() < deadline:
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor stopped native rebuild settling')
            time.sleep(min(.1, max(0, deadline-time.monotonic())))
    if (w, h) != (width, height):
        live.resize(width, height)
        event('restore-native-size', width=width, height=height)
    return live


def workload(args, work):
    # The parent assigns our job before any application/native imports.
    if sys.stdin.buffer.read(1) != b'G':
        raise RuntimeError('Missing supervisor startup gate')
    os.environ.update(TEMP=str(work), TMP=str(work),
        DLSS5TOOL_SETTINGS_PATH=str(work/'settings.json'),
        DLSS5TOOL_QUEUE_PATH=str(work/'queue.json'), DLSS5TOOL_DISABLE_UPDATE_CHECK='1')
    from dlss5tool import paths
    state = work/'state'; state.mkdir(exist_ok=True)
    paths.state_path = lambda filename: state/filename
    import numpy as np
    from dlss5tool import dlss_engine
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.super_resolution import ProcessSuperResolution
    from multiprocessing import shared_memory
    config = dict(host_backend='v2', host_auto_fallback=False, guidance_mode=0,
        dlss_runtime=str(ROOT/'runtime/nvngx_dlssnr.dll'), host_submission='compatibility',
        host_in_flight=1, host_persistent_buffers=True, host_zero_fast_path=True,
        style=0, intensity=1.0, frame_format='rgba8', color_profile='srgb')
    dlss_engine.LOG_PATH = str(work/'direct-host.log')
    owner = None
    events_path = work/'events.jsonl'

    def event(kind, **data):
        row = dict(event=kind, wall=time.time(), pid=os.getpid(), **data)
        with events_path.open('a', encoding='utf-8') as out:
            out.write(json.dumps(row, ensure_ascii=False, default=str)+'\n')
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)

    def pause(seconds):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor safety stop')
            time.sleep(min(.2, max(0, end-time.monotonic())))

    def frame(w, h):
        image = np.empty((h, w, 4), dtype=np.uint8)
        image[..., 0] = np.arange(w, dtype=np.uint16)[None, :] % 256
        image[..., 1] = np.arange(h, dtype=np.uint16)[:, None] % 256
        image[..., 2] = 96; image[..., 3] = 255
        return image

    def process_frames(cycle, w=640, h=360):
        is_sr = args.case.startswith('sr-')
        image = frame(w//2, h//2) if is_sr else frame(w, h)
        for index in range(3):
            output = owner.process(image) if is_sr else owner.process(image, reset=index == 0)
            if output is None or output.shape != (h, w, 4) or output.dtype != np.uint8:
                raise RuntimeError('Unexpected processed frame contract')
        event('processed', cycle=cycle, width=w, height=h, frames=3,
            sha256=hashlib.sha256(memoryview(output)).hexdigest())

    def identify_owner():
        if args.case.startswith('sr-'):
            return owner._process.pid, owner._job
        return owner._session._process.pid, owner._session._job

    def create():
        if args.case.startswith('sr-'):
            live = ProcessSuperResolution(320, 180, 2)
        else:
            live = ProcessLive(640, 360, config)
        return live

    def close(cycle):
        nonlocal owner
        pid, _ = identify_owner()
        session = getattr(owner, '_session', owner)
        names = [getattr(session, name).name for name in ('_input_memory', '_output_memory')
                 if getattr(session, name, None) is not None]
        log_path = getattr(session, 'log_path', None)
        event('close-start', cycle=cycle, worker_pid=pid)
        # Preserve the retired host log between the real retirement and the
        # owner's ordinary log unlink. GPU operations and cleanup stay intact.
        real_retire = getattr(session, '_retire_process_tree', None)
        if log_path and real_retire is not None:
            def capture_retired_log():
                real_retire()
                if Path(log_path).is_file():
                    (work/f'cycle-{cycle}-host.log').write_bytes(Path(log_path).read_bytes())
            session._retire_process_tree = capture_retired_log
        try:
            owner.close()
        finally:
            if log_path and real_retire is not None:
                session._retire_process_tree = real_retire
        owner = None
        for name in names:
            try:
                unexpected = shared_memory.SharedMemory(name=name, track=False)
            except FileNotFoundError:
                continue
            unexpected.close()
            raise RuntimeError('Shared frame still exists after close')
        if log_path and Path(log_path).is_file():
            (work/f'cycle-{cycle}-host.log').write_bytes(Path(log_path).read_bytes())
        event('closed', cycle=cycle, worker_pid=pid, shared_frames_unlinked=len(names))
        pause(args.gap_seconds)
        event('post-close', cycle=cycle, worker_pid=pid)

    try:
        event('baseline-start'); pause(args.gap_seconds); event('baseline-end')
        if args.case == 'nr-rebuild':
            os.environ.update(DLSS5TOOL_REBUILD_WORKDIR=str(work),
                DLSS5TOOL_REBUILD_CYCLES=str(args.cycles),
                DLSS5TOOL_REBUILD_SETTLE=str(args.settle_seconds))
            owner = ProcessLive(640, 360, config, _live_factory=rebuild_live)
            event('factory-ready', worker_pid=identify_owner()[0])
            close(args.cycles-1)
            dlss_engine.close_adapter_probe()
            event('completed', cycles=args.cycles)
            return
        for cycle in range(args.cycles):
            if owner is None:
                owner = create()
                pid, job = identify_owner()
                if args.case != 'nr-rebuild' and job is None:
                    raise RuntimeError('Production owner has no lifetime job')
                event('opened', cycle=cycle, worker_pid=pid,
                    job_armed=job is not None, backend=getattr(owner, 'backend', None),
                    adapter=getattr(owner, 'adapter_info', None))
            if args.case == 'nr-parameters':
                owner.update(dict(style=cycle % 2, intensity=.7+.05*(cycle % 8)))
                event('parameter-update', cycle=cycle, worker_pid=identify_owner()[0])
                process_frames(cycle); pause(args.settle_seconds)
            else:
                process_frames(cycle)
                event('idle-start', cycle=cycle, worker_pid=identify_owner()[0])
                pause(args.idle_seconds)
                event('idle-end', cycle=cycle, worker_pid=identify_owner()[0])
            if args.case.endswith('-recycle'):
                close(cycle)
        if owner is not None:
            close(args.cycles-1)
        dlss_engine.close_adapter_probe()
        event('completed', cycles=args.cycles)
    except BaseException:
        event('error', traceback=traceback.format_exc()); raise
    finally:
        if owner is not None:
            owner.close()
        dlss_engine.close_adapter_probe()


def supervise(args, work):
    from scripts.ngx_updater_lifecycle_probe import Processes
    from scripts.verify_p0_stability import Memory, run_manifest
    from dlss5tool.process_job import KillOnCloseJob
    processes = Processes(); memory = Memory(processes); performance = Performance()
    gpu = GpuCounters() if args.gpu_counters else None
    before = run_manifest(args.task)
    before['probe_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(work/'manifest-before.json', before)
    known, samples, gpu_errors = {}, [], []
    start = time.monotonic(); next_print = 0; exit_seen = None
    safety_error, exception = None, None
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--task', str(args.task),
        '--case', args.case, '--label', args.label, '--cycles', str(args.cycles),
        '--idle-seconds', str(args.idle_seconds), '--gap-seconds', str(args.gap_seconds),
        '--settle-seconds', str(args.settle_seconds), '--worker']
    environment = dict(os.environ, TEMP=str(work), TMP=str(work), PYTHONDONTWRITEBYTECODE='1')
    job = None; residual = []
    with (work/'console.log').open('wb') as console:
        child = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=console, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job = KillOnCloseJob(child)
            child.stdin.write(b'G'); child.stdin.flush(); child.stdin.close()
            while True:
                now = time.monotonic()
                current = []; identities = {}
                members = set(job_pids(job))
                snapshot = {r['pid']: r for r in processes.snapshot()}
                rows = {pid:r for pid,r in snapshot.items() if pid in members}
                for pid, row in rows.items():
                    identity = processes.identity(row)
                    created = identity.get('created')
                    if created is None:
                        continue
                    identities[pid] = created; known[(pid, created)] = identity
                    value = memory.read(identity)
                    # Re-check the creation time after the memory handle closed.
                    if processes.identity(row).get('created') == created:
                        current.append(value)
                system = performance.read()
                record = dict(wall=time.time(), elapsed_seconds=now-start, members=sorted(members),
                    processes=current, private_bytes=sum(r.get('private_bytes', 0) for r in current),
                    system=system)
                if gpu is not None:
                    try:
                        # Retain exited identities while PDH retires their
                        # counters, but never attribute a reused PID to us.
                        gpu_ids = dict(identities)
                        for (pid, created), identity in known.items():
                            if pid not in snapshot:
                                gpu_ids[pid] = created
                        record['gpu_processes'] = gpu.read(gpu_ids)
                    except Exception as error:
                        record['gpu_error'] = str(error); gpu_errors.append(str(error))
                samples.append(record)
                with (work/'samples.jsonl').open('a', encoding='utf-8') as output:
                    output.write(json.dumps(record)+'\n')
                if now-start >= next_print:
                    print(json.dumps(dict(case=args.case, seconds=round(now-start),
                        processes=len(members), private_mib=round(record['private_bytes']/MIB, 1),
                        system_commit_mib=round(system['commit_bytes']/MIB, 1))), flush=True)
                    next_print += 20
                reason = safety_reason(record, now-start, args.timeout)
                if safety_error is None and reason:
                    safety_error = reason
                    (work/'cancel.flag').write_text(safety_error, encoding='utf-8')
                    stop_deadline = now+10
                if child.poll() is not None:
                    exit_seen = exit_seen or now
                    if not members or now-exit_seen >= 10:
                        break
                if safety_error and now >= stop_deadline:
                    break
                time.sleep(.5)
        except BaseException:
            exception = traceback.format_exc()
        finally:
            if job is not None:
                residual = job_pids(job)
                job.close()
            elif child.poll() is None:
                child.terminate()  # exact owned Popen, startup gate never released
            child.wait(timeout=20)
            if gpu is not None:
                gpu.close()
    after = run_manifest(args.task)
    after['probe_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_json(work/'manifest-after.json', after)
    passed = child.returncode == 0 and not residual and not safety_error and not exception
    result = dict(processing_and_exit_passed=passed, child_exit_code=child.returncode,
        residual_before_supervisor_job_close=residual, safety_error=safety_error,
        supervisor_exception=exception, unchanged=before == after,
        gpu_counters=args.gpu_counters, gpu_errors=gpu_errors,
        elapsed_seconds=time.monotonic()-start, observed_processes=list(known.values()),
        peak_job_private_bytes=max((s['private_bytes'] for s in samples), default=0),
        arguments=vars(args), long_term_plateau_verified=False)
    write_json(work/'result.json', result)
    print(json.dumps({k:result[k] for k in ('processing_and_exit_passed','child_exit_code',
        'residual_before_supervisor_job_close','safety_error','supervisor_exception','unchanged')}), flush=True)
    return 0 if passed and before == after and not gpu_errors else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--case', required=True, choices=('nr-hold','nr-recycle','sr-hold',
        'sr-recycle','nr-parameters','nr-rebuild'))
    parser.add_argument('--label', default='formal')
    parser.add_argument('--cycles', type=int, default=3)
    parser.add_argument('--idle-seconds', type=float, default=60)
    parser.add_argument('--gap-seconds', type=float, default=10)
    parser.add_argument('--settle-seconds', type=float, default=1)
    parser.add_argument('--timeout', type=float, default=420)
    parser.add_argument('--gpu-counters', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    from scripts.verify_p0_stability import checked_path
    args.task = checked_path(args.task)
    if (not args.task.is_relative_to(ROOT/'tmp') or not (args.task/'TASK.md').is_file()
        or not re.fullmatch(r'[a-z0-9-]+', args.label) or not 1 <= args.cycles <= 32
        or not 0 <= args.idle_seconds <= 180 or not 1 <= args.gap_seconds <= 30
        or not .5 <= args.settle_seconds <= 5 or not 30 <= args.timeout <= 1800):
        parser.error('Registered repository tmp task and bounded arguments required')
    work = checked_path(args.task/(args.case+'-'+args.label))
    if args.worker:
        workload(args, work); return 0
    work.mkdir(exist_ok=False)
    return supervise(args, work)


if __name__ == '__main__':
    raise SystemExit(main())
