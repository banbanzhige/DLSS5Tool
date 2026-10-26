"""Bounded real SR/cache/encoder comparison using the same path as GUI export.

Register --task/TASK.md first. Uses existing runtime and synthetic SDR frames;
wall timings include transfer/wait/IPC and are not GPU kernel timings.
"""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import ctypes as C
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def alive(process):
    try:
        return process.is_alive() if hasattr(process, 'is_alive') else process.poll() is None
    except ValueError:  # Production closes the handle only after joining.
        return False


def private_bytes(pid):
    class Counters(C.Structure):
        _fields_ = [('cb', C.c_ulong), ('faults', C.c_ulong)] + [
            (name, C.c_size_t) for name in ('peak_working', 'working', 'peak_paged',
                'paged', 'peak_nonpaged', 'nonpaged', 'commit', 'peak_commit', 'private')]
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = C.c_void_p
    kernel.CloseHandle.argtypes = [C.c_void_p]
    query = C.WinDLL('psapi').GetProcessMemoryInfo
    query.argtypes = [C.c_void_p, C.POINTER(Counters), C.c_ulong]
    handle = kernel.OpenProcess(0x410, False, int(pid))
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = C.sizeof(counters)
        return int(counters.private) if query(handle, C.byref(counters), counters.cb) else None
    finally:
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--case', choices=('sr', 'sr-nr'), default='sr')
    parser.add_argument('--frames', type=int, default=48)
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=540)
    parser.add_argument('--scale', type=int, choices=(2, 4), default=2)
    parser.add_argument('--multiplier', type=int, choices=(1, 2), default=1)
    parser.add_argument('--color-profile', choices=('srgb', 'hdr10_pq', 'hdr10_hlg'), default='srgb')
    parser.add_argument('--hard-cut', action='store_true', help='Black/white cut halfway through the synthetic cache source')
    parser.add_argument('--source', type=Path, help='Existing registered-task media with matching geometry/frame count')
    parser.add_argument('--quality', choices=('high','balanced'), default='high')
    parser.add_argument('--baseline', action='store_true', help='Read core modules from Git HEAD in this process only')
    parser.add_argument('--baseline-ref', help='Freeze core modules at this local Git commit/ref')
    parser.add_argument('--no-frame-hashes', action='store_true', help='Measure throughput without per-frame SHA256 audit overhead')
    args = parser.parse_args()
    task = args.task.absolute()
    if task.resolve() != task or not task.is_relative_to(ROOT/'tmp'):
        raise ValueError('Task must resolve inside repository tmp without links')
    for path in (task, *task.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Reparse point in task path')
    if not (task/'TASK.md').is_file() or not args.label.replace('-', '').isalnum():
        raise ValueError('Registered task and plain label required')
    if not 8 <= args.frames <= 256 or args.width*args.height*args.scale**2 > 3840*2160:
        raise ValueError('Probe bounded to 8..256 frames and <=4K output')
    os.environ['DLSS5TOOL_FG_LOG_ROOT'] = str(task/'native-logs')
    baseline_commit = None
    if args.baseline or args.baseline_ref:
        baseline_commit = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options',
            (args.baseline_ref or 'HEAD')+'^{commit}'], cwd=ROOT, check=True,
            capture_output=True, text=True).stdout.strip()
        import dlss5tool
        for name in ('frame_generation', 'render_cache'):
            destination = task/f'baseline-{baseline_commit[:12]}-{name}.py'
            data = subprocess.run(['git', 'show', f'{baseline_commit}:dlss5tool/{name}.py'], cwd=ROOT,
                                  check=True, capture_output=True).stdout
            if destination.exists() and destination.read_bytes() != data:
                raise ValueError('Baseline changed')
            destination.write_bytes(data)
            spec = importlib.util.spec_from_file_location('dlss5tool.'+name, destination)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            setattr(dlss5tool, name, module)
    import numpy as np
    from dlss5tool import app_settings, dlss_engine, render_cache, super_resolution, video_export
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.memory_budget import system_memory, memory_scope
    from dlss5tool.residual_composition import release_native_composer
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.super_resolution import ProcessSuperResolution
    from dlss5tool.frame_generation import NativeStream

    prefix = task/f'{args.label}-{args.case}'
    output = prefix.with_suffix('.mp4')
    report_path = prefix.with_suffix('.json')
    if output.exists() or report_path.exists():
        raise ValueError('Refusing overwrite')
    hdr = args.color_profile != 'srgb'
    suffix = f'-{args.color_profile}-vui' if hdr else ''
    if args.hard_cut:
        suffix += '-cut'
    source = args.source.absolute() if args.source else task/f'synthetic-{args.width}x{args.height}-{args.frames}{suffix}.mp4'
    if args.source and (not source.is_relative_to(task) or source.resolve()!=source or not source.is_file()):
        raise ValueError('Custom source must be an existing file within this registered task')
    if not source.exists():
        transfer = 'smpte2084' if args.color_profile == 'hdr10_pq' else 'arib-std-b67'
        source_options = (['-c:v', 'libx265', '-preset', 'ultrafast', '-threads', '2',
            '-x265-params', 'pools=2:frame-threads=1:log-level=error:colorprim=bt2020:'
                f'transfer={transfer}:colormatrix=bt2020nc', '-pix_fmt', 'yuv420p10le',
            '-color_primaries', 'bt2020', '-colorspace', 'bt2020nc', '-color_range', 'tv',
            '-color_trc', transfer]
            if hdr else ['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p'])
        pattern = f'testsrc2=size={args.width}x{args.height}:rate=30'
        if args.hard_cut:
            half = args.frames//2
            pattern = (f'color=black:size={args.width}x{args.height}:rate=30,trim=end_frame={half},'
                f'setpts=PTS-STARTPTS[b];color=white:size={args.width}x{args.height}:rate=30,'
                f'trim=end_frame={args.frames-half},setpts=PTS-STARTPTS[w];[b][w]concat=n=2:v=1:a=0')
        subprocess.run([video_export.find_ffmpeg(), '-hide_banner', '-loglevel', 'error',
            '-n', '-f', 'lavfi', '-i', pattern,
            '-frames:v', str(args.frames), *source_options, str(source)], check=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    adapters = dlss_engine.available_render_adapters()
    if not adapters:
        raise RuntimeError('No NVIDIA render adapter')
    config = app_settings.validate(dict(super_resolution_scale=args.scale,
        frame_generation_multiplier=args.multiplier, guidance_mode=0, render_gpu=adapters[0]['id'],
        host_backend='v2', host_auto_fallback=False, host_in_flight=1,
        host_zero_fast_path=True, host_submission='merged', host_persistent_buffers=True,
        nr_quality={'total': 0.0 if args.case == 'sr' else 0.75}, output_mix=1.0,
        output_view=0, hdr_mode=hdr, nvenc_preset='p5', rate_control='quality',
        quality_profile=args.quality, video_bitrate_mbps=20))
    source_info = video_export.probe_video_stream(video_export.find_ffmpeg(), source)
    if source_info['is_hdr'] != hdr or (hdr and source_info['profile'] != args.color_profile):
        raise ValueError('Synthetic source color tags do not match the requested profile')
    cancel = threading.Event()
    lock = threading.Lock()
    times, hashes, completions, components, samples = defaultdict(list), [], [], [], []
    nr_hashes, fg_events = [], []
    registered_pids = {os.getpid()}
    replay_requests = []
    last_sample = 0.0
    def sample():
        nonlocal last_sample
        now = time.perf_counter()
        if now-last_sample < 1:
            return
        last_sample = now
        pids = {os.getpid(), *(p.pid for p in components if alive(p))}
        usage = {str(pid): private_bytes(pid) for pid in pids}
        memory = system_memory()
        samples.append(dict(seconds=now-started, private_bytes=usage, system=memory))
        if sum(v or 0 for v in usage.values()) > 4*1024**3 or (memory and
                min(memory['free_physical_bytes'], memory['free_commit_bytes']) < 4*1024**3):
            cancel.set()
            raise RuntimeError('Probe memory safety stop')
    def wrap(name, function):
        def call(*a, **kw):
            start = time.perf_counter()
            try:
                result = function(*a, **kw)
                if name == 'nr' and result is not None and not args.no_frame_hashes:
                    nr_hashes.append({'input': hashlib.sha256(memoryview(a[1])).hexdigest(),
                        'output': hashlib.sha256(memoryview(result)).hexdigest(),
                        'reset': kw.get('reset')})
                if name == 'fg' and not args.no_frame_hashes:
                    frames, valid = result
                    fg_events.append(dict(reset=bool(kw.get('reset', a[3] if len(a) > 3 else False)), valid=bool(valid),
                        input=hashlib.sha256(memoryview(a[1])).hexdigest(),
                        outputs=[hashlib.sha256(memoryview(frame)).hexdigest() for frame in frames]))
                return result
            finally:
                with lock:
                    times[name].append((time.perf_counter()-start)*1000)
        return call
    def initialized(function):
        def call(instance, *a, **kw):
            result = function(instance, *a, **kw)
            process = getattr(instance, '_process', None)
            session = getattr(instance, '_session', None)
            process = process or getattr(session, '_process', None)
            process = process or getattr(instance, 'proc', None)
            if process is not None:
                components.append(process)
                registered_pids.add(process.pid)
            return result
        return call
    original_write = video_export.FFmpegVideoWriter.write
    original_request = render_cache.RenderSession.request
    def request(instance, index):
        with instance.condition:
            previous_target, previous_produced = instance.target, instance.produced
            before, restarting = tuple(instance.cache), instance.restart
            result = original_request(instance, index)
            if instance.restart and not restarting and len(replay_requests) < 16:
                replay_requests.append(dict(seconds=time.perf_counter()-started,
                    stage=instance.settings.get('_render_stage'), index=index,
                    previous_target=previous_target, previous_produced=previous_produced,
                    before=list(before), after=list(instance.cache), allowance=instance._allowance(),
                    pool=instance.pool.snapshot() if instance.pool else None))
            return result
    def write(writer, frame):
        encoder = getattr(writer, '_process', None) or getattr(writer, '_proc', None) or getattr(writer, 'proc', None)
        if encoder is not None and getattr(encoder, 'pid', None) not in registered_pids:
            components.append(encoder)
            registered_pids.add(encoder.pid)
        sample()
        if not args.no_frame_hashes:
            hashes.append(hashlib.sha256(memoryview(np.ascontiguousarray(frame))).hexdigest())
        result = original_write(writer, frame)
        completions.append(time.perf_counter())
        return result
    watchdog = threading.Timer(120, cancel.set)
    watchdog.daemon = True
    pool = SharedCacheBudget(limit_bytes=1024**3)
    manager = render_cache.RenderCache(1024**3, pool_name=pool.name)
    started = time.perf_counter()
    watchdog.start()
    report = dict(case=args.case, settings=config, adapters=adapters, color_profile=args.color_profile,
        source=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_pattern='hard-cut' if args.hard_cut else 'testsrc2',
        scope=('GUI shared cache + encode_cached; ' +
               ('no per-frame hashes' if args.no_frame_hashes else 'frame hashes add probe overhead')))
    try:
        with ExitStack() as stack:
            stack.enter_context(memory_scope(pool))
            stack.enter_context(mock.patch.object(render_cache.RenderSession, 'request', request))
            for cls, method, name in ((ProcessSuperResolution, 'process', 'vsr'),
                    (ProcessLive, 'process', 'nr'),
                    (NativeStream, 'process', 'fg'),
                    (render_cache.RenderSession, 'wait', 'cache_wait'),
                    (video_export.FFmpegVideoWriter, 'write', 'encode_write')):
                function = write if name == 'encode_write' else getattr(cls, method)
                stack.enter_context(mock.patch.object(cls, method, wrap(name, function)))
            for cls in (ProcessSuperResolution, ProcessLive, NativeStream):
                stack.enter_context(mock.patch.object(cls, '__init__', initialized(cls.__init__)))
            session = manager.session(source, config)
            report['result'] = render_cache.encode_cached(session, output, config, cancel, lambda *_: None)
            report['cache'] = {name: getattr(manager, name).snapshot()
                for name in ('spatial', 'base') if getattr(manager, name) is not None}
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        watchdog.cancel()
        manager.close()
        report['memory_pool_after_close'] = pool.snapshot()
        pool.close()
        release_native_composer()
        report['elapsed_seconds'] = time.perf_counter()-started
        report['worker_processes_still_alive'] = [p.pid for p in components if alive(p)]
        report['registered_process_ids'] = sorted(registered_pids)
        report['replay_requests'] = replay_requests
        report['memory_samples'] = samples
        report['frame_hashes'] = hashes
        report['frame_hash_audit'] = not args.no_frame_hashes
        report['nr_hashes'] = nr_hashes
        report['fg_events'] = fg_events
        report['baseline_from_git_head'] = args.baseline
        report['baseline_commit'] = baseline_commit
        report['processing_memory_pool'] = pool.name
        report['steady_output_fps'] = ((len(completions)-5)/(completions[-1]-completions[4])
            if len(completions) > 5 else None)
        report['stages'] = {name: dict(calls=len(values), total_ms=sum(values),
            after_first_4_median_ms=statistics.median(values[4:] or values))
            for name, values in times.items()}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('status', 'elapsed_seconds', 'steady_output_fps',
        'stages', 'worker_processes_still_alive') if k in report}, ensure_ascii=False))
    return 0 if report['status'] == 'complete' and not report['worker_processes_still_alive'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
