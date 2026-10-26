"""One isolated, demand-gated GPU preview owner with one host readback slot."""
from fractions import Fraction
import gc
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np


def run(request, notify, control):
    import av
    from av.codec.hwaccel import HWAccel
    from dlss5tool.cuda_session import CudaSession
    from dlss5tool.cuda_frame import FrameTiming
    from dlss5tool.cuda_vsr import CudaVSR
    from dlss5tool.cuda_nv12 import CudaNv12Converter
    from dlss5tool.cuda_p010 import CudaP010Converter
    from dlss5tool.device_consumers import DeviceConsumers
    from dlss5tool.resident_sr import safe_path, source_identity
    from dlss5tool.cuda_vsr_runtime import sha256
    from dlss5tool.memory_budget import memory_scope, memory_allocation, require_memory_headroom
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    from dlss5tool.frame_generation import resize_original
    from dlss5tool.export_metrics import ExportTelemetry
    av.logging.restore_default_callback()
    plan, components = request['plan'], request['components']
    source = safe_path(request['source'], exists=True)
    if source_identity(source) != request['source_identity']:
        raise RuntimeError('Preview source changed after preflight')
    if (av.__version__ != components['pyav'] or
            {key:list(value) for key,value in av.library_versions.items()} != components['av_libraries']):
        raise RuntimeError('Preview decoder libraries changed after verification')
    for key, checksum in components['component_hashes'].items():
        if sha256(safe_path(components[key], exists=True)) != checksum:
            raise RuntimeError('Preview component changed after preflight: '+key)
    width, height, scale = plan['width'], plan['height'], plan['scale']
    ow, oh = width*scale, height*scale
    hdr = plan['color_profile'] != 'srgb'
    dtype = np.dtype(np.float16 if hdr else np.uint8)
    frame_bytes = ow*oh*4*dtype.itemsize
    memories, shared_frames = [], []
    for name in request['shared_frames']:
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError('Invalid preview shared-memory name')
        memory = shared_memory.SharedMemory(name=name, track=False)
        if not frame_bytes <= memory.size < frame_bytes+65536:
            memory.close()
            raise ValueError('Preview shared-memory size changed')
        memories.append(memory)
        shared_frames.append(np.ndarray((oh, ow, 4), dtype, buffer=memory.buf))
    if len(memories) != (3 if request.get('include_spatial') else 2):
        raise ValueError('One preview frame/reference/spatial slot required')
    pool = SharedCacheBudget(request['pool']) if request.get('pool') else None
    telemetry = ExportTelemetry()
    cuda = converter = vsr = consumers = container = iterator = frame = None
    readback_minimum = 0
    emitted = skipped = 0
    references = {}
    current_spatial = None
    cuts = []
    try:
      with memory_scope(pool):
        require_memory_headroom(512*1024**2+frame_bytes*10, stage='resident_preview_initialize')
        cuda = CudaSession(plan['adapter']['cuda_index'])
        expected_luid = ((plan['adapter']['luid_high'] & 0xffffffff)<<32)|plan['adapter']['luid_low']
        if cuda.luid != expected_luid:
            raise RuntimeError('Preview selected a different physical CUDA GPU')
        from dlss5tool.device_memory import admit_plan
        vram_admission = admit_plan(cuda, plan)
        if components.get('analysis_cpu'):
            from dlss5tool.analysis_quantization import cpu_fingerprint
            if components['analysis_cpu'] != cpu_fingerprint():
                raise RuntimeError('Analysis CPU dispatch changed after preflight')
        with cuda.current():
            container = av.open(str(source), hwaccel=HWAccel('cuda', device=plan['adapter']['cuda_index'],
                allow_software_fallback=False, is_hw_owned=True, options={'primary_ctx':'0', 'current_ctx':'1'}))
            stream = container.streams.video[0]
            stream.codec_context.thread_count = 1
            if not stream.codec_context.is_hwaccel:
                raise RuntimeError('Preview requires confirmed NVDEC decoding')
            iterator = container.decode(stream)
        if hdr:
            converter = CudaP010Converter(cuda, components['p010_ptx'], width, height, color_profile=plan['color_profile'])
            vsr = CudaVSR(cuda, width, height, scale, library_path=components['hdr_vsr_library'],
                cudart_directory=components['cudart_directory'], runtime_directory=components['runtime_directory'],
                log_path=request['native_log'], color_profile=plan['color_profile'], hdr_ptx=components['hdr_pack_ptx'])
        else:
            converter = CudaNv12Converter(cuda, components['nv12_ptx'], width, height)
            vsr = CudaVSR.from_runtime(cuda, width, height, scale, log_path=request['native_log'])

        def emit(pixels, source_index, kind):
            nonlocal emitted, skipped
            sequence = pixels.timing.sequence
            current_cuts = consumers.scene_cuts if consumers else cuts
            if sequence < readback_minimum:
                skipped += 1
                notify('skipped', sequence=sequence, cuts=current_cuts[-1:])
                return
            reference = references.get(source_index)
            if reference is None:
                raise RuntimeError('Requested preview source reference is unavailable')
            with telemetry.measure('preview_readback'), memory_allocation(frame_bytes*3, stage='preview_readback'):
                np.copyto(shared_frames[0], pixels._owner.download(pixels))
                np.copyto(shared_frames[1], reference)
                if request.get('include_spatial'):
                    if kind != 'real' or current_spatial is None:
                        raise RuntimeError('Raw preview spatial lease is unavailable')
                    np.copyto(shared_frames[2], current_spatial._owner.download(current_spatial))
            notify('frame', sequence=sequence, source_index=source_index, frame_kind=kind, cuts=current_cuts[-1:])
            reply = control()
            if reply.get('operation') != 'frame_ack' or reply.get('sequence') != sequence:
                raise RuntimeError('Preview frame was not acknowledged before slot reuse')
            emitted += 1

        config = dict(plan['consumer_settings'], render_gpu=plan['adapter']['id'],
            color_profile=plan['color_profile'], frame_format='rgba16f' if hdr else 'rgba8',
            color_primaries=plan.get('hdr_metadata', {}).get('color_primaries', 'bt709'))
        consumers = DeviceConsumers(cuda, ow, oh, plan['rate'], config, components,
            Path(request['native_log']).parent, emit, validate_hdr=vsr._hdr_packing.validate if hdr else None,
            telemetry=telemetry)
        notify('initialized', adapter_luid=cuda.luid)
        previous_time = None
        count = 0
        while count < plan['frames']:
            notify('gate', source_index=count)
            reply = control()
            readback_minimum = reply.get('readback_minimum')
            if (reply.get('operation') != 'continue' or reply.get('source_index') != count
                    or type(readback_minimum) is not int or not 0 <= readback_minimum < plan['frames']*plan['multiplier']):
                raise ValueError('Invalid preview demand gate')
            require_memory_headroom(1, stage='resident_preview_frame')
            with telemetry.measure('decode'), cuda.current():
                frame = next(iterator, None)
            if frame is None:
                raise ValueError('Preview source ended before its inspected frame count')
            timestamp = Fraction(frame.pts)*frame.time_base if frame.pts is not None else None
            interval = 1/Fraction(plan['rate'])
            if (timestamp is None or count == 0 and timestamp != 0 or frame.interlaced_frame
                    or int(round(getattr(frame, 'rotation', 0)/90)) % 4
                    or previous_time is not None and abs(timestamp-previous_time-interval) > max(Fraction(2,1000000),interval/500)
                    or not hdr and frame.color_trc in (16,18)):
                raise ValueError('Preview source color/timeline/orientation changed')
            timing = FrameTiming(count, frame.pts, Fraction(frame.time_base), count == 0)
            with converter.convert(frame, timing) as decoded:
                if count*plan['multiplier']+plan['multiplier']-1 >= readback_minimum:
                    with memory_allocation(frame_bytes*2, stage='preview_source_reference'):
                        original = decoded._owner.download(decoded)
                        references[count] = resize_original(original, (ow, oh)).copy()
                        original = None
                with telemetry.measure('super_resolution'):
                    processed = vsr.process_device(decoded)
                with processed:
                    current_spatial = processed
                    if consumers:
                        consumers.feed(processed)
                    else:
                        emit(processed, count, 'real')
                    current_spatial = None
            previous_time = timestamp
            frame = None
            for old in [key for key in references if key < count-1]:
                del references[old]
            count += 1
        with cuda.current():
            if next(iterator, None) is not None:
                raise ValueError('Preview source has extra frames')
        result = consumers.finish() if consumers else dict(real_frames=count, generated_frames=0,
            scene_cuts=[], cut_holds=0, endpoint_holds=0, output_frames=count)
        with cuda.current():
            iterator.close(); container.close()
            iterator = container = stream = frame = None
            gc.collect()
        if consumers:
            consumers.close(); consumers = None
        result.update(status='complete', route='resident-gpu-preview', performance=telemetry.snapshot(),
            vram_admission=vram_admission,
            preview_readbacks=emitted, skipped_preview_readbacks=skipped, max_pending_frames=1)
        if source_identity(source) != request['source_identity']:
            raise RuntimeError('Preview source changed while processing')
        notify('complete', result=result)
    except BaseException as error:
        # Report before native teardown: NGX shutdown may block on this driver.
        # The parent can then retire the owned Job immediately on failure.
        import traceback
        notify('failed', error=type(error).__name__+': '+str(error), traceback=traceback.format_exc()[-4000:])
        raise
    finally:
        if consumers: consumers.close()
        if iterator:
            with cuda.current(): iterator.close()
        if container:
            with cuda.current(): container.close()
        frame = iterator = container = None
        if converter: converter.close()
        if vsr: vsr.close()
        if cuda: cuda.close()
        shared_frames.clear()
        for memory in memories: memory.close()
        if pool: pool.close()
