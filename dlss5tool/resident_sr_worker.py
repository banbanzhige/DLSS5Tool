"""One-shot isolated NVDEC -> CUDA VSR -> existing NVENC owner.

Private inherited JSON pipe; device pointers stay inside this process. Native
work starts only after the parent arms its Windows kill-on-close Job.
"""
import gc
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
WORKER_FLAG = '--resident-sr-worker'
MAX_MESSAGE = 128*1024


def run(request, notify):
    import ctypes as C
    from fractions import Fraction
    import av
    from av.codec.hwaccel import HWAccel
    from dlss5tool.cuda_session import CudaSession, bind, check
    from dlss5tool.cuda_frame import FrameTiming
    from dlss5tool.cuda_nv12 import CudaNv12Converter
    from dlss5tool.cuda_vsr import CudaVSR
    from dlss5tool.gpu_video_export import NativeGpuVideoWriter
    from dlss5tool.memory_budget import memory_scope, memory_allocation, require_memory_headroom
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.export_metrics import ExportTelemetry
    from dlss5tool.resident_sr import safe_path, source_identity

    # This is a disposable worker. FFmpeg's Python logging callback can deadlock
    # native decoder threads; no global logging change is made in the GUI.
    av.logging.restore_default_callback()
    plan, components = request['plan'], request['components']
    if components.get('pyav') and (av.__version__ != components['pyav'] or
            {name:list(version) for name,version in av.library_versions.items()} != components.get('av_libraries')):
        raise RuntimeError('PyAV/FFmpeg libraries differ from resident conversion acceptance')
    width, height, scale = plan['width'], plan['height'], plan['scale']
    rate, total = Fraction(plan['rate']), plan['frames']
    source = safe_path(request['source'], exists=True)
    output = safe_path(request['staging'])
    if source_identity(source) != request['source_identity']:
        raise RuntimeError('Source changed after export preflight')
    for key in ('nv12_ptx', 'native_library', 'sdr_ptx'):
        path = safe_path(components[key], exists=True)
        from dlss5tool.cuda_vsr_runtime import sha256
        if sha256(path) != components.get('component_hashes', {}).get(key):
            raise RuntimeError('Resident component changed after preflight: '+key)
    from dlss5tool import paths
    if sha256(paths.runtime_root()/'vsr-cuda/manifest.json') != components.get('cuda_vsr_manifest'):
        raise RuntimeError('CUDA VSR pair changed after preflight')
    pool = SharedCacheBudget(request['pool']) if request.get('pool') else None
    telemetry = ExportTelemetry()
    cuda = converter = vsr = writer = container = stream = iterator = frame = None
    hashes = []
    completed = False
    try:
      with memory_scope(pool):
        with telemetry.measure('initialize'), memory_allocation(
                512*1024**2+width*height*64+(width*height*scale*scale)*24, stage='resident_sr_initialize') as ticket:
            cuda = CudaSession(plan['adapter']['cuda_index'])
            expected_luid = ((plan['adapter']['luid_high'] & 0xffffffff)<<32)|plan['adapter']['luid_low']
            if cuda.luid != expected_luid:
                raise RuntimeError('Actual CUDA adapter differs from the selected physical GPU')
            with cuda.current():
                available, capacity = C.c_size_t(), C.c_size_t()
                check(bind(cuda.cuda, 'cuMemGetInfo_v2', [C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)])(
                    C.byref(available), C.byref(capacity)), 'Resident GPU memory preflight')
                if available.value < 512*1024**2+width*height*64+width*height*scale*scale*24:
                    raise MemoryError('Insufficient VRAM for the bounded resident SR pipeline')
                container = av.open(str(source), mode='r', timeout=(20, 60), hwaccel=HWAccel('cuda',
                    device=plan['adapter']['cuda_index'], allow_software_fallback=False, is_hw_owned=True,
                    options={'primary_ctx':'0', 'current_ctx':'1'}))
                stream = container.streams.video[0]
                stream.codec_context.thread_count = 1
                if not stream.codec_context.is_hwaccel:
                    raise RuntimeError('Resident export requires confirmed NVDEC decoding')
                iterator = container.decode(stream)
                converter = CudaNv12Converter(cuda, components['nv12_ptx'], width, height)
                vsr = CudaVSR.from_runtime(cuda, width, height, scale, log_path=request['native_log'])
                writer = NativeGpuVideoWriter(output, width*scale, height*scale, rate,
                    audio_source=str(source), native_library=components['native_library'],
                    sdr_ptx=components['sdr_ptx'], cuda_session=cuda, device_input=True,
                    device_ordinal=plan['adapter']['cuda_index'], use_nvenc=True,
                    nvenc_preset='p5', rate_control='quality', quality_profile=plan['quality'], codec='h264')
            if ticket:
                ticket.consume(512*1024**2+width*height*64+width*height*scale*scale*24)
        notify('initialized', adapter_luid=cuda.luid)
        count = 0
        previous = None
        started_frames = last_progress = time.perf_counter()
        while True:
            require_memory_headroom(1, stage='resident_sr_frame')
            with telemetry.measure('decode'), cuda.current():
                frame = next(iterator, None)
            if frame is None:
                break
            timestamp = Fraction(frame.pts)*frame.time_base if frame.pts is not None else None
            interval = 1/rate
            if (timestamp is None or count >= total or (count == 0 and timestamp != 0)
                    or previous is not None and abs(timestamp-previous-interval) > max(Fraction(2, 1000000), interval/500)
                    or int(round(getattr(frame, 'rotation', 0)/90)) % 4):
                raise ValueError('Source frame count, CFR timeline or orientation differs from preflight')
            if frame.color_trc in (16, 18) or frame.interlaced_frame:
                raise ValueError('HDR or interlaced frames cannot enter resident SDR export')
            timing = FrameTiming(count, frame.pts, Fraction(frame.time_base), count == 0)
            with telemetry.measure('nv12_rgba'):
                decoded = converter.convert(frame, timing)
            with decoded:
                with telemetry.measure('super_resolution'):
                    processed = vsr.process_device(decoded)
                with processed:
                    if request.get('audit'):
                        # Diagnostic-only CPU snapshot; normal export never downloads RGB.
                        import hashlib
                        import numpy as np
                        rgb = vsr._output.download(processed)[..., :3]
                        hashes.append(hashlib.sha256(np.ascontiguousarray(rgb[..., ::-1]).tobytes()).hexdigest())
                    with telemetry.measure('encode'):
                        writer.write_cuda_frame(processed)
            frame = None
            previous = timestamp
            count += 1
            now = time.perf_counter()
            if count == 1 or count == total or now-last_progress >= .2:
                notify('progress', frames=count, total=total)
                last_progress = now
        if count != total:
            raise ValueError('Source ended before the inspected frame count')
        frame_seconds = time.perf_counter()-started_frames
        # Release decoder frames and codec references while their owner is current.
        with telemetry.measure('decoder_close'), cuda.current():
            iterator.close(); iterator = None
            container.close(); container = None
            frame = stream = None
            gc.collect()
        with telemetry.measure('encode_finish'):
            writer.finish()
        audio_mode = writer.audio_mode
        writer = None
        converter.close(); converter = None
        if source_identity(source) != request['source_identity']:
            raise RuntimeError('Source changed while exporting')
        result = dict(status='complete', route='resident-cuda-sr', real_frames=count, generated_frames=0,
            output_rate=str(rate), width=width*scale, height=height*scale,
            adapter=plan['adapter'], actual_luid=cuda.luid, audio_mode=audio_mode,
            frame_seconds=frame_seconds, frame_fps=count/frame_seconds, frame_hashes=hashes,
            audit=bool(request.get('audit')), performance=telemetry.snapshot(),
            decode_backend='NVDEC → CUDA VSR → NVENC', new_frames=count, cache_hits=0)
        completed = True
        # The output is drained/muxed, and NVDEC/NVENC owners are released. The
        # parent retires this process before final publication, even if NGX
        # global shutdown blocks after feature destruction on this driver.
        notify('complete', result=result)
    except BaseException as error:
        notify('failed', error=(type(error).__name__+': '+str(error))[:4000])
        raise
    except BaseException as error:
        notify('failed', error=(type(error).__name__+': '+str(error))[:4000])
        raise
    finally:
        if writer:
            writer.abort()
        if iterator:
            iterator.close()
        frame = iterator = None
        if container:
            container.close()
        container = stream = None
        gc.collect()
        if converter:
            converter.close()
        if vsr:
            vsr.close()
        if cuda:
            cuda.close()
        if pool:
            pool.close()
    return completed


def worker_main(handle, nonce):
    from multiprocessing.connection import PipeConnection
    connection = PipeConnection(handle, readable=True, writable=True)
    def notify(kind, **value):
        data = json.dumps(dict(version=1, nonce=nonce, pid=os.getpid(), kind=kind, **value)).encode('utf-8')
        if len(data) > MAX_MESSAGE:
            raise ValueError('Resident SR protocol message exceeds limit')
        connection.send_bytes(data)
    try:
        if not connection.poll(20):
            raise RuntimeError('Parent startup confirmation timed out')
        message = json.loads(connection.recv_bytes(MAX_MESSAGE).decode('utf-8'))
        if message.get('version') != 1 or message.get('nonce') != nonce or message.get('operation') != 'start':
            raise ValueError('Invalid resident export startup confirmation')
        try:
            run(message['request'], notify)
        except BaseException as error:
            notify('failed', error=(type(error).__name__+': '+str(error))[:4000])
            raise SystemExit(1)
            raise SystemExit(1)
    finally:
        connection.close()


def dispatch(argv=None):
    import re
    args = list(sys.argv[1:] if argv is None else argv)
    if WORKER_FLAG not in args:
        return False
    if len(args) != 3 or args[0] != WORKER_FLAG or not args[1].isdigit() or not re.fullmatch('[0-9a-f]{32}', args[2]):
        raise SystemExit(2)
    worker_main(int(args[1]), args[2])
    return True


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    raise SystemExit(0 if dispatch() else 2)
