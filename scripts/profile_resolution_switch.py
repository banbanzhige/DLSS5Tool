"""Bounded, synthetic comparison of process replacement and native NR resize.

Only this diagnostic's workers are instrumented. Product sources, DLLs, settings,
and input media are unchanged. Native resize runs inside the existing disposable
worker, which still exits without calling NGX shutdown after evaluation.
"""
import argparse
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
SIZES = [(2560, 1440), (2560, 1440), (2550, 1440),
         (2550, 1440), (2560, 1440), (2560, 1440)]


def event(phase, **data):
    work = Path(os.environ['DLSS5TOOL_RESOLUTION_WORK'])
    row = dict(phase=phase, pid=os.getpid(), wall=time.time(),
               clock=time.perf_counter(), **data)
    with (work/f'events-{os.getpid()}.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(row, ensure_ascii=False, default=str)+'\n')


def timed(phase, function):
    def call(*args, **kwargs):
        start = time.perf_counter()
        event(phase, edge='start')
        try:
            return function(*args, **kwargs)
        finally:
            event(phase, edge='finish', seconds=time.perf_counter()-start)
    return call


def frame(width, height):
    import numpy as np
    image = np.empty((height, width, 4), np.uint8)
    image[..., 0] = np.arange(width, dtype=np.uint16)[None, :] % 256
    image[..., 1] = np.arange(height, dtype=np.uint16)[:, None] % 256
    image[..., 2] = 96
    image[..., 3] = 255
    return image


def process_one(live, index, width, height):
    image = frame(width, height)
    start = time.perf_counter()
    output = live.process(image, reset=True)
    seconds = time.perf_counter()-start
    if output is None or output.shape != image.shape or output.dtype != image.dtype:
        raise RuntimeError('Unexpected output contract')
    event('image', index=index, width=width, height=height, seconds=seconds,
          sha256=hashlib.sha256(memoryview(output)).hexdigest())


def profile_worker(*args):
    start = time.perf_counter()
    from dlss5tool import dlss_host_process as host, dlss_engine as engine
    event('worker_import', seconds=time.perf_counter()-start)
    original_load = engine._load

    def load(*load_args, **load_kwargs):
        lib, backend = timed('host_dll_load', original_load)(*load_args, **load_kwargs)
        if not getattr(lib, '_resolution_profile_bound', False):
            for name, phase in (
                ('dlssnr_init', 'native_core_init'),
                ('dlssnr_create_feature', 'native_feature_create'),
                ('dlssnr_resize', 'native_feature_resize'),
                ('dlssnr_shutdown', 'native_shutdown'),
                ('dlssnr_process', 'native_process'),
            ):
                setattr(lib, name, timed(phase, getattr(lib, name)))
            lib._resolution_profile_bound = True
        return lib, backend

    engine._load = load
    engine._configure_render_adapter = timed('worker_adapter_discovery', engine._configure_render_adapter)
    original_live = engine.Live

    class MeasuredLive(original_live):
        def __init__(self, width, height, settings):
            timed('live_initialize', super().__init__)(width, height, settings)
            event('ready_metadata', backend=self.backend, adapter=self.adapter_info)
            if os.environ['DLSS5TOOL_RESOLUTION_CASE'] == 'native':
                previous = (width, height)
                for index, (w, h) in enumerate(SIZES*int(os.environ.get('DLSS5TOOL_RESOLUTION_CYCLES', '1'))):
                    if (w, h) != previous:
                        timed('in_process_resize', self.resize)(w, h)
                    process_one(self, index, w, h)
                    previous = (w, h)

    engine.Live = MeasuredLive
    return host._host_worker_main(*args)


def gpu_workload(work, settings):
    import numpy as np
    from dlss5tool.dlss_host_process import ProcessLive
    from dlss5tool.cuda_session import CudaSession
    from dlss5tool.cuda_frame import CudaImage, SharedCudaImage, FrameTiming
    from dlss5tool.cuda_interop import SharedCudaBuffer
    config = dict(settings, host_submission='merged', host_auto_fallback=False,
        _gpu_export_host=str(work.parent/'candidate/dlssnr_host_v2.dll'))
    live = cuda = input_mapping = output_mapping = output = None
    try:
        live = ProcessLive(640, 360, config)
        session = live._session
        pid = session._process.pid
        if not session.supports_gpu_reconfigure:
            raise RuntimeError('Candidate host lacks the GPU reconfiguration API')
        cuda = CudaSession(live.adapter_info['cuda_index'])
        descriptor = dict(live.gpu_input_create()); descriptor.pop('pitch')
        input_mapping = SharedCudaBuffer(cuda, **descriptor)
        descriptor = dict(live.gpu_output_create())
        pitch = descriptor.pop('pitch')
        output_mapping = SharedCudaBuffer(cuda, **descriptor)
        output = SharedCudaImage(output_mapping, 640, 360, pitch)
        cases = [(640, 360, 'rgba8', 'srgb'), (630, 360, 'rgba8', 'srgb'),
                 (800, 448, 'rgba16f', 'hdr10_pq'), (640, 360, 'rgba8', 'srgb')]*int(os.environ.get('DLSS5TOOL_RESOLUTION_CYCLES', '1'))
        for index, (width, height, layout, profile) in enumerate(cases):
            if index:
                updated = dict(live.settings, frame_format=layout, color_profile=profile)
                started = time.perf_counter()
                live.resize(width, height, settings=updated)
                event('gpu_resize', index=index, seconds=time.perf_counter()-started,
                      worker_pid=session._process.pid)
                if live._session is not session or session._process.pid != pid:
                    raise RuntimeError('GPU diagnostic unexpectedly replaced its worker')
            pixels = frame(width, height)
            if layout == 'rgba16f':
                pixels = (pixels.astype(np.float32)/255).astype(np.float16)
            reference = live.process(pixels, reset=True)
            with CudaImage(cuda, width, height, layout=layout, color_profile=profile) as image:
                with image.upload(pixels, FrameTiming(index, reset=True)) as source:
                    output.begin_write()
                    live.process_cuda_frame(source, input_mapping, reset=True)
                    with output.complete(source.timing) as result:
                        actual = output.download(result)
            np.testing.assert_array_equal(actual, reference)
            event('gpu_image', index=index, width=width, height=height, layout=layout,
                  matches_cpu=True, sha256=hashlib.sha256(memoryview(actual)).hexdigest())
    finally:
        for resource in (live, output, output_mapping, input_mapping, cuda):
            if resource is not None:
                resource.close()


def workload(args, work):
    if sys.stdin.buffer.read(1) != b'G':
        raise RuntimeError('Missing supervisor gate')
    os.environ.update(TEMP=str(work), TMP=str(work),
        DLSS5TOOL_RESOLUTION_WORK=str(work), DLSS5TOOL_RESOLUTION_CASE=args.case,
        DLSS5TOOL_RESOLUTION_CYCLES=str(args.cycles),
        DLSS5TOOL_SETTINGS_PATH=str(work/'settings.json'),
        DLSS5TOOL_QUEUE_PATH=str(work/'queue.json'), DLSS5TOOL_DISABLE_UPDATE_CHECK='1')
    from dlss5tool import paths
    state = work/'state'
    state.mkdir(exist_ok=True)
    paths.state_path = lambda filename: state/filename
    from dlss5tool import dlss_host_process as host, dlss_engine, app_settings
    host._host_worker_main = profile_worker
    host._HostSession._initialize_resources = timed('parent_session_initialize', host._HostSession._initialize_resources)
    host._HostSession.close = timed('parent_session_close', host._HostSession.close)
    host._CREATE_TIMEOUT = host._COMMAND_TIMEOUT = 60
    settings = dict(app_settings.DEFAULTS, host_backend='v2', host_auto_fallback=False,
        guidance_mode=0, dlss_runtime=str(ROOT/'runtime/nvngx_dlssnr.dll'),
        host_submission='compatibility', host_in_flight=1,
        host_persistent_buffers=True, host_zero_fast_path=True, style=0,
        intensity=.5, local_tone=.5, local_struct=.5, skin_struct=.5,
        frame_format='rgba8', color_profile='srgb')
    event('configuration', settings=settings, executable=sys.executable,
          host_dll=dlss_engine.HOST_DLL_V2)
    if args.case == 'gpu':
        gpu_workload(work, settings)
        dlss_engine.close_adapter_probe()
        event('complete')
        return
    live = None
    try:
        live = timed('create_process_live', host.ProcessLive)(*SIZES[0], settings)
        event('worker_identity', worker_pid=live._session._process.pid,
              token_compatibility=live._session._job.token_compatibility)
        if args.case == 'production':
            previous = SIZES[0]
            for index, (width, height) in enumerate(SIZES*args.cycles):
                if (width, height) != previous:
                    timed('replace_process', live.resize)(width, height, settings=settings)
                    event('worker_identity', worker_pid=live._session._process.pid,
                          token_compatibility=live._session._job.token_compatibility)
                timed('update_settings', live.update)(settings)
                process_one(live, index, width, height)
                previous = (width, height)
    finally:
        if live is not None:
            timed('close_process_live', live.close)()
        dlss_engine.close_adapter_probe()
    event('complete')


def manifest(task=None):
    names = ['dlss5tool/dlss_host_process.py', 'dlss5tool/dlss_engine.py',
        'dlss5tool/nvidia_token_compat.py', 'native/host_v2/dlssnr_host_v2.cpp',
        'scripts/profile_resolution_switch.py', 'runtime/dlssnr_host_v2.dll',
        'runtime/nvngx_dlssnr.dll', 'dlss5tool/host_reconfigure.py',
        'dlss5tool/gpu_reconfigure.py', 'dlss5tool/cuda_frame.py',
        'dlss5tool/cuda_interop.py', 'dlss5tool/guidance_worker.py',
        'dlss5tool/guidance_client.py']
    if task is not None and (task/'candidate/dlssnr_host_v2.dll').is_file():
        names.append(str((task/'candidate/dlssnr_host_v2.dll').relative_to(ROOT)))
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}


def supervise(args, work):
    from dlss5tool.process_job import KillOnCloseJob
    from scripts.ngx_updater_lifecycle_probe import Processes
    from scripts.verify_p0_stability import Memory
    from scripts.verify_native_worker_recycling import Performance, job_pids
    before = manifest(args.task)
    processes, memory, performance = Processes(), None, Performance()
    memory = Memory(processes)
    start = time.monotonic()
    peak, safety, residual, job = 0, None, [], None
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--task',
               str(args.task), '--case', args.case, '--cycles', str(args.cycles), '--worker']
    if args.label:
        command.extend(['--label', args.label])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TEMP=str(work), TMP=str(work))
    with (work/'console.log').open('wb') as console:
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=console, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job = KillOnCloseJob(child)
            child.stdin.write(b'G'); child.stdin.flush(); child.stdin.close()
            exited_at = None
            while True:
                members = set(job_pids(job))
                private = 0
                for row in processes.snapshot():
                    if row['pid'] in members:
                        identity = processes.identity(row)
                        private += memory.read(identity).get('private_bytes', 0)
                peak = max(peak, private)
                available = performance.read()
                if private > 8*1024**3 or min(available['free_physical_bytes'], available['free_commit_bytes']) < 8*1024**3:
                    safety = 'Diagnostic memory/headroom limit reached'
                    break
                if time.monotonic()-start > 150:
                    safety = 'Diagnostic time limit reached'
                    break
                if child.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if not members or time.monotonic()-exited_at > 5:
                        break
                time.sleep(.5)
        finally:
            if job is not None:
                residual = job_pids(job)
                job.close()
            elif child.poll() is None:
                child.terminate()
            child.wait(timeout=20)
    result = dict(case=args.case, child_exit_code=child.returncode,
        safety_error=safety, residual_before_job_close=residual,
        peak_job_private_bytes=peak, elapsed_seconds=time.monotonic()-start,
        manifest_before=before, manifest_after=manifest(args.task))
    result['passed'] = child.returncode == 0 and not safety and not residual and result['manifest_before'] == result['manifest_after']
    (work/'result.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--case', choices=('production', 'native', 'gpu'), required=True)
    parser.add_argument('--label', default='')
    parser.add_argument('--cycles', type=int, default=1)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 32:
        parser.error('Cycles must be between 1 and 32')
    from scripts.verify_p0_stability import checked_path
    args.task = checked_path(args.task)
    if not args.task.is_relative_to(ROOT/'tmp') or not (args.task/'TASK.md').is_file():
        parser.error('Registered repository tmp task required')
    if args.label and (not args.label.isascii() or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in args.label)):
        parser.error('Invalid case label')
    work = checked_path(args.task/(args.case+('-'+args.label if args.label else '')))
    if args.worker:
        workload(args, work)
        return 0
    work.mkdir(exist_ok=False)
    return supervise(args, work)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
