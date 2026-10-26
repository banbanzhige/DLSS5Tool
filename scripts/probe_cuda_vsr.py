"""Bounded isolated CUDA/D3D12 VSR correctness and transfer comparison.

Register a repository tmp task first; builds and models are reused. Each worker
has a 90-second parent timeout, a 4 GiB private-memory guard and <=4K output.
"""
import argparse
from contextlib import ExitStack
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_vsr_cuda import safe, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--backend', required=True, choices=('cuda', 'd3d12'))
    parser.add_argument('--width', type=int, default=320)
    parser.add_argument('--height', type=int, default=180)
    parser.add_argument('--scale', type=int, choices=(2, 4), default=2)
    parser.add_argument('--frames', type=int, default=16)
    parser.add_argument('--host-library', type=Path, help='Registered-task D3D12 host candidate')
    parser.add_argument('--select-adapter', action='store_true')
    parser.add_argument('--hdr', action='store_true', help='Coded HDR RGBA16F D3D12 compatibility control')
    parser.add_argument('--missing-adapter', action='store_true', help='D3D12 candidate must reject a nonexistent LUID')
    parser.add_argument('--installed-components', action='store_true', help='Use verified runtime CUDA component discovery')
    parser.add_argument('--decode-contract', action='store_true', help='Validate current PyAV resident NVDEC/DLPack/context contract')
    parser.add_argument('--decode-current-context', action='store_true', help='NVDEC borrows the already-current CUDA owner context')
    parser.add_argument('--worker', action='store_true')
    args = parser.parse_args()
    task = safe(args.task, ROOT/'tmp')
    if not (task/'TASK.md').is_file() or not args.label.replace('-', '').isalnum():
        raise ValueError('Registered task and plain label required')
    if (not 128 <= min(args.width, args.height) or args.width*args.height*args.scale**2 > 3840*2160
            or not 8 <= args.frames <= 64):
        raise ValueError('Bounded geometry, 8..64 frames and <=4K output required')
    report_path = task/f'{args.label}.json'
    if report_path.exists():
        raise ValueError('Refusing evidence overwrite')
    if args.hdr and args.backend != 'd3d12':
        raise ValueError('CUDA backend acceptance currently covers SDR RGBA8 only')
    if not args.worker:
        from dlss5tool.process_job import KillOnCloseJob
        from dlss5tool.shared_cache_budget import SharedCacheBudget
        pool = SharedCacheBudget(limit_bytes=1024**3)
        environment = dict(os.environ, TEMP=str(task), TMP=str(task), PYTHONDONTWRITEBYTECODE='1',
            PYTHONPATH=str(Path(sys.prefix)/'Lib/site-packages'), VSR_PROBE_POOL=pool.name)
        process = job = None
        try:
            process = subprocess.Popen([getattr(sys, '_base_executable', None) or sys.executable,
                '-B', str(Path(__file__).resolve()), *sys.argv[1:], '--worker'],
                cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, errors='replace',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            job = KillOnCloseJob(process)
            stdout, stderr = process.communicate('GO\n', timeout=90)
            log = dict(returncode=process.returncode, stdout=stdout[-4000:], stderr=stderr[-6000:])
        except subprocess.TimeoutExpired as error:
            log = dict(returncode='timeout', stdout=str(error.stdout)[-2000:], stderr=str(error.stderr)[-3000:])
        finally:
            if job: job.close()
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            with pool.locked(): pool.reap_locked()
            snapshot = pool.snapshot()
            pool.close()
        log.update(pool_after_retirement=snapshot, worker_still_alive=process.poll() is None,
                   worker_pid=process.pid, parent_pid=os.getpid())
        (task/f'{args.label}-process.json').write_text(json.dumps(log, indent=2), encoding='utf-8')
        print(json.dumps(log, ensure_ascii=False))
        retired = report_path.is_file() and json.loads(report_path.read_text(encoding='utf-8')).get('status') == 'complete_with_forced_retirement'
        return 0 if log['returncode'] == 0 or (log['returncode'] == 3 and retired) else 1
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('Worker native work requires parent job assignment')
    import numpy as np
    from dlss5tool import dlss_engine
    from dlss5tool.cuda_session import CudaSession
    from dlss5tool.cuda_frame import CudaImage, FrameTiming
    from dlss5tool.cuda_vsr import CudaVSR
    from dlss5tool.super_resolution import ProcessSuperResolution
    from dlss5tool.memory_budget import memory_scope, system_memory
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from scripts.profile_cached_export import private_bytes
    adapters = dlss_engine.available_render_adapters()
    if not adapters or adapters[0]['cuda_index'] is None:
        raise RuntimeError('A matched physical CUDA/DXGI adapter is required')
    adapter = adapters[0]
    expected_luid = ((adapter['luid_high'] & 0xffffffff) << 32) | adapter['luid_low']
    pool = SharedCacheBudget(os.environ['VSR_PROBE_POOL'])
    report = dict(backend=args.backend, width=args.width, height=args.height, scale=args.scale,
                  frames=args.frames, hdr=args.hdr, adapter=adapter, pid=os.getpid(),
                  frame_hashes=[], timings_ms=[], memory=[])
    if args.decode_contract:
        import ctypes as C
        import av
        from av.codec.hwaccel import HWAccel
        from dlss5tool.cuda_session import bind, check
        from dlss5tool.memory_budget import require_memory_headroom
        class Device(C.Structure): _fields_ = [('type', C.c_int), ('id', C.c_int)]
        class Dtype(C.Structure): _fields_ = [('code', C.c_uint8), ('bits', C.c_uint8), ('lanes', C.c_uint16)]
        class Tensor(C.Structure):
            _fields_ = [('data', C.c_void_p), ('device', Device), ('ndim', C.c_int), ('dtype', Dtype),
                        ('shape', C.POINTER(C.c_int64)), ('strides', C.POINTER(C.c_int64)), ('offset', C.c_uint64)]
        class Managed(C.Structure): _fields_ = [('tensor', Tensor), ('manager', C.c_void_p), ('deleter', C.c_void_p)]
        capsule_pointer = C.pythonapi.PyCapsule_GetPointer
        capsule_pointer.argtypes, capsule_pointer.restype = [C.py_object, C.c_char_p], C.c_void_p
        source = safe(task/'synthetic-1920x1080-96.mp4', task)
        require_memory_headroom(1920*1080*4*8, stage='resident_decode_probe')
        frames = []
        try:
            with memory_scope(pool), CudaSession(adapter['cuda_index']) as cuda:
                pointer_context = bind(cuda.cuda, 'cuPointerGetAttribute', [C.c_void_p, C.c_int, C.c_uint64])
                synchronize = bind(cuda.cuda, 'cuCtxSynchronize', [])
                options = {'primary_ctx': '0', 'current_ctx': '1'} if args.decode_current_context else None
                with cuda.current(), av.open(str(source), hwaccel=HWAccel('cuda', options=options,
                        device=adapter['cuda_index'], is_hw_owned=True, allow_software_fallback=False), timeout=(20, 30)) as container:
                    stream = container.streams.video[0]
                    stream.codec_context.thread_count = 1
                    iterator = container.decode(stream)
                    try:
                        for index, frame in enumerate(iterator):
                            assert stream.codec_context.is_hwaccel and frame.format.name == 'cuda'
                            # This Windows PyAV wheel cannot synchronize an
                            # explicit DLPack consumer stream. Complete decode
                            # in the shared owner context before exporting.
                            check(synchronize(), 'NVDEC producer completion')
                            planes = []
                            for plane in frame.planes:
                                assert plane.__dlpack_device__() == (2, adapter['cuda_index'])
                                capsule = plane.__dlpack__()
                                tensor = C.cast(capsule_pointer(capsule, b'dltensor'), C.POINTER(Managed)).contents.tensor
                                actual = C.c_void_p()
                                check(pointer_context(C.byref(actual), 1, tensor.data+tensor.offset), 'NVDEC pointer context')
                                assert actual.value == cuda.context.value
                                planes.append(dict(shape=list(tensor.shape[:tensor.ndim]),
                                    strides=list(tensor.strides[:tensor.ndim]), bits=tensor.dtype.bits,
                                    row_pitch=plane.line_size, same_primary_context=True))
                                capsule = tensor = None
                            frames.append(dict(index=index, pts=frame.pts, time_base=str(frame.time_base),
                                format=frame.format.name, software_format=frame.sw_format.name, planes=planes))
                            plane = None
                            frame = None
                            if index == 2: break
                    finally:
                        frame = plane = capsule = tensor = None
                        iterator.close()
            report.update(status='resident_decode_verified', pyav=av.__version__, frames=frames,
                borrowed_current_context=args.decode_current_context,
                source_sha256=digest(source), note='Resident NVDEC planes, same CUDA primary context, no RGB conversion or export acceptance')
        except BaseException as error:
            report.update(status='failed', error=f'{type(error).__name__}: {error}')
            raise
        finally:
            report['pool_after_close'] = pool.snapshot()
            pool.close()
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return 0
    if args.missing_adapter:
        from dlss5tool.super_resolution import _load_host, BASE
        if args.backend != 'd3d12' or not args.host_library:
            raise ValueError('Registered D3D12 host candidate required')
        library = _load_host(str(safe(args.host_library, task)))
        try:
            missing_low = adapter['luid_low'] ^ 0x40000000
            initialized = library.vsr_init_adapter(args.width, args.height, args.scale, 4, 0,
                BASE, str(task/f'{args.label}-native.log'), adapter['luid_high'], missing_low)
            assert not initialized, 'Native VSR silently selected another adapter'
            report.update(status='rejected_missing_adapter', requested_luid_low=missing_low)
            report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        finally:
            library.vsr_shutdown()
            pool.close()
        return 0
    started = time.perf_counter()
    timer = None
    def store():
        report['elapsed_seconds'] = time.perf_counter()-started
        report['warm_median_ms'] = statistics.median(report['timings_ms'][4:]) if len(report['timings_ms']) > 4 else None
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def retire():
        report.update(status='complete_with_forced_retirement', native_cleanup='NGX teardown exceeded 5 seconds')
        store()
        os._exit(3)
    try:
        with memory_scope(pool), ExitStack() as stack:
            if args.backend == 'cuda':
                cuda = stack.enter_context(CudaSession(adapter['cuda_index']))
                if cuda.luid != expected_luid:
                    raise RuntimeError('Actual CUDA adapter differs from the selected physical GPU')
                report['actual_luid'] = cuda.luid
                source = stack.enter_context(CudaImage(cuda, args.width, args.height, row_pitch=args.width*4+32))
                options = dict(log_path=task/f'{args.label}-native.log')
                if args.installed_components:
                    vsr = stack.enter_context(CudaVSR.from_runtime(cuda, args.width, args.height, args.scale, **options))
                else:
                    vsr = stack.enter_context(CudaVSR(cuda, args.width, args.height, args.scale,
                        library_path=task/'build/vsr_cuda.dll',
                        cudart_directory=task/'cuda-runtime-12.8/nvidia/cuda_runtime/bin',
                        runtime_directory=ROOT/'runtime', **options))
                def process(frame, index):
                    with source.upload(frame, FrameTiming(index, index, Fraction(1, 30), index == 0)) as input_frame:
                        with vsr.process_device(input_frame) as output:
                            assert output.timing == input_frame.timing and output.adapter_luid == expected_luid
                            return vsr._output.download(output)
            else:
                host = str(safe(args.host_library, task)) if args.host_library else None
                vsr = ProcessSuperResolution(args.width, args.height, args.scale,
                    is_hdr=args.hdr, render_gpu=adapter['id'] if args.select_adapter else 'auto', _host_library=host)
                stack.callback(vsr.close)
                report['actual_adapter'] = vsr.adapter_info
                def process(frame, index):
                    return vsr.process(frame)
            report['initialization_seconds'] = time.perf_counter()-started
            x = np.arange(args.width, dtype=np.uint16)[None, :]
            y = np.arange(args.height, dtype=np.uint16)[:, None]
            for index in range(args.frames):
                frame = np.empty((args.height, args.width, 4), np.uint8)
                for channel in range(3):
                    frame[..., channel] = (x*(channel+1)+y*(3-channel)+(index%4)*39) % 256
                frame[..., 3] = 255
                if index%4 == 1:
                    frame[..., :3] = (220, 31, 7)
                elif index%4 == 2:
                    frame[..., :3] = np.where(((x//7+y//9)%2)[..., None], (0, 17, 255), (255, 61, 0))
                if args.hdr:
                    frame = (frame.astype(np.float32)/255).astype(np.float16)
                before = time.perf_counter()
                output = process(frame, index)
                report['timings_ms'].append((time.perf_counter()-before)*1000)
                report['frame_hashes'].append(hashlib.sha256(memoryview(output)).hexdigest())
                if index < 4:
                    np.save(task/f'{args.label}-frame{index}.npy', output, allow_pickle=False)
                usage = private_bytes(os.getpid())
                memory = system_memory()
                report['memory'].append(dict(private_bytes=usage, system=memory))
                if (usage or 0) > 4*1024**3 or (memory and
                        min(memory['free_physical_bytes'], memory['free_commit_bytes']) < 4*1024**3):
                    raise MemoryError('Bounded probe memory stop')
            if args.backend == 'cuda':
                marks = []
                # Reuse an already-uploaded immutable source. This isolates
                # array copies + VSR + completion, excluding host readback.
                with source.complete(FrameTiming(args.frames)) as input_frame:
                    for _ in range(16):
                        before = time.perf_counter()
                        with vsr.process_device(input_frame): pass
                        marks.append((time.perf_counter()-before)*1000)
                report['device_only_median_ms'] = statistics.median(marks[4:])
                report['device_only_scope'] = 'Repeated resident input; D2D array copies, VSR, completion; no decode/encode/host transfer'
            report['status'] = 'processing_complete'
            store()
            timer = threading.Timer(5, retire)
            timer.daemon = True
            timer.start()
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        if timer: timer.cancel()
        report['pool_after_close'] = pool.snapshot()
        pool.close()
        store()
    print(json.dumps({key: report[key] for key in ('status', 'backend', 'initialization_seconds',
        'elapsed_seconds', 'warm_median_ms', 'pool_after_close')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
