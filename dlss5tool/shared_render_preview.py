"""Tk adapter for the full-quality SR/FG render session (no GPU waits on Tk)."""
import threading
import time
import math
from fractions import Fraction

import cv2
import numpy as np

from dlss5tool.render_cache import RenderCache, render_identity, file_identity
from dlss5tool.video_export import compose_hdr_frame, compose_output_frame, tone_map_hdr_preview
from dlss5tool.i18n import tr


class SharedRenderPreview:
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
        if getattr(self, '_exporting', False) or getattr(self, '_queue_running', False):
            self._schedule_settings_save()
            return
        self.pause()
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
        self.set_status(tr('status.shared_updating'))
        self._draw_work_status(tr('status.shared_updating'))
        self._shared_interim()
        self._cancel_after('_shared_preview_after')
        self._shared_preview_after = self.root.after(16, self._shared_poll)

    def _shared_interim(self):
        """Only display matching full-quality real frames, not stale effects."""
        manager = getattr(self, '_render_cache_manager', None)
        base = manager.base if manager else None
        if not base or base.closed or not base.metadata:
            return False
        config = self._shared_config()
        expected = {**config, 'frame_generation_multiplier': 1, 'output_mix': 1, 'output_size': None}
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
        if meta:
            processed = compose_hdr_frame(mix_source, processed, mix=config['output_mix'], profile=meta['profile'])
        else:
            processed = cv2.cvtColor(compose_output_frame(cv2.cvtColor(mix_source, cv2.COLOR_RGBA2BGR),
                cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR), mix=config['output_mix']), cv2.COLOR_BGR2RGBA)
        self._shared_display_config = config
        size = config.get('output_size')
        if size and (processed.shape[1], processed.shape[0]) != tuple(size):
            def resize(pixels):
                work = pixels.astype(np.float32) if pixels.dtype == np.float16 else pixels
                return cv2.resize(work, tuple(size), interpolation=cv2.INTER_LANCZOS4).astype(pixels.dtype)
            processed, reference = resize(processed), resize(reference)
        self._shared_paint(base, (processed, reference), self._frame)
        self.set_status(tr('status.shared_updating'))
        self._draw_work_status(tr('status.shared_updating'))
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
        def decode():
            cap = cv2.VideoCapture(source)
            try:
                if frame:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame)
                ok, image = cap.read()
                if ok and color.get('is_hdr'):
                    image = tone_map_hdr_preview(image, color)
                self._shared_source_result = (position, image if ok else None)
            finally:
                cap.release()
        self._shared_source_thread = threading.Thread(target=decode, name='preview-first-frame', daemon=True)
        self._shared_source_thread.start()
        return False

    def _present_shared_source(self, image):
        """Paint the waiting source frame without clearing compare-mode chrome."""
        selected = 'original' if getattr(self, '_hold_original', False) else (
            self.view_var.get() if getattr(self, 'view_var', None) else ''
        )
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
        return config['super_resolution_scale'] > 1 or config['frame_generation_multiplier'] > 1

    def _shared_config(self):
        from dlss5tool.gui import _resolve_output_size
        config = {**self._collect_settings(), **self._preview_effect_settings()}
        w, h = self._source_size()
        size = _resolve_output_size(w, h, config['output_resolution'],
            config['custom_output_width'], config['custom_output_height'])
        config['output_size'] = size if config['super_resolution_scale'] == 1 and size != (w, h) else None
        return config

    def _shared_manager(self):
        manager = getattr(self, '_render_cache_manager', None)
        if manager is None:
            manager = RenderCache(self._preview_cache_bytes(), self._ensure_shared_cache_pool().name)
            self._render_cache_manager = manager
        return manager

    def _shared_retire(self):
        """Detach immediately; close old GPU workers off the UI thread."""
        self._cancel_after('_shared_preview_after')
        self._cancel_after('_scrub_after')
        self._cancel_after('_preview_cache_resume_after')
        manager = getattr(self, '_render_cache_manager', None)
        if manager is None:
            return
        setup = getattr(self, '_shared_setup_thread', None)
        self._render_cache_manager = self._shared_session = None
        self._shared_setup_thread = None
        self._shared_setup_result = None
        self._shared_error_key = None
        def close():
            if setup:
                setup.join()
            manager.close()
        thread = threading.Thread(target=close, name='close-render-cache', daemon=True)
        self._shared_retiring = thread
        thread.start()

    def _shared_ready(self):
        config = self._shared_config()
        key = render_identity(self.video, config)
        self._shared_display_config = config
        if time.perf_counter() < getattr(self, '_shared_not_before', 0):
            return None
        retiring = getattr(self, '_shared_retiring', None)
        if retiring and retiring.is_alive():
            return None
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
            if result_key == key and revision == getattr(self, '_shared_revision', 0):
                if error:
                    self._shared_error_key, self._shared_error = key, str(error)
                    raise error
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
                result, error = None, exc
            # This worker only writes Python state; never invokes Tk.
            self._shared_setup_result = (key, result, error, revision)
        self._shared_setup_thread = threading.Thread(target=setup_worker, name='prepare-render-cache', daemon=True)
        self._shared_setup_thread.start()
        return None

    def _shared_display(self):
        if (getattr(self, '_exporting', False) or getattr(self, '_clear_preview_pending', False)
                or getattr(self, '_shared_closing', False)):
            return False
        self._cancel_after('_shared_preview_after')
        try:
            if not self.playing or (self._hold_original and not getattr(self, '_shared_session', None)):
                self._shared_source_preview()
            session = self._shared_ready()
            if session is None:
                updating = getattr(self, '_shared_painted_position', None) == (file_identity(self.video), self._frame)
                self.set_status(tr('status.shared_updating' if updating else 'status.shared_preparing'))
                ready = False
            else:
                index = getattr(self, '_shared_output_index', self._frame*session.multiplier)
                index = min(max(0, index), max(0, self.nframes*session.multiplier-1))
                self._shared_output_index = index
                pair = session.request(index)
                ready = pair is not None
                if ready:
                    self._shared_paint(session, pair, index)
                else:
                    self.set_status(tr('status.shared_buffering', frame=index+1))
                    if not self.playing:
                        self._shared_interim()
            if not ready and not self.playing:
                self._shared_preview_after = self.root.after(40, self._shared_poll)
            return ready
        except Exception as error:
            self.pause()
            self._cancel_after('_scrub_after')
            self.set_status(tr('status.shared_failed', error=str(error).splitlines()[0]))
            if getattr(self, '_shared_last_error', None) != str(error):
                self.logln('[预览] ' + str(error))
                self._shared_last_error = str(error)
            return False

    def _shared_poll(self):
        self._shared_preview_after = None
        if self._uses_shared_render() and not self.playing:
            self._shared_display()

    def _shared_paint(self, session, pair, index):
        processed, reference = pair
        config = self._shared_display_config
        meta = (session.metadata or {}).get('hdr_metadata')
        view = int(config.get('output_view', 0))
        if meta:
            rendered = compose_hdr_frame(reference, processed, view=view, mix=1, profile=meta['profile']) if view else processed
            def display(pixels):
                coded = np.ascontiguousarray((np.clip(pixels[..., 2::-1], 0, 1)*255+.5).astype(np.uint8))
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
        self._dlss_pending = False
        if selected == 'compare':
            self._blit_play_split(original, result, cw, ch)
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
            self.set_status(tr('status.shared_ready', fps=f'{self.fps*session.multiplier:g}',
                frame=index+1, count=snapshot['frames'], mib=f"{cache_bytes/1024**2:.0f}"))
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
            self.set_status(tr('status.shared_failed', error=str(error).splitlines()[0]))
            self.logln('[预览] ' + str(error))

    def _shared_buffer(self):
        if not self.playing:
            return
        if not getattr(self, '_buffering', False):
            self._audio.pause()
        self._buffering = True
        self._shared_audio_running = False
        self._shared_clock = None
        self._set_play_btn(True)
        self.set_status(tr('status.shared_buffering', frame=self._shared_output_index+1))
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
