"""Crash-isolated GPU renderer adapting to the existing bounded preview cache."""
from fractions import Fraction
import json
import multiprocessing
from multiprocessing import shared_memory
import os
from pathlib import Path
import subprocess
import time
import uuid

import numpy as np


def render_preview(source, output=None, *, multiplier=1, scale=2, enhance=True, settings=None,
                   cancel, progress=None, frame_sink, render_gate=None, metadata_sink=None,
                   source_inspector=None, telemetry=None, components=None):
    from dlss5tool import paths
    from dlss5tool.frame_generation import check_cancel, inspect_source, export_video
    from dlss5tool.device_sr_plan import make_plan
    from dlss5tool.resident_sr import safe_path, source_identity, _spawn, MAX_MESSAGE
    from dlss5tool.memory_budget import current_pool, memory_allocation, require_memory_headroom
    from dlss5tool.process_job import KillOnCloseJob, confirm_retired, private_commit
    from dlss5tool.dlss_engine import available_render_adapters
    settings = dict(settings or {})
    if not enhance:
        settings['_nr_bypass'] = 'source'
    inspect = source_inspector or inspect_source
    inspection = inspect(source, cancel, progress=progress, **({'allow_vfr':True} if source_inspector is None else {}))
    adapters = available_render_adapters(cancel=cancel)
    requested = settings.get('render_gpu', 'auto')
    adapter = next((item for item in adapters if item['id'] == requested), None) if requested != 'auto' else next(iter(adapters), None)
    plan = make_plan(inspection, settings, adapter or {})
    if components is None:
        from dlss5tool.resident_hdr_runtime import load_components
        components = load_components()
    if (plan is None or components is None or
            [adapter['vendor_id'], adapter['device_id']] not in components['verified_devices']):
        return export_video(source, output, multiplier=multiplier, scale=scale, enhance=enhance,
            settings=settings, cancel=cancel, progress=progress, frame_sink=frame_sink,
            render_gate=render_gate, metadata_sink=metadata_sink, telemetry=telemetry,
            source_inspector=lambda *args, **kwargs: inspection)
    if output is not None or (multiplier, scale) != (plan['multiplier'], plan['scale']):
        raise ValueError('GPU preview renderer contract mismatch')
    source = safe_path(source, exists=True)
    width, height = plan['width']*scale, plan['height']*scale
    dtype = np.dtype(np.float16 if plan['color_profile'] != 'srgb' else np.uint8)
    size = width*height*4*dtype.itemsize
    require_memory_headroom(size*8+512*1024**2, stage='resident_preview_start', cancel=cancel)
    pool = current_pool(create=False)
    nonce = uuid.uuid4().hex
    log_root = safe_path(os.environ.get('DLSS5TOOL_SR_PREVIEW_LOG_ROOT') or paths.state_path('sr-preview'))
    log_root.mkdir(parents=True, exist_ok=True)
    directory = safe_path(log_root/nonce)
    directory.mkdir()
    memories, views = [], []
    include_spatial = bool(plan['nr'] and multiplier == 1)
    slot_count = 3 if include_spatial else 2
    receiver = sender = log = process = job = None
    metadata = dict(source_metadata=inspection[0], source_rate=plan['rate'], source_frames=plan['frames'],
        width=width, height=height, output_rate=str(Fraction(plan['rate'])*multiplier),
        hdr_metadata=plan.get('hdr_metadata'), scene_cuts=(), color=plan['color_profile'],
        super_resolution_adapter=dict(adapter, backend='cuda', verified=True), route='resident-gpu-preview')
    cuts = set()
    next_sequence = next_source = 0
    result = None

    def send(**value):
        receiver.send_bytes(json.dumps(dict(version=1, nonce=nonce, **value)).encode('utf-8'))

    try:
        with memory_allocation(size*slot_count, stage='resident_preview_shared') as ticket:
            for _ in range(slot_count):
                memory = shared_memory.SharedMemory(create=True, size=size)
                memories.append(memory)
                views.append(np.ndarray((height,width,4), dtype, buffer=memory.buf))
            if ticket: ticket.consume(size*slot_count)
        receiver, sender = multiprocessing.Pipe(duplex=True)
        log = safe_path(directory/'worker.log').open('xb')
        process = _spawn(sender, nonce, log)
        job = KillOnCloseJob(process)
        sender.close(); sender = None
        request = dict(mode='preview', source=str(source), source_identity=source_identity(source),
            plan=plan, components=components, pool=pool.name if pool else None,
            shared_frames=[memory.name for memory in memories], include_spatial=include_spatial,
            native_log=str(directory/'native.log'))
        send(operation='start', request=request)
        deadline = time.monotonic()+120
        if metadata_sink: metadata_sink(dict(metadata))
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise TimeoutError('GPU preview worker stalled for 120 seconds')
            if not receiver.poll(.05):
                if process.poll() is not None:
                    raise RuntimeError('GPU preview worker exited before completion')
                continue
            reply = json.loads(receiver.recv_bytes(MAX_MESSAGE).decode('utf-8'))
            if (reply.get('version') != 1 or reply.get('nonce') != nonce or reply.get('pid') != process.pid):
                raise RuntimeError('Invalid GPU preview response')
            deadline = time.monotonic()+120
            kind = reply.get('kind')
            if kind == 'initialized':
                expected = ((adapter['luid_high'] & 0xffffffff)<<32)|adapter['luid_low']
                if reply.get('adapter_luid') != expected:
                    raise RuntimeError('GPU preview selected a different physical adapter')
            elif kind == 'gate':
                source_index = reply.get('source_index')
                if type(source_index) is not int or source_index != next_source or source_index >= plan['frames']:
                    raise RuntimeError('Invalid preview source progress')
                minimum = render_gate(source_index) if render_gate else 0
                minimum = minimum if type(minimum) is int else 0
                check_cancel(cancel)
                send(operation='continue', source_index=source_index, readback_minimum=minimum)
                deadline = time.monotonic()+120
                next_source += 1
            elif kind in ('frame', 'skipped'):
                sequence = reply.get('sequence')
                if type(sequence) is not int or sequence != next_sequence or sequence >= plan['frames']*multiplier:
                    raise RuntimeError('Invalid preview output sequence')
                new_cuts = reply.get('cuts', [])
                if any(type(value) is not int or not 0 < value < plan['frames'] for value in new_cuts):
                    raise RuntimeError('Invalid preview scene metadata')
                cuts.update(new_cuts)
                metadata['scene_cuts'] = tuple(sorted(cuts))
                if metadata_sink: metadata_sink(dict(metadata))
                if kind == 'skipped':
                    skip = getattr(frame_sink, 'skip', None)
                    if skip: skip(sequence)
                else:
                    from dlss5tool.render_cache import RenderPair
                    with memory_allocation(size*slot_count, stage='resident_preview_snapshot'):
                        processed, reference = (view.copy() for view in views[:2])
                        processed.flags.writeable = reference.flags.writeable = False
                        mix_source = views[2].copy() if include_spatial else processed if not plan['nr'] else None
                        if mix_source is not None: mix_source.flags.writeable = False
                        pair = RenderPair(processed, reference, mix_source)
                        options = dict(mix_source=mix_source)
                        if getattr(frame_sink, 'accepts_source_pair', False): options['source_pair'] = pair
                        frame_sink(sequence, processed, reference, **options)
                    check_cancel(cancel)
                    send(operation='frame_ack', sequence=sequence)
                next_sequence += 1
            elif kind == 'complete':
                result = reply.get('result')
                if (not isinstance(result, dict) or result.get('status') != 'complete'
                        or result.get('real_frames') != plan['frames'] or next_source != plan['frames']
                        or result.get('output_frames') != plan['frames']*multiplier
                        or next_sequence != plan['frames']*multiplier):
                    raise RuntimeError('Preview completed outside its selected frame contract')
                break
            elif kind == 'failed':
                raise RuntimeError(reply.get('error', 'GPU preview failed'))
            else:
                raise RuntimeError('Unknown GPU preview message')
            usage = private_commit(process)
            if usage is not None and usage > 4*1024**3:
                raise MemoryError('GPU preview worker exceeded its private-memory guard')
        try:
            code = process.wait(timeout=2)
            if code != 0: raise RuntimeError('GPU preview teardown failed')
            result['worker_retirement'] = 'normal'
        except subprocess.TimeoutExpired:
            result['worker_retirement'] = 'forced_after_completed_preview'
        job.close(); job = None
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        confirm_retired(process)
        result['worker_pid'] = process.pid
        return result
    finally:
        if job: job.close()
        if process and process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if process: confirm_retired(process)
        for handle in (sender, receiver, log):
            if handle is not None: handle.close()
        views.clear()
        for memory in memories:
            memory.close()
            memory.unlink()
        if pool:
            with pool.locked(): pool.reap_locked()
