import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from dlss5tool import app_settings, gui
from dlss5tool.shared_render_preview import SharedRenderPreview


class PreviewToggleTests(unittest.TestCase):
    def setUp(self):
        self.app = SharedRenderPreview()
        self.values = {'preview_super_resolution': False, 'preview_frame_generation': False}
        self.app._export_settings = {'v_'+key: SimpleNamespace(get=lambda k=key: self.values[k]) for key in self.values}
        self.export = {'super_resolution_scale': 2, 'frame_generation_multiplier': 4}
        self.app._collect_export_settings = lambda: dict(self.export)
        self.app.video = 'source.mp4'
        self.app._is_image = False
        self.app._guidance_context = False

    def test_defaults_and_persistence_validation(self):
        saved = app_settings.validate({'super_resolution_scale': 2, 'frame_generation_multiplier': 4})
        self.assertTrue(saved['preview_super_resolution'])
        self.assertTrue(saved['preview_frame_generation'])
        saved = app_settings.validate({'preview_super_resolution': True, 'preview_frame_generation': False})
        self.assertTrue(saved['preview_super_resolution'])
        self.assertFalse(saved['preview_frame_generation'])

    def test_independent_switch_matrix_does_not_change_export(self):
        for sr, fg, expected in ((False, False, (1, 1)), (True, False, (2, 1)),
                                 (False, True, (1, 4)), (True, True, (2, 4))):
            with self.subTest(sr=sr, fg=fg):
                self.values.update(preview_super_resolution=sr, preview_frame_generation=fg)
                result = self.app._preview_effect_settings()
                self.assertEqual((result['super_resolution_scale'], result['frame_generation_multiplier']), expected)
                self.assertEqual(self.app._uses_shared_render(), sr or fg)
                self.assertEqual(self.app._collect_export_settings(), self.export)

    def test_disabled_preview_legacy_size_remains_source(self):
        app = gui.App.__new__(gui.App)
        app._source_kind = 'video'
        app._image_bgr = None
        app._export_settings = self.app._export_settings
        app._collect_export_settings = self.app._collect_export_settings
        self.assertEqual(app._super_resolution_scale(), 1)
        self.assertEqual(app._super_resolution_scale({'super_resolution_scale': 2}), 2,
                         'explicit export settings must still take effect')

    def test_toggle_during_export_does_not_retire_export_session(self):
        self.app._exporting = True
        self.app._schedule_settings_save = mock.Mock()
        self.app.pause = mock.Mock()
        self.app._on_effect_preview_change()
        self.app.pause.assert_not_called()

    def test_buffer_target_respects_small_cache_capacity(self):
        from dlss5tool.render_cache import RenderSession
        session = RenderSession('source', {}, 512)
        try:
            session.metadata = {'source_frames': 120}
            session.frame_bytes = session.bytes = 512
            session.cache[0] = (np.zeros((8, 8, 4), np.uint8),)*2
            self.assertTrue(session.buffered(0, 120), 'a one-frame budget must not wait forever for 120 frames')
        finally:
            session.close()


class LegacyPreviewCacheTests(unittest.TestCase):
    def make_app(self):
        app = gui.App.__new__(gui.App)
        app.video = 'source.mp4'
        app._source_kind, app._image_bgr = 'video', None
        app._video_color_info = {'is_hdr': False}
        app._export_settings = {
            'v_preview_super_resolution': SimpleNamespace(get=lambda: False),
            'v_preview_frame_generation': SimpleNamespace(get=lambda: False),
        }
        export = {'super_resolution_scale': 2, 'frame_generation_multiplier': 2,
                  'hdr_mode': False}
        app._collect_export_settings = lambda: dict(export)
        app._collect_settings = lambda: app_settings.validate(export)
        app._local_gpu_allowed = lambda: True
        app._preview_quality = lambda: 'original'
        app._preview_cache_bytes = app._available_frame_cache_bytes = lambda: 1024
        app._media_w, app._media_h = 8, 4
        app._active_preview_size = (8, 4)
        app._frame, app.nframes, app.fps = 0, 8, 1
        app.playing = app._exporting = app._hold_original = False
        app._preview_cache_frozen = False
        app._pre_rendering = app._dlss_pending = True
        app._prerender_frame_limit = 1
        app._prefetch_gen, app._prefetch_stop = 0, threading.Event()
        app._live_lock, app._cache_lock = threading.RLock(), threading.RLock()
        app._queued_preview_frames = set()
        app._source_frame_cache, app._dlss_frame_cache = {}, {}
        app._source_cache_bytes = app._dlss_cache_bytes = 0
        app._live_cache = app._last_shown_dlss = None
        app._preview_processed_frames, app._preview_process_t0 = 0, None
        app._evict_preview_cache_locked = mock.Mock()
        app.view_var = SimpleNamespace(get=lambda: 'compare')
        app.display_view = mock.Mock()
        return app, export

    def test_worker_result_is_presented_and_playback_buffer_becomes_ready(self):
        for scale in (2, 4):
            with self.subTest(export_scale=scale):
                app, export = self.make_app()
                export['super_resolution_scale'] = scale
                source = np.full((4, 8, 3), 40, np.uint8)
                app._source_cache_store(0, source)

                class FakeLive:
                    supports_async = False

                    def process(self, rgba, reset=False):
                        app._prefetch_stop.set()
                        return rgba + 5

                app._ensure_live = lambda *args: FakeLive()
                with mock.patch('dlss5tool.gui.threading.Thread') as worker:
                    self.assertTrue(app._start_prefetch())
                    arguments = worker.call_args.kwargs['args']
                app._preview_frame_queue.put_nowait((0, source))
                app._prefetch_job(*arguments)

                self.assertEqual(app._preview_processed_frames, 1)
                self.assertIsNone(app._preview_worker_error)
                self.assertTrue(app._present_current_cached_preview())
                app.display_view.assert_called_once_with(quality='full')
                np.testing.assert_array_equal(app._cached_dlss(0), source + 5)
                self.assertEqual(app._buffer_is_ready(), (True, 1, 1))
                self.assertEqual(app._collect_export_settings()['super_resolution_scale'], scale)

    def test_export_only_scale_change_preserves_ready_preview(self):
        app, export = self.make_app()
        pixels = np.ones((4, 8, 3), np.uint8)
        worker_settings = {**app._collect_settings(), 'super_resolution_scale': 1}
        app._cache_store(0, app._hash_settings_dict(worker_settings), pixels)
        for scale in (1, 2, 4):
            export['super_resolution_scale'] = scale
            self.assertIs(app._cached_dlss(0), pixels)

    def test_image_worker_keeps_image_upscale_without_video_preview_switch(self):
        app, _ = self.make_app()
        app._source_kind, app._image_bgr = 'image', np.zeros((4, 8, 3), np.uint8)
        with mock.patch('dlss5tool.gui.threading.Thread') as worker:
            self.assertTrue(app._start_prefetch(preview_size=(16, 8)))
            settings = worker.call_args.kwargs['args'][0]
        self.assertEqual(settings['super_resolution_scale'], 2)
        self.assertEqual(app._hash_settings_dict(settings), app._settings_hash())


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.app = SharedRenderPreview()
        app = self.app
        self.rate, self.total = 60, 330
        self.pixels = (np.zeros((2, 2, 4), np.uint8),)*2
        self.missing = set()
        self.session = SimpleNamespace(multiplier=2,
            set_progress=mock.Mock(),
            set_prefetch=mock.Mock(),
            metadata={'output_rate': '60', 'source_frames': 165},
            request=lambda i: None if i in self.missing else self.pixels,
            peek=lambda i: self.pixels, buffered=lambda i, n: True)
        app._shared_ready = lambda: self.session
        app._shared_output_index = 0
        app.video = 'source.mp4'
        app._draw_work_status = mock.Mock()
        app._shared_clock = None
        app._shared_clock_index = 0
        app._hold_original = False
        app.playing = True
        app._buffering = False
        app._shared_paint = mock.Mock()
        app._shared_display = mock.Mock()
        app._set_play_btn = mock.Mock()
        app.set_status = mock.Mock()
        app.logln = mock.Mock()
        app.root = SimpleNamespace(after=mock.Mock())
        self.ms = 0
        app._audio = SimpleNamespace(has_audio=True, muted=False, mode=lambda: 'playing',
            position_ms=lambda: self.ms, play=mock.Mock(return_value=True), pause=mock.Mock())
        app.pause = mock.Mock(side_effect=lambda: setattr(app, 'playing', False))

    def tick(self, now):
        with mock.patch('dlss5tool.shared_render_preview.time.perf_counter', return_value=now):
            self.app._shared_tick()

    def test_audio_is_master_and_late_paint_does_not_rewind_sound(self):
        self.tick(100)
        self.ms = 750
        self.tick(102)  # wall clock and audio deliberately disagree
        self.assertEqual(self.app._shared_output_index, 45)
        self.app._audio.play.assert_called_once_with(0, 60)
        self.app._audio.pause.assert_not_called()
        self.assertEqual(self.app._shared_paint.call_args.args[2], 45)

    def test_cache_hole_pauses_audio_and_resume_waits_for_window(self):
        self.tick(100)
        self.ms = 500
        self.missing.add(30)
        self.tick(100.5)
        self.assertTrue(self.app._buffering)
        self.assertIsNone(self.app._shared_clock)
        self.app._audio.pause.assert_called_once()
        self.session.buffered = lambda *_: False
        self.tick(101)
        self.app._audio.play.assert_called_once()
        self.missing.clear()
        self.session.buffered = lambda *_: True
        self.tick(102)
        self.assertFalse(self.app._buffering)
        self.assertEqual(self.app._audio.play.call_args.args, (30, 60))

    def test_no_audio_duration_is_not_multiplied(self):
        self.app._audio.has_audio = False
        self.tick(100)
        self.tick(105.5)
        self.assertEqual(self.app._shared_output_index, 329)
        self.assertFalse(self.app.playing)
        self.app._audio.play.assert_called_once()

    def test_fractional_rate_uses_scanned_rate_not_ui_estimate(self):
        self.session.metadata['output_rate'] = '60000/1001'
        self.tick(100)
        self.ms = 1001
        self.tick(101)
        self.assertEqual(self.app._shared_output_index, 60)

    def test_initial_buffer_does_not_start_sound(self):
        self.session.buffered = lambda *_: False
        self.tick(100)
        self.assertTrue(self.app._buffering)
        self.app._audio.play.assert_not_called()


if __name__ == '__main__':
    unittest.main()
