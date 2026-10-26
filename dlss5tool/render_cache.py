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
from dlss5tool import appearance
from dlss5tool.deferred_error import ErrorSnapshot
from dlss5tool.memory_budget import cache_limit_bytes, current_pool, memory_scope, set_processing_priority
from dlss5tool.export_metrics import ExportTelemetry
from dlss5tool.parameter_contract import COMPOSITION_VERSION
from dlss5tool.image_sequence import ImageSequence, is_sequence, source_bytes, audio_source
from dlss5tool.frame_generation import Cancelled, check_cancel, export_video, runtime_files, inspect_source
from dlss5tool.gpu_decode import decode_options

# Shared preview should keep a short, responsive look-ahead.  Export still
# consumes frames sequentially and therefore does not need the old 120-frame
# burst, which could leave the producer waiting while the UI was composing NR.
# Keep the generic cache/export behavior backwards compatible.  The shared
# preview adapter supplies its smaller user-facing value explicitly.
DEFAULT_PREFETCH_AHEAD = 120
MAX_PREFETCH_AHEAD = 48
EXPORT_PREFETCH_AHEAD = 2

PIXEL_KEYS = (
    '_nr_bypass', 'decode_cpu_fallback',
    'nr_quality', 'nr_residual',
    'style', 'preset', 'intensity', 'local_tone', 'local_struct', 'skin_struct', 'use_auto_mask',
    'ui_correction', 'output_mix', 'guidance_mode', 'guidance_edge', 'guidance_flow_direction',
    'guidance_flow_backend', 'guidance_flow_grid', 'guidance_flow_updates', 'guidance_device',
    'guidance_depth_encoder', 'guidance_depth_profile', 'guidance_execution', 'render_gpu',
    'host_backend', 'host_tiled_mode', 'host_tile_width', 'host_tile_height', 'hdr_mode', 'output_size',
    'super_resolution_scale', 'frame_generation_multiplier', 'mods_directory',
)


class RenderPair(tuple):
    """Processed/source comparison pair, optionally retaining the NR mix input.

    SR-only pixels are a blending input, never the image labelled original.
    The optional third buffer is accounted for by the same cache budget.
    """
    def __new__(cls, processed, reference, mix_source=None):
        pair = super().__new__(cls, (processed, reference))
        pair.mix_source = reference if mix_source is None else mix_source
        return pair

    @property
    def nbytes(self):
        return sum(frame.nbytes for frame in
                   {id(frame): frame for frame in (*self, self.mix_source)}.values())


def _pair_bytes(pair):
    return pair.nbytes if isinstance(pair, RenderPair) else sum(x.nbytes for x in pair)


def _snapshot_pair(processed, reference, mix_source=None, *, source_pair=None):
    """Borrow only exact immutable snapshots owned by an upstream cache.

    Arbitrary renderer arrays and worker IPC buffers are always copied. A
    retained ndarray keeps its storage alive even after upstream eviction.
    """
    owned = {id(frame): frame for frame in (*source_pair, source_pair.mix_source)} \
        if isinstance(source_pair, RenderPair) else {}
    snapshots, copied, borrowed = {}, 0, 0
    def snapshot(frame):
        nonlocal copied, borrowed
        key = id(frame)
        if key not in snapshots:
            if owned.get(key) is frame and not frame.flags.writeable and frame.flags.c_contiguous:
                result = frame
                borrowed += frame.nbytes
            else:
                result = np.array(frame, copy=True)
                result.flags.writeable = False
                copied += result.nbytes
            snapshots[key] = result
        return snapshots[key]
    a, b = snapshot(processed), snapshot(reference)
    blend = b if mix_source is None else snapshot(mix_source)
    return RenderPair(a, b, blend), copied, borrowed


def file_identity(path):
    path = Path(path).resolve()
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
    except OSError:
        return str(path), None


def render_identity(source, settings):
    if settings.get('_render_stage') == 'spatial':
        payload = [2, file_identity(source), {k: settings.get(k) for k in
            ('hdr_mode', 'super_resolution_scale', 'render_gpu', 'decode_cpu_fallback')},
            [file_identity(paths.runtime_root()/name) for name in ('vsr_host.dll', 'nvngx_vsr.dll',
                'resident-hdr/manifest.json','vsr-cuda/manifest.json')]]
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
    for name in ('dlssnr_host_v2.dll', 'nvngx_dlssnr.dll', 'vsr_host.dll', 'nvngx_vsr.dll',
                 'resident-hdr/manifest.json', 'resident-sr/manifest.json', 'vsr-cuda/manifest.json'):
        dependencies.append(file_identity(paths.runtime_root() / name))
    for key, value in sorted(settings.items()):
        if (isinstance(value, str) and value.strip() and not key.startswith('guidance_')
                and ('weights' in key or key.endswith('_path'))):
            dependencies.append(file_identity(value))
    pixel['dlss_runtime'] = settings.get('dlss_runtime', '')
    pixel.update(appearance.settings(settings))
    pixel['_nr_bypass'] = settings.get('_nr_bypass')
    # Raw NR survives a composition implementation revision. A neutral public
    # identity still aliases its base session (used by interim previews).
    composition_version = COMPOSITION_VERSION if appearance.active(settings) or settings.get('output_mix', 1) != 1 else 0
    payload = [7, composition_version, file_identity(source), pixel, dependencies]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class RenderSession:
    def __init__(self, source, settings, limit_bytes, *, pool=None, renderer=None):
        self.source = str(Path(source).resolve())
        self.settings = dict(settings, **appearance.settings(settings))
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
        self.prefetch = True
        self.prefetch_ahead = DEFAULT_PREFETCH_AHEAD
        self.sequential_export = False
        self.upstream = None
        self.progress = None
        self.thread = None
        self.stop = threading.Event()
        self.hits = self.new_frames = self.replayed_frames = 0
        self.telemetry = ExportTelemetry()
        self.snapshot_copied_bytes = self.snapshot_borrowed_bytes = 0
        if self.pool is None:
            root=current_pool()
            if root is not None:
                from dlss5tool.shared_cache_budget import SharedCacheBudget
                self.pool=SharedCacheBudget(root.name)
        if self.pool:
            self.settings['guidance_cache_pool']=self.pool.name
            self.pool.set_reclaimer(self._reclaim)

    def _reclaim(self):
        if not self.condition.acquire(blocking=False):return False
        try:
            if not self.closed:self._trim(protect_target=True)
            self.condition.notify_all()
        finally:self.condition.release()
        return True

    @property
    def error(self):
        # Never let a consumer attach its traceback to persistent cache state.
        return self._error.exception() if self._error is not None else None

    @error.setter
    def error(self, error):
        self._error = (error if isinstance(error, ErrorSnapshot) else
                       ErrorSnapshot(error) if error is not None else None)

    def set_progress(self, progress):
        self.progress = progress
        if self.upstream:
            self.upstream.set_progress(progress)

    def _report_progress(self, *args, **kwargs):
        # Sessions survive parameter/preview changes and may later be exported.
        # Resolve the current subscriber rather than capture an old callback.
        progress = self.progress
        if progress:
            return progress(*args, **kwargs)

    def _allowance(self):
        if self.pool is None:
            return cache_limit_bytes(self.limit, self.bytes)
        with self.pool.locked():
            return min(self.limit, self.pool.allowance_locked(respect_demand=True))

    def _publish(self):
        if self.pool is not None:
            with self.pool.locked():
                self.pool.publish_locked(self.bytes)

    def _trim(self, needed=0, *, protect_target=False):
        allowance = self._allowance()
        while self.cache and self.bytes + needed > allowance:
            # Retain the neighbourhood currently being consumed, not the most
            # recent frame produced during a seek-history replay.
            candidates=[i for i in self.cache if not protect_target or i!=self.target]
            if not candidates:break
            # An export consumes increasing indices. Retire consumed history
            # before lookahead, otherwise a budget change can discard the next
            # encoded frame and force a complete temporal replay.
            consumed = [i for i in candidates if i < self.target] if self.sequential_export else []
            key = min(consumed) if consumed else max(candidates, key=lambda i: abs(i-self.target))
            self.bytes -= _pair_bytes(self.cache.pop(key))
        self._publish()
        return allowance

    def set_limit(self, limit):
        with self.condition:
            self.limit = int(limit)
            self._trim()
            self.condition.notify_all()

    def set_prefetch(self, enabled):
        """Control idle fill-ahead throughout the pipeline without discarding pixels."""
        with self.condition:
            self.prefetch = bool(enabled)
            self.condition.notify_all()
        if self.upstream:
            self.upstream.set_prefetch(enabled)

    def set_prefetch_ahead(self, frames):
        """Bound fill-ahead independently from the on/off prefetch policy."""
        try:
            value = int(frames)
        except (TypeError, ValueError):
            value = DEFAULT_PREFETCH_AHEAD
        value = max(2, min(MAX_PREFETCH_AHEAD, value))
        with self.condition:
            self.prefetch_ahead = value
            self.condition.notify_all()
        if self.upstream:
            self.upstream.set_prefetch_ahead(value)

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
            with self.condition:
                # Keep the miss and wait atomic. A faster upstream can publish
                # between them; losing that notification adds a 50 ms poll to
                # a frame that is already ready.
                value = self.request(index)
                if value is not None:
                    return value
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
        with memory_scope(self.pool):
            self._produce_scoped()

    def _produce_scoped(self):
        while True:
            with self.condition:
                if self.closed:
                    return
                self.stop = threading.Event()
                stop = self.stop
                replaying = self.restart
                known_scene_cuts = tuple((self.metadata or {}).get('scene_cuts', ())) if replaying else ()
                self.restart = False
                self.produced = -1
                self.complete = False
            def metadata(value):
                with self.condition:
                    # Replay can stop at a cached tail before rediscovering its
                    # cuts. Retain known metadata for those immutable pixels.
                    if known_scene_cuts and 'scene_cuts' in value:
                        value = {**value, 'scene_cuts': tuple(sorted(
                            set(known_scene_cuts).union(value['scene_cuts'])))}
                    self.metadata = value
                    self.condition.notify_all()
            def gate(source_index):
                set_processing_priority(source_index <= (self.target+self.multiplier-1)//self.multiplier)
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
                        if not self.prefetch or (self.pool and self.pool.under_pressure):
                            # Real frames need no neighbour. Interpolated frames
                            # require the next real frame as their right endpoint.
                            last_source = (self.target+self.multiplier-1)//self.multiplier
                            if source_index <= last_source:
                                break
                            self.condition.wait(.1)
                            continue
                        # One extra real frame supplies the right endpoint.
                        capacity = max(2*self.multiplier, self._allowance() // max(self.frame_bytes, 1))
                        ahead = min(capacity-1, max(2*self.multiplier, self.prefetch_ahead))
                        # The preview horizon has an extra real endpoint. The
                        # export's small window already includes that endpoint.
                        endpoint = 0 if self.sequential_export else self.multiplier
                        if not self.frame_bytes or source_index*self.multiplier <= self.target+ahead+endpoint:
                            break
                        self.condition.wait(.1)
                    check_cancel(stop)
                    return self.target
            def skipped(index):
                check_cancel(stop)
                with self.condition:
                    self.new_frames += 1
                    if replaying and index < self.target:
                        self.replayed_frames += 1
                    self.produced = index
                    self.condition.notify_all()
            def sink(index, processed, reference, *, mix_source=None, source_pair=None):
                check_cancel(stop)
                if reference is None:
                    reference = processed
                size = sum(frame.nbytes for frame in {id(f): f for f in
                    (processed, reference, mix_source) if f is not None}.values())
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
                            raise RuntimeError('缓存预算或系统内存余量不足一个全精度帧对，请释放内存或调整缓存预算')
                        others = [i for i in self.cache if i < self.target]
                        if others:
                            victim = max(others, key=lambda i: abs(i-self.target))
                            self.bytes -= _pair_bytes(self.cache.pop(victim))
                            self._publish()
                            continue
                        self.condition.wait(.05)
                        check_cancel(stop)
                    old = self.cache.pop(index, None)
                    if old:
                        self.bytes -= _pair_bytes(old)
                    allowance = self._trim(size)
                    if size > allowance:
                        raise RuntimeError('缓存预算或系统内存余量不足一个全精度帧对，请释放内存或调整缓存预算；未降低分辨率')
                    with self.telemetry.measure('cache_snapshot'):
                        pair, copied, borrowed = _snapshot_pair(
                            processed, reference, mix_source, source_pair=source_pair)
                    self.cache[index] = pair
                    self.snapshot_copied_bytes += copied
                    self.snapshot_borrowed_bytes += borrowed
                    self.produced = index
                    self.bytes += size
                    self._publish()
                    self.condition.notify_all()
            sink.accepts_source_pair = True
            sink.skip = skipped
            try:
                result = self.renderer(self.source, None, multiplier=self.multiplier,
                    scale=int(self.settings.get('super_resolution_scale', 1)), enhance=True,
                    settings=self.settings, cancel=stop, frame_sink=sink, render_gate=gate,
                    metadata_sink=metadata, progress=self._report_progress, telemetry=self.telemetry)
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
                    'snapshot_copied_bytes': self.snapshot_copied_bytes,
                    'snapshot_borrowed_bytes': self.snapshot_borrowed_bytes,
                    'performance': self.telemetry.snapshot(),
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
            self.error = None
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

    def inspect(self, source, cancel, progress=None, *, allow_cpu_fallback=False):
        identity = (file_identity(source), allow_cpu_fallback)
        if self.inspection is None or self.inspection[0] != identity:
            result = inspect_source(source, cancel, progress=progress, allow_vfr=True,
                                    **({'allow_cpu_fallback': True} if allow_cpu_fallback else {}))
            check_cancel(cancel)
            self.inspection = (identity, result)
        return self.inspection[1]

    def has_complete_spatial(self, source, settings):
        """Prefer already cached SR pixels over starting another full producer."""
        values = {k: settings[k] for k in ('hdr_mode','super_resolution_scale','render_gpu','decode_cpu_fallback') if k in settings}
        values.update(frame_generation_multiplier=1,guidance_mode=0,output_size=None,_render_stage='spatial')
        key = render_identity(source, values)
        with self.lock:
            device = self.base
            desired_base={**appearance.raw_settings(settings),'frame_generation_multiplier':1,
                          'output_mix':1,'output_size':None,'_render_stage':'base'}
            if (getattr(device, 'device_preview', False) and device.key == render_identity(source, desired_base)
                    and device.multiplier == 1 and not device.closed and not device.error):
                with device.condition:
                    count = (device.metadata or {}).get('source_frames', 0)
                    if count and all(index in device.cache for index in range(count)):
                        return True
            session = self.spatial
            if session is None or session.key != key or session.closed or session.error:
                return False
            with session.condition:
                count = (session.metadata or {}).get('source_frames', 0)
                return count > 0 and len(session.cache) >= count and all(index in session.cache for index in range(count))

    def session(self, source, settings, is_current=None):
        def latest():
            if is_current and not is_current():
                raise Cancelled('已被更新的参数替代')
        key = render_identity(source, settings)
        with self.lock:
            latest()
            current = self.sessions.get(key)
            if current is not None and not current.closed and not current.error:
                self.sessions.move_to_end(key)
                return current
            if current is not None and current.error and not current.closed:
                # A failed worker must never be handed back to a later preview
                # or export request.  Retire it before rebuilding the same key.
                current.close()
            base_settings = {**appearance.raw_settings(settings), 'frame_generation_multiplier':1,
                             'output_mix':1, 'output_size':None, '_render_stage':'base'}
            base_key = render_identity(source, base_settings)
            spatial_settings = {k:settings[k] for k in ('hdr_mode','super_resolution_scale','render_gpu','decode_cpu_fallback') if k in settings}
            spatial_settings.update(frame_generation_multiplier=1,guidance_mode=0,output_size=None,_render_stage='spatial')
            spatial_key = render_identity(source,spatial_settings)
            reuse_device_base = bool(getattr(self.base,'device_preview',False) and self.base.key==base_key
                and not self.base.closed and not self.base.error)
            # GPU base snapshots retain raw SR alongside NR and the labelled
            # original. Seed those immutable SR pixels when NR parameters change.
            # Changing only FG continues the same base owner and temporal state.
            if getattr(self.base,'device_preview',False) and not reuse_device_base:
                old = self.base
                old_spatial = {k:old.settings[k] for k in ('hdr_mode','super_resolution_scale','render_gpu','decode_cpu_fallback') if k in old.settings}
                old_spatial.update(frame_generation_multiplier=1,guidance_mode=0,output_size=None,_render_stage='spatial')
                seeds=[];seed_metadata=None;seed_target=0
                if render_identity(old.source,old_spatial)==spatial_key:
                    with old.condition:
                        seeds=[(index,RenderPair(pair.mix_source,pair[1],pair.mix_source)) for index,pair in old.cache.items()]
                        seed_metadata=dict(old.metadata) if old.metadata else None
                        seed_target=old.target
                old.close();self.base=None
                if seeds:
                    from dlss5tool.resident_preview import render_preview
                    def seeded_spatial_renderer(*args,**kwargs):
                        kwargs['enhance']=False
                        return render_preview(*args,**kwargs,source_inspector=lambda *v,**o:self.inspect(*v,**o,**decode_options(spatial_settings)))
                    pool=None
                    if self.pool_name:
                        from dlss5tool.shared_cache_budget import SharedCacheBudget
                        pool=SharedCacheBudget(self.pool_name)
                    self.spatial=RenderSession(source,spatial_settings,self.limit//3,pool=pool,renderer=seeded_spatial_renderer)
                    with self.spatial.condition:
                        self.spatial.cache=OrderedDict(seeds)
                        self.spatial.bytes=sum(_pair_bytes(pair) for _,pair in seeds)
                        self.spatial.metadata=seed_metadata
                        self.spatial.produced=max(index for index,_ in seeds)
                        self.spatial.target=seed_target
                        count=(seed_metadata or {}).get('source_frames',0)
                        self.spatial.complete=bool(count and all(index in self.spatial.cache for index in range(count)))
                        self.spatial.restart=not self.spatial.complete
                        self.spatial.frame_bytes=_pair_bytes(seeds[0][1])
                        self.spatial._trim(protect_target=True)
            from dlss5tool.device_sr_plan import eligible_settings as device_settings
            from dlss5tool.resident_hdr_runtime import load_components as device_components
            components = device_components() if device_settings(base_settings) and self.spatial is None and not reuse_device_base else None
            if components is not None:
                self.close();latest()
                from dlss5tool.resident_preview import render_preview
                def device_renderer(*args,**kwargs):
                    return render_preview(*args,**kwargs,components=components,
                        source_inspector=lambda *v,**o:self.inspect(*v,**o,**decode_options(base_settings)))
                pool=None
                if self.pool_name:
                    from dlss5tool.shared_cache_budget import SharedCacheBudget
                    pool=SharedCacheBudget(self.pool_name)
                self.base=RenderSession(source,base_settings,self.limit,pool=pool,renderer=device_renderer)
                self.base.device_preview=True
                reuse_device_base=True
            # SR + NR and FG have separate identities. Changing only the FG
            # multiplier retires the FG worker, not the full-quality SR/NR cache.
            for old in self.sessions.values():
                if old is not self.base:
                    old.close()
            self.sessions.clear()
            latest()
            base_settings = {**appearance.raw_settings(settings), 'frame_generation_multiplier': 1, 'output_mix': 1, 'output_size': None,
                             '_render_stage': 'base'}
            # raw_settings retains source/native-zero execution identity;
            # re-enabling NR creates a fresh producer and temporal history.
            base_key = render_identity(source, base_settings)
            multiplier = int(settings.get('frame_generation_multiplier', 1))
            def lease():
                if self.pool_name:
                    from dlss5tool.shared_cache_budget import SharedCacheBudget
                    return SharedCacheBudget(self.pool_name)
                return None
            layers = 3 if multiplier > 1 or settings.get('output_mix', 1) != 1 or settings.get('output_size') or appearance.active(settings) else 2
            base_limit = self.limit//layers
            spatial_settings = {k: settings[k] for k in ('hdr_mode', 'super_resolution_scale', 'render_gpu', 'decode_cpu_fallback') if k in settings}
            spatial_settings.update(frame_generation_multiplier=1, guidance_mode=0, output_size=None,
                                    _render_stage='spatial')
            spatial_key = render_identity(source, spatial_settings)
            if not reuse_device_base and (self.spatial is None or self.spatial.key != spatial_key
                    or self.spatial.closed or self.spatial.error):
                if self.base:
                    self.base.close()
                    self.base = None
                if self.spatial:
                    self.spatial.close()
                latest()
                def spatial_renderer(*args, **kwargs):
                    kwargs['enhance'] = False
                    def inspect_policy(*values, **options):
                        return self.inspect(*values, **options, **decode_options(spatial_settings))
                    return export_video(*args, **kwargs, source_inspector=inspect_policy)
                self.spatial = RenderSession(source, spatial_settings, base_limit,
                                             pool=lease(), renderer=spatial_renderer)
            elif self.spatial is not None:
                self.spatial.set_limit(base_limit)
            if (self.base is None or self.base.key != base_key
                    or self.base.closed or self.base.error):
                if self.base:
                    self.base.close()
                latest()
                spatial = self.spatial
                def base_renderer(*args, **kwargs):
                    kwargs['enhance'] = base_settings['_nr_bypass'] != 'source'
                    return export_video(*args, **kwargs, input_session=spatial)
                self.base = RenderSession(source, base_settings, base_limit, pool=lease(), renderer=base_renderer)
                self.base.upstream = spatial
            else:
                self.base.set_limit(self.limit*2//3 if layers==3 else self.limit) if reuse_device_base else self.base.set_limit(base_limit)
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
            if getattr(self.base,'device_preview',False) and self.spatial is None:
                base_share=limit*2//3 if derived else limit
                self.base.set_limit(base_share)
                for session in derived:session.set_limit(limit-base_share)
                return
            share = limit//(3 if derived else 2)
            if self.spatial:
                self.spatial.set_limit(share)
            if self.base:
                self.base.set_limit(share)
            for session in derived:
                session.set_limit(limit-2*share)

    def close(self):
        with self.lock:
            seen, closed = set(), set()
            failure = None
            for session in [*self.sessions.values(), self.base, self.spatial]:
                if session is None or id(session) in seen:
                    continue
                seen.add(id(session))
                try:
                    session.close()
                except Exception as error:
                    if failure is None:
                        failure = ErrorSnapshot(error)
                else:
                    closed.add(id(session))
            for key, session in list(self.sessions.items()):
                if id(session) in closed:
                    del self.sessions[key]
            if id(self.base) in closed:
                self.base = None
            if id(self.spatial) in closed:
                self.spatial = None
            self.inspection = None
            if failure is not None:
                raise failure.exception()


def encode_cached(session, output, settings, cancel, progress, *, frame_progress=None,
                  performance_sink=None):
    # Pure SR needs only a short pipeline window. NR/FG retain their existing
    # overlap horizon; their temporal stages can otherwise starve one another.
    # Both paths retire consumed pixels first during sequential encoding.
    short_window = session.multiplier == 1 and appearance.enhancement_bypass(settings) == 'source'
    previous = []
    layer = session
    while isinstance(layer, RenderSession):
        with layer.condition:
            previous.append((layer, layer.prefetch_ahead, layer.sequential_export))
            if short_window:
                layer.prefetch_ahead = EXPORT_PREFETCH_AHEAD
            layer.sequential_export = True
            layer.condition.notify_all()
        layer = layer.upstream
    try:
        return _encode_cached(session, output, settings, cancel, progress,
                              frame_progress=frame_progress, performance_sink=performance_sink)
    finally:
        for layer, ahead, sequential in previous:
            with layer.condition:
                layer.prefetch_ahead = ahead
                layer.sequential_export = sequential
                layer.condition.notify_all()


def _encode_cached(session, output, settings, cancel, progress, *, frame_progress=None,
                   performance_sink=None):
    from dlss5tool.video_export import compose_hdr_frame, compose_output_frame
    from dlss5tool.gpu_export_runtime import create_video_writer as FFmpegVideoWriter
    import cv2
    import uuid
    destination = Path(output).resolve()
    if destination.exists():
        raise ValueError('输出文件已存在，不覆盖')
    sequence = ImageSequence.load(session.source) if is_sequence(session.source) else None
    if sequence:
        sequence.validate(lambda: check_cancel(cancel))

    # Cache rendering and final encoding are two parts of one export.  They
    # can overlap (the encoder consumes frames while the cache producer is
    # still running), so forwarding both raw fractions to the UI makes the
    # progress bar jump back to zero whenever encoding starts.  Keep one
    # monotonic fraction for each part and expose their combined 50/50
    # progress instead.  The lock is needed because render callbacks arrive
    # from the cache worker while encode callbacks run on this thread.
    progress_lock = threading.Lock()
    render_fraction = 0.0
    encode_fraction = 0.0

    def report_combined(stage, message, fraction):
        nonlocal render_fraction, encode_fraction
        try:
            value = max(0.0, min(float(fraction), 1.0))
        except (TypeError, ValueError):
            value = 0.0
        with progress_lock:
            if stage == 'render':
                render_fraction = max(render_fraction, value)
            else:
                encode_fraction = max(encode_fraction, value)
            combined = 0.5 * (render_fraction + encode_fraction)
        progress(message, combined)

    def report_render(message, fraction=0, counts=None):
        # Timestamp inspection also supplies counts, but it is only a small
        # preflight step.  Keep it within the first 5% of the render half so
        # its 100% completion does not make the actual frame render appear
        # complete before rendering has started.
        if counts and counts[1]:
            try:
                inspected = float(counts[0]) / float(counts[1])
            except (TypeError, ValueError, ZeroDivisionError):
                inspected = 0.0
            fraction = min(max(inspected, 0.0), 1.0) * 0.05
        report_combined('render', message, fraction)

    def report_encode(message, fraction=0, counts=None):
        report_combined('encode', message, fraction)

    session.set_prefetch(True)
    session.set_progress(report_render)
    telemetry = ExportTelemetry()
    with telemetry.measure('metadata_wait'):
        metadata = session.wait_metadata(cancel)
    hdr = metadata['hdr_metadata']
    rate = Fraction(metadata['output_rate'])
    total = metadata['source_frames'] * session.multiplier
    if not destination.parent.is_dir() or destination.suffix.lower() not in ('.mp4', '.mov', '.mkv'):
        raise ValueError('请选择现有目录中的 MP4 / MOV / MKV 输出文件')
    disk_need = source_bytes(session.source) * session.multiplier * int(session.settings.get('super_resolution_scale', 1))**2 * 3
    if shutil.disk_usage(destination.parent).free < disk_need + 15*1024**3:
        raise ValueError('输出盘余量不足预计峰值＋15 GiB')
    hit = calculated = 0
    if frame_progress:
        frame_progress(0, total)
    temporary = destination.with_name(f'.{destination.stem}.cache-{uuid.uuid4().hex}{destination.suffix}')
    writer = None
    success = False
    computed_before = session.snapshot()['computed']
    try:
        timeline = metadata.get('source_metadata') or {}
        frame_timestamps = timeline.get('_source_pts') if session.multiplier == 1 else None
        timeline_options = ({'frame_timestamps': frame_timestamps,
                             'timeline_end': timeline.get('_source_end')}
                            if frame_timestamps is not None else {})
        with telemetry.measure('encoder_init'):
            writer = FFmpegVideoWriter(temporary, metadata['width'], metadata['height'], float(rate),
                audio_source=audio_source(session.source), use_nvenc=True, hdr_metadata=hdr,
                nvenc_preset=settings.get('nvenc_preset', 'p5'), rate_control=settings.get('rate_control', 'quality'),
                quality_profile=settings.get('quality_profile', 'high'), video_bitrate_mbps=settings.get('video_bitrate_mbps', 20),cancel=cancel,
                **timeline_options)
        for i in range(total):
            if sequence:
                sequence.check_frame(i // session.multiplier)
            with session.condition:
                cached = i in session.cache
            with telemetry.measure('render_wait'):
                processed, reference = session.wait(i, cancel)
            check_cancel(cancel)
            if cached:
                hit += 1
            else:
                calculated += 1
            view = int(settings.get('output_view', 0))
            with telemetry.measure('encode_conversion'):
                if hdr:
                    result = compose_hdr_frame(reference, processed, view=view, mix=1, profile=hdr['profile']) if view else processed
                else:
                    result = cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR)
                    if view:
                        result = compose_output_frame(cv2.cvtColor(reference, cv2.COLOR_RGBA2BGR), result, view=view, mix=1)
            with telemetry.measure('encode_write'):
                writer.write(result)
            if frame_progress:
                frame_progress(i+1, total)
            from dlss5tool.i18n import tr
            report_encode(
                tr('status.shared_export', hit=hit, done=i+1, total=total),
                (i+1) / total,
            )
        with telemetry.measure('encode_finish'):
            writer.finish()
        check_cancel(cancel)
        if sequence:
            sequence.validate(lambda: check_cancel(cancel))
        os.rename(temporary, destination)
        success = True
        scene_cuts = tuple((session.metadata or {}).get('scene_cuts', ()))
        cuts = len(scene_cuts)
        return {**metadata, 'scene_cuts': scene_cuts, 'status': 'complete', 'real_frames': metadata['source_frames'],
                'generated_frames': (session.result or {}).get('generated_frames',
                    (metadata['source_frames']-1-cuts)*(session.multiplier-1)),
                'cut_holds': cuts*(session.multiplier-1), 'endpoint_holds': session.multiplier-1,
                'cache_hits': hit, 'cache_misses': calculated,
                'new_frames': session.snapshot()['computed']-computed_before, 'output': str(destination),
                'performance': telemetry.snapshot()}
    finally:
        if performance_sink:
            try:
                performance_sink(telemetry.snapshot())
            except Exception:
                pass  # Telemetry subscribers cannot bypass writer cleanup.
        if not success:
            session.suspend()
        if writer and not success:
            writer.abort()
        if temporary.exists():
            temporary.unlink()
