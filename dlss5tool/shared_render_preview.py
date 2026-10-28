"""Tk adapter for the full-quality SR/FG render session (no GPU waits on Tk)."""
import threading
import time
import math
from fractions import Fraction

import cv2
import numpy as np

from dlss5tool.render_cache import RenderCache, render_identity, file_identity
from dlss5tool.video_export import compose_hdr_frame, compose_output_frame, compose_sdr_rgba, tone_map_hdr_preview
from dlss5tool.i18n import tr
from dlss5tool.image_sequence import open_capture
from dlss5tool.gpu_decode import decode_options
from dlss5tool.preview_feedback import PreviewFeedback
from dlss5tool.preview_jobs import LatestPreviewJob
from dlss5tool.deferred_error import ErrorSnapshot
from dlss5tool.dlss_error_guidance import format_error, runtime_guidance


class SharedRenderPreview(PreviewFeedback):
    def _shared_report_error(self, error):
        text = str(error)
        display = format_error(text)
        self.set_status(tr('status.shared_failed', error=display.splitlines()[0]))
        if getattr(self, '_shared_last_error', None) != text:
            # Commit before a modal dialog pumps Tk callbacks.
            self._shared_last_error = text
            self.logln('[预览] ' + display)
            if runtime_guidance(text):
                self._show_dlss_error(text)

    def _shared_wait_status(self, stage):
        key = (self.video, getattr(self, '_shared_revision', 0),
               getattr(self, '_shared_output_index', getattr(self, '_frame', 0)))
        detail = getattr(self, '_shared_progress_detail', None)
        if detail and detail[0] == getattr(self, '_shared_progress_token', None):
            stage += '\n' + detail[1]
        self._show_preview_wait(stage, key=key)

    def _shared_bind_progress(self, session):
        key = (session, getattr(self, '_shared_revision', 0))
        if getattr(self, '_shared_progress_binding', None) == key:
            return
        self._shared_progress_binding = key
        token = self._shared_progress_token = object()
        self._shared_progress_detail = None
        def progress(message, fraction=0, counts=None):
            # Worker threads publish Python data only. Tk is updated by polling.
            if token is self._shared_progress_token:
                detail = str(message)
                if counts and counts[1]:
                    detail += f' ({int(counts[0])}/{int(counts[1])})'
                self._shared_progress_detail = (token, detail)
        session.set_progress(progress)

    def _preview_auto_prerender(self):
        settings = getattr(self, '_preview_runtime_settings', None)
        if settings is not None:
            return bool(settings.get('preview_auto_prerender', False))
        variable = getattr(self, '_preview_settings', {}).get('v_auto_prerender')
        if variable is not None:
            return bool(variable.get())
        return bool(getattr(self, '_saved_settings', {}).get('preview_auto_prerender', False))

    def _preview_effect_enabled(self, effect):
        key = 'preview_' + effect
        variable = getattr(self, '_export_settings', {}).get('v_' + key)
        if variable is not None:
            return bool(variable.get())
        return bool(getattr(self, '_saved_settings', {}).get(key, False))

    def _preview_effect_settings(self):
        config = (dict(self._collect_export_settings()) if hasattr(self, '_export_settings') else
                  {'super_resolution_scale': 1, 'frame_generation_multiplier': 1})
        if not self._preview_effect_enabled('super_resolution'):
            config['super_resolution_scale'] = 1
        if not self._preview_effect_enabled('frame_generation'):
            config['frame_generation_multiplier'] = 1
        return config

    def _on_effect_preview_change(self):
        if hasattr(self, '_export_preview_key'):
            self._last_export_preview_key = self._export_preview_key()
        if getattr(self, '_exporting', False) or getattr(self, '_queue_running', False):
            self._schedule_settings_save()
            return
        self.pause()
        self._clear_preview_wait()
        self._shared_progress_token = None
        self._shared_progress_binding = None
        self._shared_revision = getattr(self, '_shared_revision', 0)+1
        self._shared_output_index = self._frame*self._preview_effect_settings()['frame_generation_multiplier']
        self._shared_clock = None
        self._shared_painted_position = None
        if self._uses_shared_render():
            self._dlss_pending = True
        self._freeze_preview_cache(resume_ms=None)
        self._cache_clear()
        self._schedule_settings_save()
        if self.video:
            self.display_view(quality='fast')
            if not self._uses_shared_render():
                self._schedule_preview_cache_resume()

    def _shared_feedback(self):
        """Acknowledge now; submit only after the gesture settles."""
        self._shared_revision = getattr(self, '_shared_revision', 0)+1
        self._shared_not_before = time.perf_counter()+.12
        self._shared_last_error = None
        self._shared_error_key = None
        self._shared_progress_token = None
        self._shared_progress_binding = None
        self._show_preview_wait(tr('status.shared_updating'),
                                key=(self.video, self._shared_revision))
        # Never compose a full-resolution image in a slider/spinbox callback.
        # Keep the last painted frame until the settled request is ready.
        self._cancel_after('_shared_preview_after')
        self._shared_preview_after = self.root.after(16, self._shared_poll)

    def _shared_interim(self):
        """Only display matching full-quality real frames, not stale effects."""
        manager = getattr(self, '_render_cache_manager', None)
        base = manager.base if manager else None
        if not base or base.closed or not base.metadata:
            return False
        config = self._shared_config()
        from dlss5tool.appearance import raw_settings
        expected = {**raw_settings(config), 'frame_generation_multiplier': 1, 'output_mix': 1, 'output_size': None}
        if base.key != render_identity(self.video, expected):
            return False
        multiplier = config['frame_generation_multiplier']
        if getattr(self, '_shared_output_index', self._frame*multiplier) % multiplier:
            return False
        pair = base.peek(self._frame)
        if pair is None:
            return False
        processed, reference = pair
        mix_source = getattr(pair, 'mix_source', reference)
        meta = base.metadata.get('hdr_metadata')
        from dlss5tool.appearance import preflight
        try:
            preflight(config, hdr=bool(meta))
        except ValueError:
            # The normal asynchronous display path reports the preflight error.
            # Never raise from the immediate slider-feedback Tk callback.
            return False
        def compose():
            if meta:
                result = compose_hdr_frame(mix_source, processed, mix=config['output_mix'], profile=meta['profile'], settings=config)
            else:
                result = compose_sdr_rgba(mix_source,processed,mix=config['output_mix'],settings=config)
            original = reference
            size = config.get('output_size')
            if size and (result.shape[1], result.shape[0]) != tuple(size):
                def resize(pixels):
                    work = pixels.astype(np.float32) if pixels.dtype == np.float16 else pixels
                    return cv2.resize(work, tuple(size), interpolation=cv2.INTER_LANCZOS4).astype(pixels.dtype)
                result, original = resize(result), resize(original)
            return result, original
        job = getattr(self, '_shared_interim_job', None)
        if job is None:
            from dlss5tool.residual_composition import release_native_composer
            job = self._shared_interim_job = LatestPreviewJob(
                'preview-interim', persistent=True, cleanup=release_native_composer)
        key = (base, self._frame, getattr(self, '_shared_revision', 0), render_identity(self.video, config))
        result = job.request(key, compose)
        if result is None:
            return False
        self._shared_display_config = config
        paint_key = (key, self._hold_original, self.view_var.get(), self._canvas_size(),
                     getattr(self, 'canvas', None), getattr(self, 'split_x', .5),
                     getattr(self, '_preview_zoom', 1), getattr(self, '_preview_pan_x', .5),
                     getattr(self, '_preview_pan_y', .5), getattr(self, 'compare_layout', None).get()
                     if getattr(self, 'compare_layout', None) is not None else None)
        if paint_key != getattr(self, '_shared_interim_painted', None):
            self._shared_paint(base, result, self._frame, waiting=True)
            self._shared_interim_painted = paint_key
        return True

    def _shared_source_preview(self):
        """Independent one-frame decode, not gated on timestamp scan or NGX."""
        position = (file_identity(self.video), int(self._frame))
        original = self._hold_original or self.view_var.get() == 'original'
        if not original and getattr(self, '_shared_painted_position', None) == position:
            return False
        selected = 'original' if original else self.view_var.get()
        if (selected == 'compare'
                and getattr(self, '_split_orig', None) is not None
                and getattr(self, '_split_frame', -1) == int(self._frame)):
            self._present_shared_source(self._split_orig)
            return True
        error = getattr(self, '_shared_source_error', None)
        if error and error[0] == position:
            self.set_status(error[1])
            return False
        result = getattr(self, '_shared_source_result', None)
        if result and result[0] == position:
            if result[1] is not None:
                self._present_shared_source(result[1])
                return True
            return False
        worker = getattr(self, '_shared_source_thread', None)
        if worker and worker.is_alive():
            return False
        source, frame = str(self.video), int(self._frame)
        color = dict(getattr(self, '_video_color_info', None) or {})
        policy = self._decode_settings() if hasattr(self, '_decode_settings') else {}
        decode_revision = getattr(self, '_decode_revision', 0)
        def decode():
            cap = None
            try:
                cap = open_capture(source, **decode_options(policy))
                if frame:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
                ok, image = cap.read()
                if ok and color.get('is_hdr'):
                    image = tone_map_hdr_preview(image, color)
                if decode_revision == getattr(self, '_decode_revision', 0):
                    self._shared_source_result = (position, image if ok else None)
            except Exception as error:
                if decode_revision == getattr(self, '_decode_revision', 0):
                    self._shared_source_result = (position, None)
                    self._shared_source_error = (position, str(error))
            finally:
                if cap is not None:
                    cap.release()
        self._shared_source_thread = threading.Thread(target=decode, name='preview-first-frame', daemon=True)
        self._shared_source_thread.start()
        return False

    def _present_shared_source(self, image):
        """Paint the waiting source frame without clearing compare-mode chrome."""
        selected = 'original' if getattr(self, '_hold_original', False) else (
            self.view_var.get() if getattr(self, 'view_var', None) else ''
        )
        size = self._canvas_size()
        paint_key = (self.video, self._frame, selected, id(image),
                     id(getattr(self, '_split_dlss', None)), getattr(self, '_dlss_pending', False),
                     size, getattr(self, '_preview_zoom', 1),
                     getattr(self, '_preview_pan_x', .5), getattr(self, '_preview_pan_y', .5),
                     getattr(self, 'canvas', None), getattr(self, 'split_x', .5),
                     getattr(self, '_shared_revision', 0))
        if paint_key == getattr(self, '_shared_source_paint_key', None):
            return
        self._shared_source_paint_key = paint_key
        if selected == 'compare' and hasattr(self, '_blit_play_split'):
            same_frame = getattr(self, '_split_frame', -1) == int(self._frame)
            processed = getattr(self, '_split_dlss', None) if same_frame else None
            pending = processed is None or bool(getattr(self, '_dlss_pending', False))
            self._blit_play_split(
                image, processed, *self._canvas_size(), pending=pending,
            )
            return
        self._draw_fit(image, *self._canvas_size(), badge=tr('status.shared_source'))

    def _uses_shared_render(self):
        if (not getattr(self, 'video', None) or getattr(self, '_is_image', False)
                or getattr(self, '_guidance_context', False)
                or not hasattr(self, '_export_settings')):
            return False
        config = self._preview_effect_settings()
        # HDR must be composed in linear light before preview tone mapping,
        # including NR-only playback. The legacy preview is already SDR BGR.
        hdr = bool(config.get('hdr_mode') and (getattr(self, '_video_color_info', None) or {}).get('is_hdr'))
        return hdr or config['super_resolution_scale'] > 1 or config['frame_generation_multiplier'] > 1

    def _shared_config(self):
        from dlss5tool.gui import _resolve_output_size
        config = {**self._collect_settings(), **self._preview_effect_settings()}
        w, h = self._source_size()
        size = _resolve_output_size(w, h, config['output_resolution'],
            config['custom_output_width'], config['custom_output_height'])
        config['output_size'] = size if config['super_resolution_scale'] == 1 and size != (w, h) else None
        return config

    def _shared_manager(self):
        retiring = getattr(self, '_shared_retiring', None)
        if ((retiring and retiring.is_alive()) or
                getattr(self, '_shared_retire_pending', None)):
            raise RuntimeError(getattr(self, '_shared_retire_error', None) or
                               '上一缓存处理尚未退出，请稍后重试或重新清理缓存')
        manager = getattr(self, '_render_cache_manager', None)
        if manager is None:
            manager = RenderCache(self._preview_cache_bytes(), self._ensure_shared_cache_pool().name)
            self._render_cache_manager = manager
        return manager

    def _shared_retire(self):
        """Detach immediately; close old GPU workers off the UI thread."""
        job = getattr(self, '_shared_interim_job', None)
        if job:
            job.invalidate()
        self._shared_interim_painted = None
        self._clear_preview_wait()
        self._shared_progress_token = None
        self._shared_progress_binding = None
        self._shared_source_paint_key = None
        self._cancel_after('_shared_preview_after')
        self._cancel_after('_scrub_after')
        self._cancel_after('_preview_cache_resume_after')
        manager = getattr(self, '_render_cache_manager', None)
        if not hasattr(self, '_shared_retire_lock'):
            self._shared_retire_lock = threading.RLock()
            self._shared_retire_pending = []
        with self._shared_retire_lock:
            if manager is not None:
                self._shared_retire_pending.append(manager)
            if not self._shared_retire_pending:
                return
        setup = getattr(self, '_shared_setup_thread', None)
        self._render_cache_manager = self._shared_session = None
        self._shared_setup_thread = None
        self._shared_setup_result = None
        self._shared_error_key = None
        previous = getattr(self, '_shared_retiring', None)
        self._shared_retire_error = None
        def close():
            try:
                if previous:
                    previous.join()
                if setup:
                    setup.join()
                with self._shared_retire_lock:
                    pending, self._shared_retire_pending = self._shared_retire_pending, []
                errors = []
                for retired in pending:
                    try:
                        retired.close()
                    except Exception as error:
                        with self._shared_retire_lock:
                            self._shared_retire_pending.append(retired)
                        errors.append(str(error))
                self._shared_retire_error = '; '.join(errors) or None
            except Exception as error:
                self._shared_retire_error = str(error)
        thread = threading.Thread(target=close, name='close-render-cache', daemon=True)
        self._shared_retiring = thread
        try:
            thread.start()
        except Exception as error:
            self._shared_retiring = previous
            self._shared_retire_error = str(error)

    def _shared_ready(self):
        if hasattr(self, '_local_gpu_allowed') and not self._local_gpu_allowed():
            return None
        if time.perf_counter() < getattr(self, '_shared_not_before', 0):
            return None
        config = self._shared_config()
        key = render_identity(self.video, config)
        self._shared_display_config = config
        retiring = getattr(self, '_shared_retiring', None)
        if retiring and retiring.is_alive():
            return None
        if getattr(self, '_shared_retire_pending', None):
            raise RuntimeError(self._shared_retire_error or '缓存处理尚未退出')
        session = getattr(self, '_shared_session', None)
        if session and session.key == key and not session.closed:
            return session
        if getattr(self, '_shared_error_key', None) == key:
            raise RuntimeError(self._shared_error)
        setup = getattr(self, '_shared_setup_thread', None)
        if setup:
            if setup.is_alive():
                return None
            self._shared_setup_thread = None
            result_key, result, error, revision = self._shared_setup_result
            self._shared_setup_result = None
            if result_key == key and revision == getattr(self, '_shared_revision', 0):
                if error:
                    self._shared_error_key, self._shared_error = key, str(error)
                    raise error.exception() from None
                self._shared_session = result
                self._shared_output_index = self._frame * result.multiplier
                self._shared_clock = None
                return result
        # Retire the legacy preview worker before allocating the full pipeline.
        self._freeze_preview_cache(resume_ms=None)
        legacy = getattr(self, '_play_dlss_thread', None)
        if legacy and legacy.is_alive():
            return None
        self._cache_clear()
        manager = self._shared_manager()
        source = str(self.video)
        revision = getattr(self, '_shared_revision', 0)
        def setup_worker():
            try:
                self._close_super_resolution()
                self._close_live()
                result, error = manager.session(source, config,
                    is_current=lambda: revision == getattr(self, '_shared_revision', 0)), None
            except Exception as exc:
                result, error = None, ErrorSnapshot(exc)
            # This worker only writes Python state; never invokes Tk.
            self._shared_setup_result = (key, result, error, revision)
        self._shared_setup_thread = threading.Thread(target=setup_worker, name='prepare-render-cache', daemon=True)
        self._shared_setup_thread.start()
        return None

    def _shared_display(self):
        if (getattr(self, '_exporting', False) or getattr(self, '_clear_preview_pending', False)
                or getattr(self, '_workbench_mode', 'local') != 'local'
                or getattr(self, '_shared_closing', False)):
            return False
        if hasattr(self, '_local_gpu_allowed') and not self._local_gpu_allowed():
            self._shared_wait_status(tr('resources.preview_wait'))
            return False
        self._cancel_after('_shared_preview_after')
        try:
            if not self.playing or (self._hold_original and not getattr(self, '_shared_session', None)):
                self._shared_source_preview()
            session = self._shared_ready()
            if session is None:
                updating = getattr(self, '_shared_painted_position', None) == (file_identity(self.video), self._frame)
                self._shared_wait_status(tr('status.shared_updating' if updating else 'status.shared_preparing'))
                ready = False
            else:
                index = getattr(self, '_shared_output_index', self._frame*session.multiplier)
                index = min(max(0, index), max(0, self.nframes*session.multiplier-1))
                self._shared_output_index = index
                self._shared_bind_progress(session)
                if hasattr(session, 'set_prefetch_ahead'):
                    session.set_prefetch_ahead(self._preview_prefetch())
                session.set_prefetch(self.playing or self._preview_auto_prerender())
                pair = session.request(index)
                ready = pair is not None
                if ready:
                    job = getattr(self, '_shared_interim_job', None)
                    if job:
                        job.invalidate()
                    self._shared_interim_painted = None
                    self._shared_paint(session, pair, index,
                                       waiting=self.playing and getattr(self, '_buffering', False))
                else:
                    if not self.playing and session.multiplier > 1:
                        self._shared_interim()
                    self._shared_wait_status(tr('status.shared_buffering', frame=index+1))
            if not ready and not self.playing:
                self._shared_preview_after = self.root.after(40, self._shared_poll)
            return ready
        except Exception as error:
            self.pause()
            self._clear_preview_wait()
            PreviewFeedback._set_parameter_status(self, 'failed')
            self._cancel_after('_scrub_after')
            self._shared_report_error(error)
            return False

    def _shared_poll(self):
        self._shared_preview_after = None
        if self._uses_shared_render() and not self.playing:
            self._shared_display()

    def _shared_paint(self, session, pair, index, waiting=False):
        if not waiting:
            self._clear_preview_wait()
            PreviewFeedback._set_parameter_status(self, 'applied')
        processed, reference = pair
        config = self._shared_display_config
        meta = (session.metadata or {}).get('hdr_metadata')
        view = int(config.get('output_view', 0))
        if meta:
            rendered = compose_hdr_frame(reference, processed, view=view, mix=1, profile=meta['profile']) if view else processed
            def display(pixels):
                coded = np.ascontiguousarray(pixels[..., 2::-1])
                return tone_map_hdr_preview(coded, meta)
            original, result = display(reference), display(rendered)
        else:
            original = cv2.cvtColor(reference, cv2.COLOR_RGBA2BGR)
            result = cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR)
            if view:
                result = compose_output_frame(original, result, view=view, mix=1)
        self._frame = index // session.multiplier
        self._shared_painted_position = (file_identity(self.video), self._frame)
        if self.timeline.get() != self._frame:
            self.timeline.set(self._frame)
        self._sync_transport_labels()
        cw, ch = self._canvas_size()
        selected = 'original' if self._hold_original else self.view_var.get()
        self._dlss_pending = waiting
        if selected == 'compare':
            self._blit_play_split(original, result, cw, ch, pending=waiting)
        else:
            self._draw_fit(original if selected == 'original' else result, cw, ch)
        now = time.perf_counter()
        if now-getattr(self, '_shared_status_time', 0) > .2 or not self.playing:
            from dlss5tool.gui import _frame_ranges
            snapshot = session.snapshot()
            cache_bytes = snapshot['bytes']
            upstream = session.upstream
            while upstream:
                cache_bytes += upstream.snapshot()['bytes']
                upstream = upstream.upstream
            self.timeline.set_cache_ranges(_frame_ranges(snapshot['ranges']), [])
            status = tr('status.shared_ready', fps=f'{self.fps*session.multiplier:g}',
                frame=index+1, count=snapshot['frames'], mib=f"{cache_bytes/1024**2:.0f}")
            if not self.playing and not self._preview_auto_prerender():
                status += ' · ' + tr('status.auto_prerender_off')
            if not waiting:
                self.set_status(status)
            self._shared_status_time = now

    def _shared_play(self):
        multiplier = self._preview_effect_settings()['frame_generation_multiplier']
        if getattr(self, '_shared_output_index', 0) >= self.nframes*multiplier-1:
            self._frame = self._shared_output_index = 0
        self._shared_output_index = getattr(self, '_shared_output_index', self._frame*multiplier)
        self._cancel_after('_shared_preview_after')
        self._cancel_after('_play_after')
        self._audio.pause()
        self._shared_clock = None
        self.playing = True
        self._set_play_btn(True)
        self._shared_tick()

    def _shared_tick(self):
        if not self.playing:
            return
        if self._hold_original:
            from dlss5tool.gui import _alt_is_down
            if not _alt_is_down():
                self._set_hold_original(False)
        try:
            session = self._shared_ready()
            if session is None or not session.metadata:
                self._shared_display()
                if self.playing:
                    self._shared_buffer()
                return
            session.set_prefetch(True)
            if hasattr(session, 'set_prefetch_ahead'):
                session.set_prefetch_ahead(self._preview_prefetch())
            self._shared_bind_progress(session)
            fps = float(Fraction(session.metadata['output_rate']))
            total = session.metadata['source_frames']*session.multiplier
            index = min(self._shared_output_index, total-1)
            if getattr(self, '_shared_clock', None) is None:
                session.request(index)
                if not session.buffered(index, math.ceil(fps*.3)):
                    self._shared_display()
                    self._shared_buffer()
                    return
                self._shared_output_index = index
                self._shared_paint(session, session.peek(index), index)
                self._shared_clock_index = index
                self._shared_clock = time.perf_counter()
                self._buffering = False
                self._set_play_btn(True)
                self._shared_audio_running = bool(self._audio.play(index, fps))
            now = time.perf_counter()
            media_time = self._shared_clock_index/fps + now-self._shared_clock
            if (getattr(self, '_shared_audio_running', False) and self._audio.has_audio
                    and not self._audio.muted and self._audio.mode() == 'playing'):
                milliseconds = self._audio.position_ms()
                if milliseconds is not None:
                    media_time = max(self._shared_clock_index/fps, milliseconds/1000)
            target = min(total-1, max(index, int(math.floor(media_time*fps+1e-7))))
            pair = session.request(target)
            if pair is None:
                # Stop audio at the first cache hole, before waiting for inference.
                self._shared_output_index = target
                self._shared_buffer()
                return
            if target != index:
                # Late UI callbacks present the cached frame at the media clock;
                # they do not stretch time or repeatedly rewind the audio.
                self._shared_output_index = target
                self._shared_paint(session, pair, target)
            if media_time >= total/fps:
                self.pause()
                return
            delay = max(1, min(20, round(((target+1)/fps-media_time)*1000)))
            self._play_after = self.root.after(delay, self._shared_tick)
        except Exception as error:
            self.pause()
            self._clear_preview_wait()
            self._shared_report_error(error)

    def _shared_buffer(self):
        if not self.playing:
            return
        if not getattr(self, '_buffering', False):
            self._audio.pause()
        self._buffering = True
        self._shared_audio_running = False
        self._shared_clock = None
        self._set_play_btn(True)
        self._shared_wait_status(tr('status.shared_buffering', frame=self._shared_output_index+1))
        self._play_after = self.root.after(40, self._shared_tick)

    def _shared_advance(self, index):
        if self.playing:
            self._shared_output_index = index
            self._shared_tick()

    def _shared_step(self, delta):
        self.pause()
        multiplier = self._preview_effect_settings()['frame_generation_multiplier']
        index = getattr(self, '_shared_output_index', self._frame*multiplier)
        self._shared_output_index = min(max(0, index+int(delta)), self.nframes*multiplier-1)
        self._shared_display()
        self._focus_preview_host()
