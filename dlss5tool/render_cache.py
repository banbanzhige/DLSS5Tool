"""Bounded full-precision render cache shared by playback and video encoding.

One sequential producer owns temporal model state. If an evicted past frame is
requested, restart from source frame zero, never stitch incompatible histories.
Only encoding settings are excluded from the pixel identity.
"""
from collections import OrderedDict
from fractions import Fraction
import hashlib
import json
import os
import shutil
from pathlib import Path
import threading

import numpy as np

from dlss5tool import paths
from dlss5tool.frame_generation import Cancelled, check_cancel, export_video, runtime_files, inspect_source

PIXEL_KEYS = (
    'style', 'preset', 'intensity', 'local_tone', 'local_struct', 'skin_struct', 'use_auto_mask',
    'ui_correction', 'output_mix', 'guidance_mode', 'guidance_edge', 'guidance_flow_direction',
    'guidance_flow_backend', 'guidance_flow_grid', 'guidance_flow_updates', 'guidance_device',
    'guidance_depth_encoder', 'guidance_depth_profile', 'guidance_execution', 'render_gpu',
    'host_backend', 'host_tiled_mode', 'host_tile_width', 'host_tile_height', 'hdr_mode', 'output_size',
    'super_resolution_scale', 'frame_generation_multiplier', 'mods_directory',
)


def file_identity(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return str(path), None


def render_identity(source, settings):
    if settings.get('_render_stage') == 'spatial':
        payload = [1, file_identity(source), {k: settings.get(k) for k in
            ('hdr_mode', 'super_resolution_scale', 'render_gpu')},
            [file_identity(paths.runtime_root()/name) for name in ('vsr_host.dll', 'nvngx_vsr.dll')]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    # Include all inference-specific settings, but never the pool name, budget,
    # preview window/view, encoder container/quality, progress, or UI language.
    pixel = {k: v for k, v in settings.items() if k in PIXEL_KEYS or
             k.startswith('host_') or
             (k.startswith('guidance_') and not any(s in k for s in ('cache', 'transport', 'output_layout')))}
    dependencies = [file_identity(p) for p in runtime_files()]
    from dlss5tool import mod_paths
    dependencies.append(file_identity(mod_paths.runtime_path(settings)))
    dependencies.extend(file_identity(p) for p in mod_paths.guidance_candidates(settings).values())
    if settings.get('guidance_mode'):
        dependencies.append(file_identity(mod_paths.enhancement_path(settings) / 'enhancement.json'))
    for name in ('dlssnr_host_v2.dll', 'nvngx_dlssnr.dll', 'vsr_host.dll', 'nvngx_vsr.dll'):
        dependencies.append(file_identity(paths.runtime_root() / name))
    for key, value in sorted(settings.items()):
        if (isinstance(value, str) and value.strip() and not key.startswith('guidance_')
                and ('weights' in key or key.endswith('_path'))):
            dependencies.append(file_identity(value))
    pixel['dlss_runtime'] = settings.get('dlss_runtime', '')
    payload = [4, file_identity(source), pixel, dependencies]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class RenderSession:
    def __init__(self, source, settings, limit_bytes, *, pool=None, renderer=None):
        self.source = str(Path(source).resolve())
        self.settings = dict(settings)
        self.settings['output_view'] = 0  # compare/difference overlays are consumers
        self.multiplier = int(settings.get('frame_generation_multiplier', 1))
        self.key = render_identity(source, settings)
        self.limit = int(limit_bytes)
        self.pool = pool
        self.renderer = renderer or export_video
        self.condition = threading.Condition(threading.RLock())
        self.cache = OrderedDict()
        self.bytes = 0
        self.frame_bytes = 0
        self.target = 0
        self.produced = -1
        self.metadata = None
        self.result = None
        self.error = None
        self.complete = False
        self.closed = False
        self.restart = False
        self.suspended = False
        self.upstream = None
        self.thread = None
        self.stop = threading.Event()
        self.hits = self.new_frames = self.replayed_frames = 0

    def _allowance(self):
        if self.pool is None:
            return self.limit
        with self.pool.locked():
            return min(self.limit, self.pool.allowance_locked(respect_demand=True))

    def _publish(self):
        if self.pool is not None:
            with self.pool.locked():
                self.pool.publish_locked(self.bytes)

    def _trim(self, needed=0):
        allowance = self._allowance()
        while self.cache and self.bytes + needed > allowance:
            # Retain the neighbourhood currently being consumed, not the most
            # recent frame produced during a seek-history replay.
            key = max(self.cache, key=lambda i: abs(i-self.target))
            left, right = self.cache.pop(key)
            self.bytes -= left.nbytes + right.nbytes
        self._publish()
        return allowance

    def set_limit(self, limit):
        with self.condition:
            self.limit = int(limit)
            self._trim()
            self.condition.notify_all()

    def _start(self):
        if self.closed or self.error:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._produce, name='shared-render-cache', daemon=True)
        self.thread.start()

    def request(self, index):
        index = max(0, int(index))
        with self.condition:
            if self.closed:
                raise Cancelled('处理缓存已关闭')
            self.suspended = False
            self.target = index
            self._trim()
            if self.error:
                raise RuntimeError(str(self.error))
            value = self.cache.get(index)
            if value is not None:
                self.cache.move_to_end(index)
            elif index <= self.produced and not self.restart:
                self.restart = True
                self.stop.set()
                self.complete = False
            if not self.complete:
                self._start()
            self.condition.notify_all()
            return value

    def peek(self, index):
        """Read only: UI display must not redirect the sequential producer."""
        with self.condition:
            return None if self.closed else self.cache.get(int(index))

    def buffered(self, index, desired):
        """Contiguous readiness, capped to the actual available pixel budget."""
        with self.condition:
            if not self.frame_bytes or not self.metadata:
                return False
            total = self.metadata['source_frames']*self.multiplier
            capacity = max(1, self._allowance()//self.frame_bytes)
            count = min(max(1, int(desired)), capacity, max(0, total-index))
            return count > 0 and all(i in self.cache for i in range(index, index+count))

    def wait(self, index, cancel):
        while True:
            check_cancel(cancel)
            value = self.request(index)
            if value is not None:
                return value
            with self.condition:
                if self.closed:
                    raise Cancelled('处理缓存已关闭')
                if self.complete and self.metadata and index >= self.metadata['source_frames']*self.multiplier:
                    raise IndexError(index)
                self.condition.wait(.05)

    def wait_metadata(self, cancel):
        with self.condition:
            if self.closed:
                raise Cancelled('处理缓存已关闭')
            if self.metadata:
                return dict(self.metadata)
        self.request(0)
        while True:
            check_cancel(cancel)
            with self.condition:
                if self.error:
                    raise RuntimeError(str(self.error))
                if self.closed:
                    raise Cancelled('处理缓存已关闭')
                if self.metadata:
                    return dict(self.metadata)
                self.condition.wait(.05)

    def _produce(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                self.stop = threading.Event()
                stop = self.stop
                replaying = self.restart
                self.restart = False
                self.produced = -1
                self.complete = False
            def metadata(value):
                with self.condition:
                    self.metadata = value
                    self.condition.notify_all()
            def gate(source_index):
                with self.condition:
                    while not stop.is_set():
                        if self.suspended:
                            self.condition.wait(.1)
                            continue
                        # Once a missing interval has been reconstructed, do
                        # not recompute the already-cached tail just to prefetch.
                        # Resume temporal replay only for the next real hole.
                        if replaying and self.target in self.cache:
                            self.condition.wait(.05)
                            continue
                        # One extra real frame supplies the right endpoint.
                        capacity = max(2*self.multiplier, self._allowance() // max(self.frame_bytes, 1))
                        ahead = min(capacity-1, max(2*self.multiplier, 120))
                        if not self.frame_bytes or source_index*self.multiplier <= self.target+ahead+self.multiplier:
                            break
                        self.condition.wait(.1)
                    check_cancel(stop)
            def sink(index, processed, reference):
                check_cancel(stop)
                if reference is None:
                    reference = processed
                size = processed.nbytes + reference.nbytes
                with self.condition:
                    if self.closed or stop.is_set():
                        raise Cancelled('缓存任务已撤销')
                    self.frame_bytes = size
                    self.new_frames += 1
                    if replaying and index < self.target:
                        self.replayed_frames += 1
                    # History is evaluated, but far-behind replay pixels need
                    # not occupy the target's cache space.
                    if index < self.target:
                        self.produced = index
                        self.condition.notify_all()
                        return
                    # Never evict the frame being awaited to make room for
                    # lookahead. This also permits a one-pair cache budget.
                    while (index > self.target and self.cache
                           and self.bytes + size > self._allowance()):
                        if size > self._allowance():
                            raise RuntimeError('缓存预算不足一个全精度帧对，请增加缓存预算')
                        others = [i for i in self.cache if i < self.target]
                        if others:
                            victim = max(others, key=lambda i: abs(i-self.target))
                            self.bytes -= sum(x.nbytes for x in self.cache.pop(victim))
                            self._publish()
                            continue
                        self.condition.wait(.05)
                        check_cancel(stop)
                    old = self.cache.pop(index, None)
                    if old:
                        self.bytes -= sum(x.nbytes for x in old)
                    allowance = self._trim(size)
                    if size > allowance:
                        raise RuntimeError('缓存预算不足一个全精度帧对，请增加缓存预算；未降低分辨率')
                    a, b = np.array(processed, copy=True), np.array(reference, copy=True)
                    a.flags.writeable = b.flags.writeable = False
                    self.cache[index] = (a, b)
                    self.produced = index
                    self.bytes += size
                    self._publish()
                    self.condition.notify_all()
            try:
                result = self.renderer(self.source, None, multiplier=self.multiplier,
                    scale=int(self.settings.get('super_resolution_scale', 1)), enhance=True,
                    settings=self.settings, cancel=stop, frame_sink=sink, render_gate=gate, metadata_sink=metadata)
                with self.condition:
                    self.result = result
                    self.complete = True
            except Cancelled:
                pass
            except BaseException as error:
                with self.condition:
                    self.error = error
            with self.condition:
                self.condition.notify_all()
                if self.restart and not self.closed and not self.error:
                    continue
                return

    def snapshot(self):
        with self.condition:
            return {'frames': len(self.cache), 'bytes': self.bytes, 'computed': self.new_frames,
                    'replayed': self.replayed_frames, 'complete': self.complete,
                    'ranges': sorted({i//self.multiplier for i in self.cache
                        if all((i//self.multiplier)*self.multiplier+j in self.cache
                               for j in range(self.multiplier))})}

    def suspend(self):
        with self.condition:
            self.suspended = True
            self.condition.notify_all()
        if self.upstream:
            self.upstream.suspend()

    def close(self):
        with self.condition:
            self.closed = True
            self.stop.set()
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=130)
            if self.thread.is_alive():
                raise RuntimeError('缓存处理尚未退出')
        with self.condition:
            self.cache.clear()
            self.bytes = 0
            self._publish()
        if self.pool is not None and (not self.thread or not self.thread.is_alive()):
            self.pool.close()
            self.pool = None


class RenderCache:
    def __init__(self, limit_bytes, pool_name=None):
        self.limit = int(limit_bytes)
        self.pool_name = pool_name
        self.sessions = OrderedDict()
        self.base = None
        self.spatial = None
        self.inspection = None
        self.lock = threading.RLock()

    def inspect(self, source, cancel):
        identity = file_identity(source)
        if self.inspection is None or self.inspection[0] != identity:
            result = inspect_source(source, cancel)
            check_cancel(cancel)
            self.inspection = (identity, result)
        return self.inspection[1]

    def session(self, source, settings, is_current=None):
        def latest():
            if is_current and not is_current():
                raise Cancelled('已被更新的参数替代')
        key = render_identity(source, settings)
        with self.lock:
            latest()
            current = self.sessions.get(key)
            if current is not None and not current.closed:
                self.sessions.move_to_end(key)
                return current
            # SR + NR and FG have separate identities. Changing only the FG
            # multiplier retires the FG worker, not the full-quality SR/NR cache.
            for old in self.sessions.values():
                if old is not self.base:
                    old.close()
            self.sessions.clear()
            latest()
            base_settings = {**settings, 'frame_generation_multiplier': 1, 'output_mix': 1, 'output_size': None,
                             '_render_stage': 'base'}
            base_key = render_identity(source, base_settings)
            multiplier = int(settings.get('frame_generation_multiplier', 1))
            def lease():
                if self.pool_name:
                    from dlss5tool.shared_cache_budget import SharedCacheBudget
                    return SharedCacheBudget(self.pool_name)
                return None
            layers = 3 if multiplier > 1 or settings.get('output_mix', 1) != 1 or settings.get('output_size') else 2
            base_limit = self.limit//layers
            spatial_settings = {k: settings[k] for k in ('hdr_mode', 'super_resolution_scale', 'render_gpu') if k in settings}
            spatial_settings.update(frame_generation_multiplier=1, guidance_mode=0, output_size=None,
                                    _render_stage='spatial')
            spatial_key = render_identity(source, spatial_settings)
            if self.spatial is None or self.spatial.key != spatial_key or self.spatial.closed:
                if self.base:
                    self.base.close()
                    self.base = None
                if self.spatial:
                    self.spatial.close()
                latest()
                def spatial_renderer(*args, **kwargs):
                    kwargs['enhance'] = False
                    return export_video(*args, **kwargs, source_inspector=self.inspect)
                self.spatial = RenderSession(source, spatial_settings, base_limit,
                                             pool=lease(), renderer=spatial_renderer)
            else:
                self.spatial.set_limit(base_limit)
            if self.base is None or self.base.key != base_key or self.base.closed:
                if self.base:
                    self.base.close()
                latest()
                spatial = self.spatial
                def base_renderer(*args, **kwargs):
                    return export_video(*args, **kwargs, input_session=spatial)
                self.base = RenderSession(source, base_settings, base_limit, pool=lease(), renderer=base_renderer)
                self.base.upstream = spatial
            else:
                self.base.set_limit(base_limit)
            if layers == 2:
                current = self.base
            else:
                base = self.base
                def renderer(*args, **kwargs):
                    return export_video(*args, **kwargs, input_session=base)
                current = RenderSession(source, {**settings, '_render_stage': 'output'}, self.limit-2*base_limit,
                    pool=lease(), renderer=renderer)
                current.upstream = base
            self.sessions[key] = current
            return current

    def set_limit(self, limit):
        with self.lock:
            self.limit = int(limit)
            derived = [s for s in self.sessions.values() if s is not self.base]
            share = limit//(3 if derived else 2)
            if self.spatial:
                self.spatial.set_limit(share)
            if self.base:
                self.base.set_limit(share)
            for session in derived:
                session.set_limit(limit-2*share)

    def close(self):
        with self.lock:
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()
            if self.base:
                self.base.close()
                self.base = None
            if self.spatial:
                self.spatial.close()
                self.spatial = None
            self.inspection = None


def encode_cached(session, output, settings, cancel, progress):
    from dlss5tool.video_export import FFmpegVideoWriter, compose_hdr_frame, compose_output_frame
    import cv2
    import uuid
    destination = Path(output).resolve()
    if destination.exists():
        raise ValueError('输出文件已存在，不覆盖')
    metadata = session.wait_metadata(cancel)
    hdr = metadata['hdr_metadata']
    rate = Fraction(metadata['output_rate'])
    total = metadata['source_frames'] * session.multiplier
    if not destination.parent.is_dir() or destination.suffix.lower() not in ('.mp4', '.mov', '.mkv'):
        raise ValueError('请选择现有目录中的 MP4 / MOV / MKV 输出文件')
    disk_need = Path(session.source).stat().st_size * session.multiplier * int(session.settings.get('super_resolution_scale', 1))**2 * 3
    if shutil.disk_usage(destination.parent).free < disk_need + 15*1024**3:
        raise ValueError('输出盘余量不足预计峰值＋15 GiB')
    hit = calculated = 0
    temporary = destination.with_name(f'.{destination.stem}.cache-{uuid.uuid4().hex}{destination.suffix}')
    writer = None
    success = False
    computed_before = session.snapshot()['computed']
    try:
        writer = FFmpegVideoWriter(temporary, metadata['width'], metadata['height'], float(rate),
            audio_source=session.source, use_nvenc=True, hdr_metadata=hdr,
            nvenc_preset=settings.get('nvenc_preset', 'p5'), rate_control=settings.get('rate_control', 'quality'),
            quality_profile=settings.get('quality_profile', 'high'), video_bitrate_mbps=settings.get('video_bitrate_mbps', 20))
        for i in range(total):
            with session.condition:
                cached = i in session.cache
            processed, reference = session.wait(i, cancel)
            check_cancel(cancel)
            if cached:
                hit += 1
            else:
                calculated += 1
            view = int(settings.get('output_view', 0))
            if hdr:
                result = compose_hdr_frame(reference, processed, view=view, mix=1, profile=hdr['profile']) if view else processed
            else:
                result = cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR)
                if view:
                    result = compose_output_frame(cv2.cvtColor(reference, cv2.COLOR_RGBA2BGR), result, view=view, mix=1)
            writer.write(result)
            from dlss5tool.i18n import tr
            progress(tr('status.shared_export', hit=hit, done=i+1, total=total), (i+1)/total)
        writer.finish()
        check_cancel(cancel)
        os.rename(temporary, destination)
        success = True
        cuts = len((session.metadata or {}).get('scene_cuts', ()))
        return {**metadata, 'status': 'complete', 'real_frames': metadata['source_frames'],
                'generated_frames': (session.result or {}).get('generated_frames',
                    (metadata['source_frames']-1-cuts)*(session.multiplier-1)),
                'cut_holds': cuts*(session.multiplier-1), 'endpoint_holds': session.multiplier-1,
                'cache_hits': hit, 'cache_misses': calculated,
                'new_frames': session.snapshot()['computed']-computed_before, 'output': str(destination)}
    finally:
        if not success:
            session.suspend()
        if writer and not success:
            writer.abort()
        if temporary.exists():
            temporary.unlink()
