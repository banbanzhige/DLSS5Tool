"""Bounded real-runtime P0 verification with process-tree memory/exit evidence.

Use only a registered tmp task. Workers get isolated settings and synthetic
media. The supervisor never terminates processes by name: recovery requires an
observed descendant PID and its exact creation time. No models are downloaded.
Result status covers processing and process-tree exit only; memory observations
are reported separately and do not certify a long-term plateau.
"""
import argparse
import ctypes as C
import gc
import hashlib
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
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GIB = 1024**3
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def disable_legacy_extensions():
    """Diagnostic-only, per-process policy; never changes system settings."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.SetProcessMitigationPolicy.argtypes = [C.c_int, C.c_void_p, C.c_size_t]
    kernel.SetProcessMitigationPolicy.restype = C.c_int
    flags = C.c_uint32(1)
    if not kernel.SetProcessMitigationPolicy(6, C.byref(flags), C.sizeof(flags)):
        raise C.WinError(C.get_last_error())
    return {'pid': os.getpid(), 'disable_legacy_extension_points': True}


# Spawned GPU workers inherit this diagnostic flag. Set the policy before
# importing project/graphics modules, including during multiprocessing startup.
EXTENSION_POLICY = (disable_legacy_extensions()
    if os.environ.get('DLSS5TOOL_P0_DISABLE_EXTENSIONS') == '1' else None)


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def checked_path(path):
    absolute = Path(os.path.abspath(path))
    for item in [absolute, *absolute.parents]:
        if item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Reparse point rejected: {item}')
    if absolute.resolve() != absolute:
        raise ValueError(f'Unexpected resolved path: {absolute}')
    return absolute


def run_manifest(task=None):
    names = ['dlss5tool/'+name+'.py' for name in ('adapter_probe','adapter_inventory','process_job','dlss_engine',
        'gui','dlss_host_process','memory_budget','super_resolution','render_cache',
        'shared_render_preview','residual_composition','local_preview_composition','preview_jobs','__main__')]
    names += ['gui.py','scripts/verify_p0_stability.py','scripts/profile_native_frame_memory.py',
              'scripts/inspect_native_allocations.py','scripts/profile_thread_activity.py',
              'scripts/native_thread_activity.cpp','scripts/native_heap_trace.cpp']
    names += ['native/realtime/'+name for name in ('quality_bridge.cpp','quality.cpp','quality.h',
        'quality_gpu_output.cpp','quality_gpu_output.h')]
    names += ['scripts/prepare_realtime_build.py', 'third_party/realtime/veyra/shaders/NrResidualComposite.hlsl',
              'third_party/realtime/veyra/shaders/NrProtection.hlsli']
    if task is not None:
        for flag, binary in (('THREAD_ACTIVITY','native_thread_activity.dll'),
                             ('HEAP_TRACE','native_heap_trace.dll')):
            if os.environ.get('DLSS5TOOL_P0_'+flag) == '1':
                name = str(checked_path(task/binary).relative_to(ROOT))
                if name not in names:
                    names.append(name)
        candidate = os.environ.get('DLSS5TOOL_P0_COMPOSER_HOST')
        if candidate:
            path = checked_path(candidate)
            if not path.is_relative_to(task) or not path.is_file() or path.suffix.lower() != '.dll':
                raise ValueError('Composer candidate must be a regular task-local DLL')
            names.append(str(path.relative_to(ROOT)))
            for name in ('quality_bridge.cpp','quality.cpp','quality.h'):
                source = checked_path(path.parent/name)
                if source.is_file():
                    names.append(str(source.relative_to(ROOT)))
    names += ['runtime/'+name+'.dll' for name in ('vsr_host','nvngx_vsr','nvngx_dlssnr','dlssnr_host_v2','dlss5_quality')]
    manifest = []
    for name in names:
        path = ROOT/name
        with path.open('rb') as source:
            digest = hashlib.file_digest(source,'sha256').hexdigest()
        manifest.append(dict(file=name,bytes=path.stat().st_size,sha256=digest))
    return dict(files=manifest, diagnostic_environment={key:os.environ.get(key) for key in
        ('DLSS5TOOL_P0_MEMORY_MAP','DLSS5TOOL_P0_HEAP_TRACE','DLSS5TOOL_P0_DISABLE_EXTENSIONS',
         'DLSS5TOOL_P0_THREAD_ACTIVITY',
         'DLSS5TOOL_P0_COMPOSER_HOST','DLSS5TOOL_DISABLE_NATIVE_COMPOSER')})


class Memory:
    class Counters(C.Structure):
        _fields_ = [('cb', C.c_uint32), ('faults', C.c_uint32)] + [
            (key, C.c_size_t) for key in ('peak_working_set', 'working_set',
            'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]

    def __init__(self, processes):
        self.processes = processes
        self.psapi = C.WinDLL('psapi', use_last_error=True)
        self.psapi.GetProcessMemoryInfo.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32]
        self.psapi.GetProcessMemoryInfo.restype = C.c_int
        self.processes.k.GetProcessHandleCount.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        self.processes.k.GetProcessHandleCount.restype = C.c_int

    def read(self, row):
        handle = self.processes.k.OpenProcess(0x1000 | 0x10, False, row['pid'])
        if not handle:
            return dict(row, memory_query_error=C.get_last_error())
        try:
            counters = self.Counters()
            counters.cb = C.sizeof(counters)
            if not self.psapi.GetProcessMemoryInfo(handle, C.byref(counters), counters.cb):
                return dict(row, memory_query_error=C.get_last_error())
            count = C.c_uint32()
            self.processes.k.GetProcessHandleCount(handle, C.byref(count))
            return dict(row, private_bytes=counters.private,
                        working_set_bytes=counters.working_set, handles=count.value)
        finally:
            self.processes.k.CloseHandle(handle)


def settings(scale):
    from dlss5tool import app_settings
    return dict(app_settings.DEFAULTS, guidance_mode=0, host_backend='v2',
        host_auto_fallback=False, host_in_flight=1, dlss_runtime='__bundled__',
        intensity=.5, local_tone=.5, local_struct=.5, skin_struct=.5,
        super_resolution_scale=scale, preview_super_resolution=True,
        frame_generation_multiplier=1, preview_cache_mb=256,
        output_view=0, output_mix=1, nr_quality={'total': .8},
        use_nvenc=True, nvenc_preset='p5', quality_profile='high',
        rate_control='quality', video_bitrate_mbps=12, hdr_mode=True)


def make_source(work, frames, hdr):
    from dlss5tool.video_export import find_ffmpeg
    source = work/'source.mp4'
    codec = (['-c:v', 'libx265', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
              '-x265-params', 'pools=2:frame-threads=1:log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
              '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc',
              '-color_trc', 'smpte2084'] if hdr else
             ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p'])
    subprocess.run([find_ffmpeg(), '-v', 'error', '-n', '-f', 'lavfi', '-i',
        'testsrc2=size=960x540:rate=30', '-frames:v', str(frames), '-threads', '2',
        *codec, str(source)], check=True, timeout=90, creationflags=NO_WINDOW)
    return source


def worker(args, work):
    os.environ.update(DLSS5TOOL_SETTINGS_PATH=str(work/'settings.json'),
        DLSS5TOOL_QUEUE_PATH=str(work/'queue.json'), DLSS5TOOL_DISABLE_UPDATE_CHECK='1',
        DLSS5TOOL_FG_LOG_ROOT=str(work/'fg-logs'), TEMP=str(work), TMP=str(work),
        PYTHONDONTWRITEBYTECODE='1')
    from dlss5tool import paths
    state = work/'state'
    state.mkdir(exist_ok=True)
    paths.state_path = lambda filename: state/filename
    from dlss5tool import app_settings
    from dlss5tool import frame_generation as fg
    from dlss5tool.residual_composition import release_native_composer
    from dlss5tool.video_export import probe_video_stream, find_ffmpeg
    config = settings(args.scale)
    config['frame_generation_multiplier'] = args.multiplier
    app_settings.save(config)
    events_lock = threading.Lock()
    cancel = threading.Event()

    def event(kind, **data):
        with events_lock:
            with (work/'events.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps(dict(event=kind, wall=time.time(), **data), ensure_ascii=False) + '\n')

    def progress(text, fraction, counts=None):
        if (work/'cancel.flag').exists():
            cancel.set()
        match = re.search(r'处理 (\d+)/(\d+) 源帧', text)
        if match:
            number, total = map(int, match.groups())
            if number == 1 or number % 16 == 0 or number == total:
                event('frame', frame=number, total=total)
            if args.case == 'cancel' and number >= 16 and not result.get('cancel_verified'):
                cancel.set()

    result = {'case': args.case, 'scale': args.scale, 'frames': args.frames,
              'multiplier': args.multiplier, 'enhance': args.enhance,
              'extension_point_policy': EXTENSION_POLICY}
    candidate = os.environ.get('DLSS5TOOL_P0_COMPOSER_HOST')
    if candidate:
        candidate = checked_path(candidate)
        if (args.case not in ('images','native-composer') or not candidate.is_relative_to(args.task)
                or not candidate.is_file() or candidate.suffix.lower() != '.dll'):
            raise ValueError('Composer override requires images/native-composer and a task-local DLL')
        from dlss5tool import residual_composition
        residual_composition._native_library_path = lambda: str(candidate)
    manager = None
    try:
        event('starting', pid=os.getpid())
        if args.case.startswith('native-'):
            from scripts.profile_native_frame_memory import run
            result.update(run(args, work, config, event))
        elif args.case in ('images', 'exit'):
            result.update(gui_images(args, work, config, event))
        else:
            source = make_source(work, args.frames, args.hdr)
            source_metadata = probe_video_stream(find_ffmpeg(), source)
            assert source_metadata['is_hdr'] == args.hdr, source_metadata
            result['source_metadata'] = source_metadata
            event('source-ready')
            output = work/'output.mp4'
            if args.case == 'cached':
                from dlss5tool.render_cache import RenderCache, encode_cached
                manager = RenderCache(128*1024**2)
                session = manager.session(source, config)
                report = encode_cached(session, output, config, cancel, progress)
                event('cache-before-close', spatial=manager.spatial.snapshot(), base=manager.base.snapshot())
                manager.close()
                assert not manager.sessions and manager.base is manager.spatial is None
            else:
                if args.case == 'cancel':
                    try:
                        fg.export_video(source, output, multiplier=args.multiplier, scale=args.scale,
                            enhance=args.enhance, settings=config, cancel=cancel, progress=progress,
                            log_dir=work/'cancel-logs')
                    except fg.Cancelled:
                        assert not output.exists()
                        result['cancel_verified'] = True
                        event('cancelled')
                    else:
                        raise AssertionError('Cancellation did not interrupt actual processing')
                    cancel.clear()
                report = fg.export_video(source, output, multiplier=args.multiplier, scale=args.scale,
                    enhance=args.enhance, settings=config, cancel=cancel, progress=progress,
                    log_dir=work/'complete-logs')
            actual = probe_video_stream(find_ffmpeg(), output)
            assert not report.get('cleanup_errors'), report.get('cleanup_errors')
            assert report['real_frames'] == args.frames and actual['frames'] == args.frames*args.multiplier
            assert (actual['width'], actual['height']) == (960*args.scale, 540*args.scale)
            assert actual['is_hdr'] == args.hdr, actual
            if args.hdr:
                assert report['color'] == actual['profile'] == 'hdr10_pq'
            result.update(report=report, output_metadata=actual)
        result['status'] = 'passed'
        event('closed')
    except BaseException:
        result.update(status='failed', error=traceback.format_exc())
        raise
    finally:
        try:
            if manager is not None:
                manager.close()
        finally:
            release_native_composer()
            gc.collect()
            save(work/'worker-result.json', result)


def gui_images(args, work, config, event):
    import numpy as np
    import cv2
    from tkinterdnd2 import TkinterDnD
    from dlss5tool import gui
    if args.trace_gui:
        import tracemalloc
        tracemalloc.start(3)
    files = []
    for index, (width, height) in enumerate([(640, 360), (960, 540), (800, 448)]):
        pixels = np.zeros((height, width, 3), np.uint8)
        pixels[..., 0] = np.arange(width, dtype=np.uint16)[None, :] % 256
        pixels[..., 1] = np.arange(height, dtype=np.uint16)[:, None] % 256
        cv2.rectangle(pixels, (width//4, height//4), (width//2, height//2), (80, 180, 240), -1)
        path = work/f'image-{index}.png'
        ok, encoded = cv2.imencode('.png', pixels)
        assert ok
        encoded.tofile(path)
        files.append(path)
    root = TkinterDnD.Tk()
    root.withdraw()
    errors, closed = [], []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    app = gui.App(root)
    destroy = root.destroy
    def destroy_root():
        closed.append(True)
        destroy()
    root.destroy = destroy_root

    def wait(predicate, timeout=120):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            root.update()
            if errors:
                raise RuntimeError(str(errors))
            if (work/'cancel.flag').exists():
                raise RuntimeError('Supervisor stopped the workload')
            if predicate():
                return
            time.sleep(.01)
        raise TimeoutError(str(getattr(app, '_live_error', 'GUI wait timed out')))

    cycles = []
    from scripts.profile_thread_activity import load_counter, read_counter
    thread_counter = load_counter()
    gui_inspector = None
    if args.trace_gui and os.environ.get('DLSS5TOOL_P0_MEMORY_MAP') == '1':
        from scripts.inspect_native_allocations import Inspector
        gui_inspector = Inspector()
    try:
        for index in range(args.cycles if args.case == 'images' else 1):
            source = files[index % len(files)]
            assert app._load_image(str(source))
            app.view_var.set('dlss')
            app.on_view_change()
            wait(lambda: app._cached_dlss(0) is not None and not app._play_dlss_busy)
            if args.case == 'images':
                destination = work/f'export-{index}.png'
                report = app._export_image_source(str(source), config, out_path=str(destination), notify=False)
                assert report['success'], report
                actual = cv2.imread(str(destination))
                original = cv2.imread(str(source))
                assert actual.shape[:2] == (original.shape[0]*args.scale, original.shape[1]*args.scale)
            else:
                app._cache_clear()
                app.display_view(quality='precise')
            state = dict(cycle=index, source=source.name,
                host_pid=app._live._session._process.pid if app._live else None,
                sr_pid=app._super_resolution_live._process.pid if app._super_resolution_live else None)
            if args.trace_gui:
                from scripts.diagnose_composer_lifetime import memory_snapshot
                from scripts.profile_native_frame_memory import loaded_modules
                modules = loaded_modules()
                with (work/'gui-modules.jsonl').open('a', encoding='utf-8') as module_log:
                    module_log.write(json.dumps(dict(cycle=index, wall=time.time(), modules=modules))+'\n')
                collected = gc.collect()
                current, peak = tracemalloc.get_traced_memory()
                state.update(memory=memory_snapshot(), python_bytes=current,
                    python_peak_bytes=peak, collected=collected,
                    tk_images=len(root.tk.call('image', 'names')),
                    overlay_modules=[path for path in modules if 'nvspcap' in path.lower()],
                    source_cache_bytes=app._source_cache_bytes, dlss_cache_bytes=app._dlss_cache_bytes,
                    python_sites=[dict(site=str(s.traceback), bytes=s.size, count=s.count)
                        for s in tracemalloc.take_snapshot().statistics('lineno')[:10]])
                state['thread_activity'] = read_counter(thread_counter)
                if gui_inspector is not None:
                    metadata = gui_inspector.snapshot()
                    state['native_heap'] = metadata['default_heap']
                    with (work/'gui-frame-memory-regions.jsonl').open('a', encoding='utf-8') as out:
                        out.write(json.dumps(dict(metadata,cycle=index))+'\n')
                import ctypes as C
                kernel = C.WinDLL('kernel32')
                kernel.GetModuleHandleW.argtypes = [C.c_wchar_p]
                kernel.GetModuleHandleW.restype = C.c_void_p
                module = kernel.GetModuleHandleW('dlss5_quality.dll')
                if module:
                    library = C.CDLL('dlss5_quality.dll',handle=module)
                    library.dlss5_quality_lifetime.argtypes = [C.POINTER(C.c_uint64)]
                    counts = (C.c_uint64*3)()
                    library.dlss5_quality_lifetime(counts)
                    state['composer_lifetime'] = dict(created=counts[0],destroyed=counts[1],retained_device_refs=counts[2])
            cycles.append(state)
            event('image-complete', **state)
            if index == args.gui_idle_cycle and args.idle_seconds:
                from scripts.profile_native_frame_memory import start_heap_trace
                trace, patches = start_heap_trace(work, event)
                inspector = None
                if os.environ.get('DLSS5TOOL_P0_MEMORY_MAP') == '1':
                    from scripts.inspect_native_allocations import Inspector
                    inspector = Inspector()
                def observe_idle(number):
                    from scripts.diagnose_composer_lifetime import memory_snapshot
                    event('gui-idle-sample', memory=memory_snapshot(), thread_activity=read_counter(thread_counter))
                    if inspector is not None:
                        with (work/'gui-frame-memory-regions.jsonl').open('a', encoding='utf-8') as out:
                            out.write(json.dumps(inspector.snapshot())+'\n')
                    if trace is not None:
                        assert trace.trace_dump(str(work/f'native-heap-{number}.jsonl')) == 0
                event('idle-start', seconds=args.idle_seconds, scope='GUI preview idle')
                deadline = time.monotonic()+args.idle_seconds
                checkpoint = time.monotonic()+20
                number = 0
                try:
                    observe_idle(number)
                    while time.monotonic() < deadline:
                        root.update()
                        if (work/'cancel.flag').exists():
                            raise RuntimeError('Supervisor stopped GUI idle test')
                        if time.monotonic() >= checkpoint:
                            number += 1
                            observe_idle(number)
                            checkpoint += 20
                        time.sleep(.05)
                    observe_idle('final')
                finally:
                    if trace is not None:
                        restored = trace.trace_end()
                        event('heap-trace-ended', restored=restored, patches=patches)
                        assert restored == patches
                event('idle-end', scope='GUI preview idle')
            until = time.monotonic()+.5
            while time.monotonic() < until:
                root.update()
                time.sleep(.01)
        if args.case == 'images':
            app.pause()
            event('paused')
            finished = app._finish_clear_preview_cache
            def observe_clear():
                finished()
                event('cache-cleared', bytes=app._dlss_cache_bytes+app._source_cache_bytes)
                assert app._dlss_cache_bytes == app._source_cache_bytes == 0
            app._finish_clear_preview_cache = observe_clear
            app.clear_preview_cache()
            wait(lambda: not app._clear_preview_pending)
        event('exit-requested')
        app._on_close()
        if not closed:
            wait(lambda: bool(closed))
        assert not errors, errors
        return dict(cycles=cycles, tk_errors=errors, window_destroyed=bool(closed))
    finally:
        if not closed:
            app._export_cancel_event.set()
            app._exporting = app._queue_running = False
            app._on_close()


def supervise(args, work):
    from scripts.ngx_updater_lifecycle_probe import Processes
    from dlss5tool.memory_budget import system_memory
    processes = Processes()
    memory = Memory(processes)
    initial = processes.snapshot()
    existed = {row['pid'] for row in initial}
    known, observations, samples, held = {}, {}, [], {}
    gpu_snapshots = []
    start, next_print = time.monotonic(), 0
    before_manifest = run_manifest(args.task)
    save(work/'run-manifest-before.json',before_manifest)
    safety_error, exit_seen = None, None
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--task', str(args.task),
        '--label', args.label, '--case', args.case, '--frames', str(args.frames),
        '--cycles', str(args.cycles), '--scale', str(args.scale), '--native-width', str(args.native_width),
        '--gui-idle-cycle', str(args.gui_idle_cycle),
        '--multiplier', str(args.multiplier),
        '--timeout', str(args.timeout), '--idle-seconds', str(args.idle_seconds),
        '--run-seconds', str(args.run_seconds), '--worker']
    if args.hdr:
        command.append('--hdr')
    if args.composer_resize:
        command.append('--composer-resize')
    if not args.enhance:
        command.append('--no-enhance')
    environment = dict(os.environ)
    if args.disable_extensions:
        command.append('--disable-extensions')
        environment['DLSS5TOOL_P0_DISABLE_EXTENSIONS'] = '1'
    if args.simulate_pressure:
        command.append('--simulate-pressure')
        environment['DLSS5TOOL_P0_INJECT_MEMORY_PRESSURE'] = '1'
    if args.trim_heaps:
        command.append('--trim-heaps')
        environment['DLSS5TOOL_P0_TRIM_HEAPS'] = '1'
    if args.vsr_host:
        command.extend(['--vsr-host', str(args.vsr_host)])
        environment['DLSS5TOOL_P0_VSR_HOST'] = str(args.vsr_host)
    if args.trace_gui:
        command.append('--trace-gui')
    if args.device_work:
        command.append('--device-work')
    if args.device_compute:
        command.append('--device-compute')
    if args.device_readback:
        command.append('--device-readback')
    if args.device_timing:
        command.append('--device-timing')
    if args.device_cuda:
        command.append('--device-cuda')
    with (work/'console.log').open('wb') as console:
        child = subprocess.Popen(command, stdout=console, stderr=subprocess.STDOUT,
            cwd=ROOT, env=environment, creationflags=NO_WINDOW)
        try:
            while True:
                now = time.monotonic()
                rows = processes.snapshot()
                descendants = {child.pid}
                parents = descendants | set(known)
                for _ in range(8):
                    added = {row['pid'] for row in rows if row['parent'] in parents and row['pid'] not in existed}
                    if added <= parents:
                        break
                    parents |= added
                    descendants |= added
                current = []
                for row in rows:
                    if row['pid'] not in descendants and row['pid'] not in known:
                        continue
                    identity = processes.identity(row)
                    if identity.get('created') is None:
                        continue  # A process may exit between the two snapshots.
                    old = known.get(row['pid'])
                    if old is not None and old['created'] != identity['created']:
                        continue  # Never attach a recycled PID to the workload.
                    if row['pid'] not in held:
                        handle = processes.k.OpenProcess(0x1000 | 0x100000, False, row['pid'])
                        if not handle:
                            continue
                        # Holding the process object prevents its PID being
                        # recycled into another family while observing exits.
                        held[row['pid']] = handle
                    known[row['pid']] = identity
                    value = memory.read(identity)
                    current.append(value)
                    observations[row['pid']] = value
                status = system_memory()
                private = sum(row.get('private_bytes', 0) for row in current)
                record = dict(seconds=round(now-start, 3), wall=time.time(), private_bytes=private,
                    working_set_bytes=sum(row.get('working_set_bytes', 0) for row in current),
                    handles=sum(row.get('handles', 0) for row in current),
                    processes=current, system=status)
                samples.append(record)
                if now-start >= next_print:
                    print(json.dumps(dict(case=args.case, elapsed=round(now-start), processes=len(current),
                        private_mib=round(private/1024**2, 1))), flush=True)
                    save(work/'memory-samples.json', samples)
                    if args.gpu_snapshots:
                        try:
                            query = subprocess.run(['nvidia-smi',
                                '--query-gpu=uuid,memory.used,memory.free,utilization.gpu',
                                '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                check=True, timeout=8, creationflags=NO_WINDOW)
                            devices = []
                            for line in query.stdout.splitlines():
                                uuid, used, free, utilization = [v.strip() for v in line.split(',')]
                                devices.append(dict(uuid=uuid, used_mib=float(used),
                                    free_mib=float(free), utilization_percent=float(utilization)))
                            gpu_snapshots.append(dict(wall=time.time(), devices=devices,
                                scope='system GPU snapshot, not owned-process attribution'))
                        except Exception as query_error:
                            gpu_snapshots.append(dict(wall=time.time(), error=str(query_error)))
                    next_print += 20
                if safety_error is None and (private > 8*GIB or now-start > args.timeout or
                        status and min(status['free_physical_bytes'], status['free_commit_bytes']) < 8*GIB or
                        now-start > 3 and child.poll() is None and not any(
                            row['pid'] == child.pid and 'private_bytes' in row for row in current)):
                    safety_error = 'Workload memory/headroom or elapsed-time limit reached'
                    (work/'cancel.flag').write_text(safety_error, encoding='utf-8')
                    stop_deadline = now+10
                if child.poll() is not None:
                    exit_seen = exit_seen or now
                    if now-exit_seen >= 8 or not current:
                        break
                if safety_error and now >= stop_deadline:
                    break
                time.sleep(.25)
        finally:
            # Recovery targets are exact observed descendants with creation-time
            # verification. Recovering a leftover process makes the gate fail.
            remaining = []
            for row in processes.snapshot():
                previous = known.get(row['pid'])
                if previous and processes.identity(row).get('created') == previous.get('created'):
                    remaining.append(previous)
            cleanup = [processes.terminate_owned(row) for row in reversed(remaining)]
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
            save(work/'memory-samples.json', samples)
            passed = child.returncode == 0 and not safety_error and not remaining
            result = dict(status='passed' if passed else 'failed', case=args.case,
                run_arguments=vars(args),
                processing_and_exit_passed=passed, long_term_memory_plateau_verified=False,
                elapsed_seconds=round(time.monotonic()-start, 3), child_exit_code=child.returncode,
                safety_error=safety_error, peak_tree_private_bytes=max((s['private_bytes'] for s in samples), default=0),
                peak_tree_working_set_bytes=max((s['working_set_bytes'] for s in samples), default=0),
                observed_processes=list(observations.values()), gpu_snapshots=gpu_snapshots,
                gpu_snapshots_enabled=args.gpu_snapshots,
                remaining_before_recovery=remaining,
                recovery=cleanup, samples=len(samples), scope='synthetic media; actual existing runtimes and production processing')
            events = ([json.loads(line) for line in (work/'events.jsonl').read_text(encoding='utf-8').splitlines()]
                      if (work/'events.jsonl').exists() else [])
            frame_events = [event for event in events if event.get('event') == 'frame']
            warm = frame_events[0]['wall']+20 if frame_events else float('inf')
            end = frame_events[-1]['wall'] if frame_events else 0
            steady = [s['private_bytes'] for s in samples if args.case == 'continuous' and warm <= s['wall'] <= end]
            if len(steady) >= 20:
                result['steady_sample_count'] = len(steady)
                count = max(1, len(steady)//4)
                result['post_warmup_private_delta_bytes'] = statistics.median(steady[-count:])-statistics.median(steady[:count])
                result['post_warmup_private_range_bytes'] = max(steady)-min(steady)
            idle_start = next((e['wall']+10 for e in events if e['event'] == 'idle-start'), None)
            idle_end = next((e['wall'] for e in events if e['event'] == 'idle-end'), None)
            if idle_start is not None and idle_end is not None:
                idle = [s for s in samples if idle_start <= s['wall'] <= idle_end]
                if len(idle) >= 20:
                    count = max(1, len(idle)//5)
                    delta = (statistics.median(s['private_bytes'] for s in idle[-count:])-
                             statistics.median(s['private_bytes'] for s in idle[:count]))
                    span = (statistics.median(s['wall'] for s in idle[-count:])-
                            statistics.median(s['wall'] for s in idle[:count]))
                    result['idle_observation'] = dict(samples=len(idle), private_delta_bytes=delta,
                        measured_span_seconds=span, private_bytes_per_second=delta/max(span,.001))
            save(work/'result.json', result)
            after_manifest = run_manifest(args.task)
            save(work/'run-manifest-after.json',after_manifest)
            save(work/'source-comparison.json',dict(unchanged=before_manifest == after_manifest))
            for handle in held.values():
                processes.k.CloseHandle(handle)
            print(json.dumps({key:result[key] for key in ('status','case','elapsed_seconds',
                'peak_tree_private_bytes','remaining_before_recovery','safety_error')}, ensure_ascii=True), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--case', choices=('continuous','cached','images','cancel','exit',
                        'native-sr','native-nr','native-composer','native-device',
                        'native-device11','native-cuda','native-idle','native-adapters'), required=True)
    parser.add_argument('--label', default='r1')
    parser.add_argument('--frames', type=int, default=600)
    parser.add_argument('--cycles', type=int, default=12)
    parser.add_argument('--scale', type=int, choices=(1,2), default=2)
    parser.add_argument('--multiplier', type=int, choices=(1,2), default=1)
    parser.add_argument('--enhance', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--native-width', type=int, choices=(320,960), default=320)
    parser.add_argument('--idle-seconds', type=int, default=0)
    parser.add_argument('--run-seconds', type=int, default=0,
                        help='Bounded native continuous-processing window, up to 1800 seconds')
    parser.add_argument('--hdr', action='store_true')
    parser.add_argument('--composer-resize', action='store_true',
                        help='Diagnostic-only alternate 1080p/896p on one stateless native composer')
    parser.add_argument('--disable-extensions', action='store_true',
                        help='Diagnostic-only per-process legacy extension policy')
    parser.add_argument('--simulate-pressure', action='store_true',
                        help='Test-only fake low-memory snapshot after one native frame; never allocates pressure')
    parser.add_argument('--trim-heaps', action='store_true', help='Diagnostic-only LFH cache decommit')
    parser.add_argument('--vsr-host', type=Path, help='Diagnostic-only task-local candidate VSR DLL')
    parser.add_argument('--trace-gui', action='store_true', help='Diagnostic-only GUI heap/Tk image counters')
    parser.add_argument('--gpu-snapshots', action=argparse.BooleanOptionalAction, default=True,
                        help='Read optional whole-GPU nvidia-smi snapshots; disable for low-interference controls')
    parser.add_argument('--gui-idle-cycle', type=int, default=0,
                        help='Zero-based image cycle for the diagnostic idle observation')
    parser.add_argument('--device-work', action='store_true', help='One 32x32 D3D11 clear, without project native DLLs')
    parser.add_argument('--device-compute', action='store_true', help='One empty D3D11 compute dispatch, without project native DLLs')
    parser.add_argument('--device-readback', action='store_true', help='One 1080p staging Copy/Map/Unmap; requires --device-work')
    parser.add_argument('--device-timing', action='store_true', help='One D3D11 timestamp/disjoint query sequence; no project native DLL')
    parser.add_argument('--device-cuda', action='store_true', help='Initialize CUDA before the minimal D3D device in the same process')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.composer_resize and args.case != 'native-composer':
        parser.error('Composer resize control requires native-composer')
    if args.run_seconds and (args.case not in ('native-sr','native-nr','native-composer') or
                            args.idle_seconds or not 1 <= args.run_seconds <= 1800):
        parser.error('Continuous native window requires SR/NR/composer, 1..1800 seconds, no idle window')
    if not 0 <= args.gui_idle_cycle < args.cycles or args.gui_idle_cycle and args.case != 'images':
        parser.error('GUI idle cycle must exist within an images run')
    if args.device_work and args.case != 'native-device11':
        parser.error('Minimal device work is limited to native-device11')
    if args.device_compute and (args.case != 'native-device11' or args.device_work):
        parser.error('Compute control requires native-device11 without --device-work')
    if args.device_readback and not args.device_work:
        parser.error('Readback control requires --device-work')
    if args.device_timing and args.case != 'native-device11':
        parser.error('Timestamp control requires native-device11')
    if args.device_cuda and args.case not in ('native-device', 'native-device11'):
        parser.error('Combined CUDA/D3D control requires native-device or native-device11')
    if args.simulate_pressure and args.case not in ('native-sr', 'native-nr'):
        parser.error('Simulated pressure is limited to native SR/NR diagnostics')
    if (args.multiplier != 1 or not args.enhance) and args.case != 'continuous':
        parser.error('FG and pure SR comparison options are limited to continuous diagnostics')
    if args.trim_heaps and args.case not in ('native-sr', 'native-nr'):
        parser.error('Heap trimming is limited to native SR/NR diagnostics')
    args.task = checked_path(args.task)
    if args.vsr_host:
        args.vsr_host = checked_path(args.vsr_host)
        if (args.case != 'native-sr' or not args.vsr_host.is_relative_to(args.task) or
                args.vsr_host.suffix.lower() != '.dll' or not args.vsr_host.is_file()):
            parser.error('Candidate DLL must be a regular file inside this task; native-sr only')
    if not args.task.is_relative_to(ROOT/'tmp') or not (args.task/'TASK.md').is_file():
        parser.error('Use a registered task under this repository tmp/')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', args.label):
        parser.error('Label must be a simple file name')
    if (not 32 <= args.frames <= 3600 or not 1 <= args.cycles <= 32 or
            not 30 <= args.timeout <= 2100 or not 0 <= args.idle_seconds <= 600):
        parser.error('Bounds: frames 32..3600; cycles 1..32; timeout 30..1800; idle 0..600 seconds')
    work = checked_path(args.task/(args.case+'-'+args.label))
    if args.worker:
        worker(args, work)
        return 0
    if shutil.disk_usage(args.task).free < 15*GIB+512*1024**2:
        parser.error('Insufficient disk headroom')
    work.mkdir(exist_ok=False)
    return supervise(args, work)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
