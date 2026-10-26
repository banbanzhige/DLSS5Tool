"""Bounded SR research: GPU timestamps, quality levels, production cached export.

Use a registered repository tmp task. Reuses .venv/runtime/SDK, never changes
production code or quality defaults. NVIDIA samples cover the entire GPU, not
just this process. PDH counters retain PID/engine attribution. Native workers
exit after writing evidence instead of exercising the known slow NGX shutdown;
therefore their throughput is explicitly a warm processing measurement.
"""
import argparse
from collections import defaultdict
import ctypes as C
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
FIELDS = ('gpu', 'memory_busy', 'encoder', 'decoder', 'sm_clock_mhz', 'mem_clock_mhz',
          'power_w', 'vram_mib')
NATIVE_FIELDS = ('cpu_pack_input', 'cpu_record', 'cpu_submit_wait', 'cpu_unpack_output',
                 'gpu_upload', 'gpu_vsr', 'gpu_readback', 'gpu_total')


def summary(values):
    return dict(mean=statistics.mean(values), median=statistics.median(values),
                p95=sorted(values)[min(len(values)-1, int(len(values)*.95))],
                min=min(values), max=max(values)) if values else None


def validate_task(path):
    path = path.absolute()
    if path.resolve() != path or not path.is_relative_to(ROOT/'tmp'):
        raise ValueError('Task must resolve inside repository tmp')
    for item in (path, *path.parents):
        if item.lstat().st_file_attributes & 0x400:
            raise ValueError(f'Reparse point: {item}')
    if not (path/'TASK.md').is_file() or shutil.disk_usage(path).free < 16*1024**3:
        raise ValueError('Registered task and >=16 GiB free required')
    return path


class Sampler:
    def __init__(self):
        self.samples, self.engines, self.errors = [], [], []
        self.stop = threading.Event()

    def start(self):
        query = ('utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,'
                 'clocks.sm,clocks.mem,power.draw,memory.used')
        self.process = subprocess.Popen(['nvidia-smi', f'--query-gpu={query}',
            '--format=csv,noheader,nounits', '-lms', '200'], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        def read():
            for line in self.process.stdout:
                try:
                    self.samples.append(dict(time=time.perf_counter(),
                        **dict(zip(FIELDS, map(float, line.strip().split(','))))))
                except ValueError:
                    self.errors.append(line.strip())
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        self.pdh_thread = threading.Thread(target=self.pdh, daemon=True)
        self.pdh_thread.start()

    def pdh(self):
        query = C.c_void_p()
        try:
            pdh = C.WinDLL('pdh')
            pdh.PdhOpenQueryW.argtypes = [C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
            pdh.PdhAddEnglishCounterW.argtypes = [C.c_void_p, C.c_wchar_p, C.c_size_t, C.POINTER(C.c_void_p)]
            pdh.PdhCollectQueryData.argtypes = [C.c_void_p]
            pdh.PdhGetFormattedCounterArrayW.argtypes = [C.c_void_p, C.c_ulong,
                C.POINTER(C.c_ulong), C.POINTER(C.c_ulong), C.c_void_p]
            pdh.PdhCloseQuery.argtypes = [C.c_void_p]
            class Value(C.Structure):
                _fields_ = [('status', C.c_ulong), ('value', C.c_double)]
            class Item(C.Structure):
                _fields_ = [('name', C.c_wchar_p), ('formatted', Value)]
            counter = C.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, C.byref(query)):
                raise RuntimeError('PdhOpenQueryW failed')
            if pdh.PdhAddEnglishCounterW(query, r'\GPU Engine(*)\Utilization Percentage', 0, C.byref(counter)):
                raise RuntimeError('GPU Engine counter unavailable')
            pdh.PdhCollectQueryData(query)
            while not self.stop.wait(.5):
                if pdh.PdhCollectQueryData(query):
                    continue
                size, count = C.c_ulong(), C.c_ulong()
                pdh.PdhGetFormattedCounterArrayW(counter, 0x200, C.byref(size), C.byref(count), None)
                if not size.value:
                    continue
                buffer = C.create_string_buffer(size.value)
                if pdh.PdhGetFormattedCounterArrayW(counter, 0x200, C.byref(size), C.byref(count), buffer):
                    continue
                values = C.cast(buffer, C.POINTER(Item))
                active = {values[i].name: values[i].formatted.value for i in range(count.value)
                          if values[i].formatted.status in (0, 1) and values[i].formatted.value > .01}
                self.engines.append(dict(time=time.perf_counter(), values=active))
        except Exception as error:
            self.errors.append(f'PDH: {error}')
        finally:
            if query.value:
                pdh.PdhCloseQuery(query)

    def close(self):
        self.stop.set()
        self.process.terminate()
        self.process.wait(timeout=5)
        self.thread.join(timeout=5)
        self.pdh_thread.join(timeout=5)


def native_worker(args):
    import numpy as np
    from dlss5tool import super_resolution
    from scripts.profile_cached_export import private_bytes
    from dlss5tool.memory_budget import system_memory
    dll = args.task/'native'/'profile.dll'
    library = C.WinDLL(str(dll))
    library.vsr_init.argtypes = [C.c_int]*5 + [C.c_wchar_p]*2
    library.vsr_init.restype = C.c_int
    library.vsr_process.argtypes = [C.c_void_p, C.c_void_p]
    library.vsr_process.restype = C.c_int
    library.probe_profile_init.restype = C.c_int
    library.probe_profile_frame.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(C.c_double)]
    library.probe_profile_frame.restype = C.c_int
    frames = []
    dtype = np.float16 if args.hdr else np.uint8
    # Moving colored patterns; bounded input window, no retained frame history.
    x = np.arange(args.width, dtype=np.uint16)[None, :]
    y = np.arange(args.height, dtype=np.uint16)[:, None]
    for shift in range(4):
        frame = np.empty((args.height, args.width, 4), dtype=dtype)
        for channel in range(3):
            channel_value = ((x*(channel+1) + y + shift*17) % 256)
            frame[..., channel] = channel_value/255 if args.hdr else channel_value
        frame[..., 3] = 1 if args.hdr else 255
        frames.append(frame)
    output = np.empty((args.height*args.scale, args.width*args.scale, 4), dtype=dtype)
    started = time.perf_counter()
    if not library.vsr_init(args.width, args.height, args.scale, args.quality, int(args.hdr),
            super_resolution.BASE, str(args.task/f'{args.label}-native.log')):
        raise RuntimeError('VSR init failed')
    initialization = time.perf_counter()-started
    if not library.probe_profile_init():
        raise RuntimeError('GPU timestamp init failed')
    timings = (C.c_double*8)()
    stages = defaultdict(list)
    walls, marks, private = [], [], []
    active_start = None
    calls = 0
    loop_start = time.perf_counter()
    while calls < args.frames or (active_start and time.perf_counter()-active_start < args.seconds):
        if time.perf_counter()-loop_start > 60:
            raise RuntimeError('60 second native processing limit')
        if calls == 8:
            active_start = time.perf_counter()
        start = time.perf_counter()
        if not library.probe_profile_frame(frames[calls % len(frames)].ctypes.data,
                None if args.no_readback else output.ctypes.data, timings):
            raise RuntimeError('VSR evaluation failed')
        end = time.perf_counter()
        if calls >= 8:
            walls.append((end-start)*1000)
            for name, value in zip(NATIVE_FIELDS, timings):
                stages[name].append(value)
        calls += 1
        if calls % 32 == 0:
            memory = system_memory()
            usage = private_bytes(os.getpid())
            private.append(usage)
            if (usage or 0) > 4*1024**3 or (memory and
                    min(memory['free_physical_bytes'], memory['free_commit_bytes']) < 4*1024**3):
                raise RuntimeError('Probe memory safety stop')
    active_end = time.perf_counter()
    check = None
    if not args.no_readback:
        check = np.empty_like(output)
        # Same session, same repeated input, instrumented vs ordinary host call.
        library.probe_profile_frame(frames[0].ctypes.data, output.ctypes.data, timings)
        if not library.vsr_process(frames[0].ctypes.data, check.ctypes.data):
            raise RuntimeError('Uninstrumented control failed')
        check = dict(equal=bool(np.array_equal(check, output)),
            max_absolute_difference=float(np.abs(check.astype(np.float32)-output.astype(np.float32)).max()))
        np.save(args.task/f'{args.label}-sample.npy', output, allow_pickle=False)
    report = dict(status='complete', mode='native', pid=os.getpid(), quality=args.quality,
        width=args.width, height=args.height, scale=args.scale, hdr=args.hdr,
        no_readback=args.no_readback, initialization_seconds=initialization, calls=calls,
        active_start=active_start, active_end=active_end, warm_frames=len(walls),
        warm_fps=len(walls)/(active_end-active_start), wall_ms=summary(walls),
        stages_ms={name: summary(values) for name, values in stages.items()},
        instrumented_pixel_control=check, private_bytes_samples=private,
        runtime_sha256=hashlib.sha256(Path(super_resolution.VSR_RUNTIME_DLL).read_bytes()).hexdigest(),
        notes='Direct disposable native worker; preloaded inputs; no decode/IPC/cache/encode. '
              'GPU timestamps include scheduling gaps; GPU stages nest inside CPU submit/wait. '
              'Warm throughput excludes startup and shutdown; no-readback is an upper-bound experiment.')
    library.probe_profile_release()
    return report


def cache_worker(args):
    import cv2
    import numpy as np
    from contextlib import ExitStack
    from dlss5tool import super_resolution, video_export, frame_generation, render_cache
    from scripts import profile_cached_export as cached
    if args.opencv_threads is not None:
        cv2.setNumThreads(args.opencv_threads)
    marks, pids, calls = {}, [], 0
    fine, fine_lock = defaultdict(list), threading.Lock()
    resize = cv2.resize
    convert = cv2.cvtColor
    def timed(name, function, *a, **kw):
        start = time.perf_counter()
        try:
            return function(*a, **kw)
        finally:
            with fine_lock:
                fine[name].append((time.perf_counter()-start)*1000)
    def resize_timed(frame, size, *a, **kw):
        name = f'resize:{tuple(frame.shape)}->{size}:contiguous={frame.flags.c_contiguous}'
        return timed(name, resize, frame, size, *a, **kw)
    def convert_timed(frame, code, *a, **kw):
        return timed(f'cvtColor:{tuple(frame.shape)}:code={code}', convert, frame, code, *a, **kw)
    def fast_cut(previous, current):
        a = cv2.resize(previous, (64, 64), interpolation=cv2.INTER_AREA)[..., :3].astype(np.float32)
        b = cv2.resize(current, (64, 64), interpolation=cv2.INTER_AREA)[..., :3].astype(np.float32)
        return float(np.mean(np.abs(a-b))) / 255 > .45
    original = super_resolution.ProcessSuperResolution.process
    original_init = super_resolution.ProcessSuperResolution.__init__
    original_write = video_export.FFmpegVideoWriter.write
    def init(instance, *a, **kw):
        result = original_init(instance, *a, **kw)
        pids.append(instance._process.pid)
        return result
    def process(instance, *a, **kw):
        nonlocal calls
        calls += 1
        if calls == 9:
            marks['active_start'] = time.perf_counter()
        return original(instance, *a, **kw)
    def write(instance, *a, **kw):
        result = original_write(instance, *a, **kw)
        marks['active_end'] = time.perf_counter()
        return result
    sys.argv = [str(ROOT/'scripts/profile_cached_export.py'), '--task', str(args.task),
        '--label', args.label, '--case', args.case, '--frames', str(args.frames),
        '--width', str(args.width), '--height', str(args.height), '--scale', str(args.scale),
        '--color-profile', args.color_profile, '--multiplier', str(args.multiplier)]
    if args.no_frame_hashes:
        sys.argv.append('--no-frame-hashes')
    if args.hard_cut:
        sys.argv.append('--hard-cut')
    if args.baseline_ref:
        sys.argv.extend(('--baseline-ref', args.baseline_ref))
    candidate = None
    if args.reuse_scene_cuts or args.small_scene_proxy:
        # Exact, asserted substitutions in an isolated function, never a source edit.
        source = inspect.getsource(frame_generation.export_video)
        old = 'scene_proxy = analysis_rgba8(rgba, config)'
        proxy = 'analysis_rgba8(rgba, config)'
        if args.small_scene_proxy:
            proxy = f'cv2.resize({proxy}, (64, 64), interpolation=cv2.INTER_AREA)'
        if args.reuse_scene_cuts:
            proxy = f'({proxy}) if input_session is None else None'
        if source.count(old) != 1:
            raise ValueError('Historical scene prototype is already in production; use the default path and --baseline-ref for comparison')
        source = source.replace(old, f'scene_proxy = {proxy}')
        old = 'previous_scene_proxy = cv2.resize(scene_proxy, (64, 64), interpolation=cv2.INTER_AREA)'
        new = ('previous_scene_proxy = scene_proxy' if args.small_scene_proxy else
               'previous_scene_proxy = (cv2.resize(scene_proxy, (64, 64), interpolation=cv2.INTER_AREA) '
               'if scene_proxy is not None else None)')
        assert source.count(old) == 1
        source = source.replace(old, new)
        namespace = dict(frame_generation.__dict__)
        if args.fast_cut:
            namespace['is_cut'] = fast_cut
        exec(compile(source, '<isolated-scene-candidate>', 'exec'), namespace)
        candidate = namespace['export_video']
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(super_resolution.ProcessSuperResolution, '__init__', init))
        stack.enter_context(mock.patch.object(super_resolution.ProcessSuperResolution, 'process', process))
        stack.enter_context(mock.patch.object(video_export.FFmpegVideoWriter, 'write', write))
        stack.enter_context(mock.patch.object(cv2, 'resize', resize_timed))
        stack.enter_context(mock.patch.object(cv2, 'cvtColor', convert_timed))
        if args.fast_cut:
            stack.enter_context(mock.patch.object(frame_generation, 'is_cut', fast_cut))
        if candidate is not None:
            stack.enter_context(mock.patch.object(frame_generation, 'export_video', candidate))
            stack.enter_context(mock.patch.object(render_cache, 'export_video', candidate))
        result = cached.main()
    cached_report = json.loads((args.task/f'{args.label}-{args.case}.json').read_text(encoding='utf-8'))
    return dict(status='complete' if result == 0 else 'failed', mode='cache',
        pid=os.getpid(), vsr_pids=pids, fast_cut=args.fast_cut, reuse_scene_cuts=args.reuse_scene_cuts,
        small_scene_proxy=args.small_scene_proxy, frame_hash_audit=not args.no_frame_hashes,
        opencv_threads=cv2.getNumThreads(),
        steady_output_fps=cached_report.get('steady_output_fps'),
        baseline_commit=cached_report.get('baseline_commit'),
        component_pids=cached_report.get('registered_process_ids', []),
        fine_stages_ms={name: dict(calls=len(values), total_ms=sum(values),
                                 warm=summary(values[8:] or values)) for name, values in fine.items()}, **marks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--mode', choices=('native', 'cache'), default='native')
    parser.add_argument('--case', choices=('sr', 'sr-nr'), default='sr')
    parser.add_argument('--quality', type=int, choices=(1, 2, 3, 4), default=4)
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=540)
    parser.add_argument('--scale', type=int, choices=(2, 4), default=2)
    parser.add_argument('--multiplier', type=int, choices=(1, 2), default=1)
    parser.add_argument('--frames', type=int, default=128)
    parser.add_argument('--seconds', type=float, default=6)
    parser.add_argument('--hdr', action='store_true')
    parser.add_argument('--no-readback', action='store_true')
    parser.add_argument('--fast-cut', action='store_true', help='Research variant: resize contiguous RGBA before selecting RGB')
    parser.add_argument('--no-frame-hashes', action='store_true')
    parser.add_argument('--color-profile', choices=('srgb', 'hdr10_pq', 'hdr10_hlg'), default='srgb',
                        help='Synthetic cache source encoding; native HDR uses --hdr')
    parser.add_argument('--hard-cut', action='store_true', help='Use a synthetic black/white cut in cache mode')
    parser.add_argument('--baseline-ref', help='Read cache/export core from a fixed local Git commit')
    parser.add_argument('--reuse-scene-cuts', action='store_true', help='Research: consume upstream cuts without recomputing them')
    parser.add_argument('--small-scene-proxy', action='store_true', help='Research: build 64x64 RGBA scene proxy once per source frame')
    parser.add_argument('--opencv-threads', type=int, choices=(1, 2, 4, 8, 20))
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline_ref and (args.mode != 'cache' or args.fast_cut or args.reuse_scene_cuts or args.small_scene_proxy):
        raise ValueError('Frozen baseline accepts cache mode without prototype substitutions')
    args.task = validate_task(args.task)
    if not re.fullmatch('[a-zA-Z0-9-]+', args.label):
        raise ValueError('Plain label required')
    if (min(args.width, args.height) < 128 or args.width*args.height*args.scale**2 > 3840*2160
            or not 16 <= args.frames <= 256 or not 0 <= args.seconds <= 20):
        raise ValueError('Probe limited to <=4K, 16..256 frames, <=20 seconds minimum')
    report_path = args.task/f'{args.label}-efficiency.json'
    if args.worker:
        if report_path.exists():
            raise ValueError('Refusing overwrite')
        report = native_worker(args) if args.mode == 'native' else cache_worker(args)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        sys.stdout.flush()
        # Reclaim this isolated native research session without slow SDK shutdown.
        if args.mode == 'native':
            os._exit(0)
        return 0 if report['status'] == 'complete' else 1
    telemetry_path = args.task/f'{args.label}-telemetry.json'
    if report_path.exists() or telemetry_path.exists():
        raise ValueError('Refusing overwrite')
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TEMP=str(args.task), TMP=str(args.task))
    sampler = Sampler()
    sampler.start()
    try:
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
            *sys.argv[1:], '--worker'], cwd=ROOT, env=environment, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=150, creationflags=NO_WINDOW)
    finally:
        sampler.close()
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    start, end = report.get('active_start', float('inf')), report.get('active_end', -1)
    selected = [sample for sample in sampler.samples if start <= sample['time'] <= end]
    target_pids = [report.get('pid'), *report.get('vsr_pids', []), *report.get('component_pids', [])]
    engines = defaultdict(list)
    for sample in sampler.engines:
        if start <= sample['time'] <= end:
            totals = defaultdict(float)
            for instance, value in sample['values'].items():
                match = re.match(r'pid_(\d+)_.*engtype_(.+)', instance)
                if match and int(match[1]) in target_pids:
                    totals[match[2]] += value
            for kind in set(totals) | set(engines):
                engines[kind].append(totals[kind])
    telemetry = dict(returncode=result.returncode, stdout=result.stdout[-4000:], stderr=result.stderr[-4000:],
        sample_interval_seconds=.2, scope='NVIDIA samples: whole GPU; PDH: target PID engine counters',
        active_samples=len(selected), active_summary={key: summary([s[key] for s in selected]) for key in FIELDS},
        pid_engine_percent={key: summary(values) for key, values in engines.items()},
        errors=sampler.errors, samples=sampler.samples, pdh_samples=sampler.engines)
    telemetry_path.write_text(json.dumps(telemetry, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(label=args.label, status=report.get('status'),
        fps=report.get('warm_fps', report.get('steady_output_fps')), gpu=telemetry['active_summary'].get('gpu'),
        engines=telemetry['pid_engine_percent'], errors=sampler.errors, returncode=result.returncode), ensure_ascii=False))
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
