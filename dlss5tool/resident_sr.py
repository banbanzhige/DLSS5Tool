"""Pre-start selection and transactional isolated resident SDR super-resolution."""
from fractions import Fraction
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from dlss5tool.resident_sr_worker import WORKER_FLAG, MAX_MESSAGE


def safe_path(value, *, exists=False):
    path = Path(value).absolute()
    if path.resolve() != path:
        raise ValueError('Resident export path redirects: '+str(path))
    for item in (path, *path.parents):
        if item.exists() and item.lstat().st_file_attributes & 0x400:
            raise ValueError('Resident export path contains a reparse point: '+str(item))
    if exists and not path.is_file():
        raise FileNotFoundError(path)
    return path


def source_identity(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def eligible_settings(settings):
    from dlss5tool import appearance
    return (settings.get('frame_generation_multiplier', 1) == 1
        and settings.get('super_resolution_scale') in (2, 4)
        and not settings.get('hdr_mode') and settings.get('output_view', 0) == 0
        and not settings.get('output_size') and appearance.enhancement_bypass(settings) == 'source'
        and settings.get('rate_control', 'quality') == 'quality'
        and settings.get('quality_profile', 'high') in ('high', 'balanced')
        and settings.get('nvenc_preset', 'p5') == 'p5'
        and settings.get('codec', 'auto') in ('auto', 'h264'))


def make_plan(inspection, settings, adapter):
    from dlss5tool.cuda_nv12 import verified_geometry
    from dlss5tool.cuda_vsr_runtime import verified_geometry as vsr_geometry
    from dlss5tool.gpu_decode import BACKEND
    from dlss5tool.gpu_decode import BACKEND
    meta, rate, width, height, count = inspection
    scale = settings['super_resolution_scale']
    rotations = [meta.get('tags', {}).get('rotate', 0),
                 *[item.get('rotation', 0) for item in meta.get('side_data_list', [])]]
    try:
        rotated = any(float(value) % 360 for value in rotations)
    except (ValueError, TypeError):
        return None
    if (not eligible_settings(settings) or meta.get('is_hdr') or meta.get('_is_vfr')
            or meta.get('decode_backend') not in (None, BACKEND)
            or meta.get('field_order') not in (None, 'unknown', 'progressive')
            or meta.get('decode_backend') not in (None, BACKEND)
            or rotated
            or not verified_geometry(width, height) or not vsr_geometry(width, height, scale)
            or meta.get('pixel_format') not in ('yuv420p', 'yuvj420p', 'nv12')
            or meta.get('codec_name') not in ('h264', 'hevc')
            or meta.get('color_space') not in ('bt709', 'bt470bg', 'smpte170m')
            or meta.get('color_range') not in ('tv', 'pc', 'unknown')
            or type(adapter.get('cuda_index')) is not int or not 0 < Fraction(rate) <= 240
            or type(count) is not int or count <= 0):
        return None
    return dict(width=width, height=height, scale=scale, rate=str(rate), frames=count,
                quality=settings.get('quality_profile', 'high'), adapter=dict(adapter))


def try_export(manager, source, output, settings, *, cancel, progress, frame_progress=None):
    """Choose before starting frames; unsupported settings keep cached export."""
    from dlss5tool import appearance
    if (settings.get('hdr_mode') is True or settings.get('frame_generation_multiplier', 1) > 1
            or appearance.enhancement_bypass(settings) != 'source'):
        from dlss5tool.resident_hdr import try_export as try_hdr
        return try_hdr(manager, source, output, settings, cancel=cancel, progress=progress,
                       frame_progress=frame_progress)
    if os.name != 'nt' or not eligible_settings(settings):
        return None
    from dlss5tool.image_sequence import is_sequence
    if is_sequence(source):
        return None
    from dlss5tool.resident_sr_runtime import load_components
    components = load_components()
    if components is None:
        return None
    if manager.has_complete_spatial(source, settings):
        return None
    from dlss5tool.dlss_engine import available_render_adapters
    adapters = available_render_adapters(cancel=cancel)
    requested = settings.get('render_gpu', 'auto')
    adapter = next((item for item in adapters if item['id'] == requested), None) if requested != 'auto' else next(iter(adapters), None)
    if adapter is None:
        raise RuntimeError('Selected render GPU is unavailable')
    if [adapter['vendor_id'], adapter['device_id']] not in components['verified_devices']:
        return None
    def inspect_progress(message, fraction=0, counts=None):
        progress(message, min(1.0, max(0.0, float(fraction or 0)))*.05)
    inspection = manager.inspect(source, cancel, progress=inspect_progress,
        allow_cpu_fallback=settings.get('decode_cpu_fallback') is True)
    plan = make_plan(inspection, settings, adapter)
    if plan is None or plan['frames'] < 96 or plan['width']*plan['height']*plan['scale']**2 < 1920*1080:
        return None
    # Keep the existing encoder capability decision before starting the worker.
    from dlss5tool.video_export import find_ffmpeg, select_video_encoder
    _, nvenc = select_video_encoder(find_ffmpeg(), plan['width']*plan['scale'],
        plan['height']*plan['scale'], float(Fraction(plan['rate'])), False, True,
        'p5', 'quality', plan['quality'], settings.get('video_bitrate_mbps', 20), codec='h264')
    if not nvenc:
        return None
    # Retire preview producers before allocating the full-chain owner.
    manager.close()
    if frame_progress:
        frame_progress(0, plan['frames'])
    result = export_resident(source, output, plan, components, cancel=cancel,
        progress=lambda message,fraction: progress(message,.05+.9*fraction),
        frame_progress=frame_progress, pool_name=manager.pool_name)
    from dlss5tool.i18n import tr
    progress(tr('status.shared_export',hit=0,done=plan['frames'],total=plan['frames']),1)
    return result


def _spawn(sender, nonce, log):
    from dlss5tool import resident_sr_worker
    handle = sender.fileno()
    command = ([sys.executable] if getattr(sys, 'frozen', False) else
        [sys._base_executable, '-B', str(Path(resident_sr_worker.__file__).resolve())])
    command += [WORKER_FLAG, str(handle), nonce]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    if not getattr(sys, 'frozen', False):
        environment['PYTHONPATH'] = str(Path(sys.prefix)/'Lib/site-packages')
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {'handle_list':[handle]}
    previous = os.get_handle_inheritable(handle)
    try:
        os.set_handle_inheritable(handle, True)
        return subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, close_fds=True, startupinfo=startup,
            creationflags=subprocess.CREATE_NO_WINDOW, cwd=Path(resident_sr_worker.__file__).resolve().parents[1])
    finally:
        os.set_handle_inheritable(handle, previous)


def _cleanup_transaction(directory):
    safe_path(directory)
    if not directory.name.startswith('.sr-') or len(directory.name) != 36:
        raise ValueError('Invalid owned SR transaction directory')
    for path in directory.iterdir():
        safe_path(path, exists=True)
        path.unlink()
    directory.rmdir()


def export_resident(source, output, plan, components, *, cancel, progress=None,
                    frame_progress=None, pool_name=None, audit=False, log_root=None):
    """Return only after owned worker retirement and final no-overwrite rename."""
    from dlss5tool.frame_generation import check_cancel
    from dlss5tool.memory_budget import current_pool, require_memory_headroom
    from dlss5tool.process_job import KillOnCloseJob, confirm_retired, private_commit
    from dlss5tool import paths
    source, destination = safe_path(source, exists=True), safe_path(output)
    if destination.exists() or destination == source:
        raise FileExistsError(destination)
    if destination.suffix.lower() not in ('.mp4', '.mov', '.mkv') or not destination.parent.is_dir():
        raise ValueError('Existing output directory and MP4/MOV/MKV required')
    disk_need = source.stat().st_size*plan.get('multiplier', 1)*plan['scale']**2*3
    if shutil.disk_usage(destination.parent).free < disk_need+15*1024**3:
        raise ValueError('Output disk needs estimated peak plus 15 GiB free')
    if audit and plan['frames'] > 256:
        raise ValueError('Per-frame audit is bounded to 256 frames')
    check_cancel(cancel)
    require_memory_headroom(512*1024**2, stage='resident_sr_start', cancel=cancel)
    nonce = uuid.uuid4().hex
    transaction = destination.parent/('.sr-'+nonce)
    safe_path(transaction)
    staging = transaction/('video'+destination.suffix)
    log_root = safe_path(log_root if log_root is not None else
        os.environ.get('DLSS5TOOL_SR_LOG_ROOT') or paths.state_path('sr-export'))
    log_root.mkdir(exist_ok=True, parents=True)
    receiver = sender = log = None
    process = job = None
    started = time.perf_counter()
    result = None
    pool = current_pool(create=False)
    if pool_name is None and pool:
        pool_name = pool.name
    request = dict(source=str(source), staging=str(staging), source_identity=source_identity(source),
        plan=plan, components=components, pool=pool_name, audit=audit, native_log=str(log_root/(nonce+'-native.log')))
    try:
        transaction.mkdir()
        log = safe_path(log_root/(nonce+'.log')).open('xb')
        receiver, sender = multiprocessing.Pipe(duplex=True)
        process = _spawn(sender, nonce, log)
        job = KillOnCloseJob(process)
        sender.close()
        receiver.send_bytes(json.dumps(dict(version=1, nonce=nonce, operation='start', request=request)).encode('utf-8'))
        deadline = time.monotonic()+120
        previous_frames = 0
        peak_private = 0
        last_memory_sample = float('-inf')
        while True:
            check_cancel(cancel)
            now = time.monotonic()
            if now-last_memory_sample >= .2:
                last_memory_sample = now
                peak_private = max(peak_private, private_commit(process) or 0)
                if peak_private > 4*1024**3:
                    raise MemoryError('Resident GPU worker exceeded its 4 GiB private-memory guard')
            if time.monotonic() > deadline:
                raise TimeoutError('Resident GPU export worker stalled for 120 seconds')
            if not receiver.poll(.05):
                if process.poll() is not None:
                    raise RuntimeError('Resident GPU export worker exited before completing output')
                continue
            reply = json.loads(receiver.recv_bytes(MAX_MESSAGE).decode('utf-8'))
            if (reply.get('version') != 1 or reply.get('nonce') != nonce or reply.get('pid') != process.pid):
                raise RuntimeError('Invalid resident GPU export response')
            deadline = time.monotonic()+120
            if reply.get('kind') == 'failed':
                raise RuntimeError(reply.get('error', 'Resident GPU export failed'))
            if reply.get('kind') == 'progress':
                done = reply.get('frames')
                if type(done) is not int or not previous_frames <= done <= plan['frames'] or reply.get('total') != plan['frames']:
                    raise RuntimeError('Invalid resident export frame progress')
                previous_frames = done
                require_memory_headroom(1, stage='resident_sr_progress', cancel=cancel)
                if frame_progress:
                    encoded = reply.get('encoded_frames', done)
                    expected_total = plan['frames']*plan.get('multiplier', 1)
                    if type(encoded) is not int or not 0 <= encoded <= expected_total:
                        raise RuntimeError('Invalid encoded-frame progress')
                    frame_progress(encoded, expected_total)
                if progress:
                    from dlss5tool.i18n import tr
                    progress(tr('status.shared_export',hit=0,done=done,total=plan['frames']),done/plan['frames'])
            elif reply.get('kind') == 'initialized':
                expected = ((plan['adapter']['luid_high'] & 0xffffffff)<<32)|plan['adapter']['luid_low']
                if reply.get('adapter_luid') != expected:
                    raise RuntimeError('Resident worker selected a different physical GPU')
            elif reply.get('kind') == 'complete':
                result = reply.get('result')
                if (not isinstance(result, dict) or result.get('status') != 'complete'
                        or result.get('real_frames') != plan['frames']
                        or result.get('output_rate') != str(Fraction(plan['rate'])*plan.get('multiplier', 1))
                        or (result.get('width'), result.get('height')) != (plan['width']*plan['scale'], plan['height']*plan['scale'])):
                    raise RuntimeError('Resident worker output differs from the selected contract')
                break
            else:
                raise RuntimeError('Unknown resident worker message')
        # Give native cleanup a short opportunity, then retire the owned Job.
        # Completion includes the encoder drain, audio policy and decoder close.
        try:
            code = process.wait(timeout=2)
            if code != 0:
                raise RuntimeError('Resident worker failed during native teardown')
            result['worker_retirement'] = 'normal'
        except subprocess.TimeoutExpired:
            result['worker_retirement'] = 'forced_after_completed_output'
        job.close(); job = None
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        result['worker_pid'] = process.pid
        result['worker_peak_private_bytes'] = peak_private
        confirm_retired(process)
        check_cancel(cancel)
        safe_path(staging, exists=True)
        safe_path(destination)
        if staging.stat().st_size <= 0 or source_identity(source) != request['source_identity']:
            raise RuntimeError('Output empty or source changed')
        os.rename(staging, destination)
        result.update(output=str(destination), elapsed_seconds=time.perf_counter()-started)
        return result
    finally:
        if job:
            job.close()
        if process and process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if process:
            confirm_retired(process)
        for handle in (sender, receiver, log):
            if handle is not None:
                handle.close()
        if pool:
            with pool.locked():
                pool.reap_locked()
        if transaction.exists():
            _cleanup_transaction(transaction)
