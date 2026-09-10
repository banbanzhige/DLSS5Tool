"""User-triggered cache invalidation must retire workers before re-rendering."""
import queue
import threading
import unittest
from unittest.mock import Mock

from dlss5tool.gui import App


class ClearPreviewCacheTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.video = 'preview.mp4'
        app._frame = 67
        app.playing = True
        app._exporting = False
        app._guidance_preview_epoch = 4
        app._guidance_preview_busy = False
        app._guidance_result = app._guidance_ready = app._guidance_presented = object()
        app._guidance_display_signature = object()
        app._cache_lock = threading.RLock()
        app._dlss_frame_cache = {67: object()}
        app._source_frame_cache = {67: object()}
        app._queued_preview_frames = {68}
        app._dlss_cache_bytes = app._source_cache_bytes = 100
        app._prefetch_stop = threading.Event()
        app._prefetch_gen = 2
        app._shared_cache_pool = None
        app._live_cache = app._last_shown_dlss = object()
        app._play_orig = app._split_orig = app._split_dlss = object()
        app._last_dlss_frame = app._split_frame = 67
        app._preview_zoom = 2.0
        app._preview_pan_x, app._preview_pan_y = .3, .7
        app.root = Mock()
        app.timeline = Mock()
        app._audio = Mock()
        app.pause = Mock(side_effect=lambda: setattr(app, 'playing', False))
        app._cancel_after = Mock()
        app._schedule_preview_cache_resume = Mock()
        app._schedule_full_preview = Mock()
        app.set_status = Mock()
        app.logln = Mock()
        app.display_view = Mock()
        return app

    def test_clear_retires_work_preserves_media_position_and_regenerates(self):
        app = self.make_app()
        app.clear_preview_cache()
        self.assertFalse(app.playing)
        self.assertTrue(app._clear_preview_pending)
        self.assertTrue(app._prefetch_stop.is_set())
        self.assertEqual(app._prefetch_gen, 3)
        self.assertEqual(app._guidance_preview_epoch, 5)
        self.assertIsNone(app._guidance_result)
        app.root.after.assert_called_once_with(0, app._poll_clear_preview_cache)
        app.root.after.reset_mock()

        # The old worker must not be joined on Tk or allowed to refill new caches.
        worker = Mock()
        worker.is_alive.return_value = True
        app._play_dlss_thread = worker
        app._poll_clear_preview_cache()
        worker.join.assert_not_called()
        app.root.after.assert_called_once()
        app.display_view.assert_not_called()
        self.assertTrue(app._clear_preview_pending)

        worker.is_alive.return_value = False
        pool = Mock()
        app._shared_cache_pool = pool
        app._cache_clear = Mock(wraps=app._cache_clear)
        # The shared-budget publishing context is unrelated to invalidation.
        pool.locked.return_value = threading.RLock()
        app._publish_frame_cache_locked = Mock()
        app._poll_clear_preview_cache()
        pool.invalidate_guidance.assert_called_once()
        app._cache_clear.assert_called_once_with()
        self.assertFalse(app._clear_preview_pending)
        self.assertFalse(app._preview_cache_frozen)
        self.assertEqual(app._dlss_frame_cache, {})
        self.assertEqual(app._source_frame_cache, {})
        self.assertEqual(app._dlss_cache_bytes + app._source_cache_bytes, 0)
        self.assertEqual(app._last_dlss_frame, -1)
        self.assertIsNone(app._split_orig)
        self.assertIsNone(app._live_cache)
        self.assertEqual((app.video, app._frame), ('preview.mp4', 67))
        self.assertEqual((app._preview_zoom, app._preview_pan_x, app._preview_pan_y), (2., .3, .7))
        app._audio.close.assert_not_called()
        app.timeline.set_cache_ranges.assert_called_once_with([], [])
        app.display_view.assert_called_once_with(quality='fast')
        app._schedule_full_preview.assert_called_once()

    def test_busy_or_empty_media_is_noop(self):
        for state in ('_exporting', '_queue_running', '_diagnosing', '_switching_backend',
                      '_module_reload_thread', '_clear_preview_pending', 'empty'):
            with self.subTest(state=state):
                app = self.make_app()
                if state == 'empty':
                    app.video = None
                else:
                    setattr(app, state, True)
                app.clear_preview_cache()
                app.pause.assert_not_called()
                app._schedule_preview_cache_resume.assert_not_called()

    def test_guidance_request_waits_until_cleared(self):
        app = self.make_app()
        app.clear_preview_cache()
        app._collect_settings = Mock(side_effect=AssertionError('must not start new work'))
        app._request_guidance_preview(('old',))
        app._collect_settings.assert_not_called()
        app._guidance_context = True
        app._poll_clear_preview_cache()
        app.display_view.assert_called_once_with(quality='fast')
        app._schedule_full_preview.assert_not_called()

    def test_old_guidance_completion_is_discarded(self):
        app = self.make_app()
        app._guidance_generation = 1
        app._settings_hash = lambda: ('settings',)
        old_key = app._guidance_preview_key()
        app.clear_preview_cache()
        app._guidance_context = True
        app._guidance_preview_busy = True
        app._guidance_preview_queue = queue.SimpleQueue()
        app._guidance_preview_queue.put((old_key, object(), {}, False, ''))
        app._poll_guidance_preview()
        self.assertIsNone(app._guidance_result)
        self.assertFalse(app._guidance_preview_busy)

    def test_normal_resume_cannot_restart_work_during_clear(self):
        app = self.make_app()
        app.clear_preview_cache()
        app._resume_preview_cache()
        self.assertTrue(app._preview_cache_frozen)
        app.display_view.assert_not_called()
        self.assertFalse(app._preview_session_active())

    def test_cancelled_clear_does_not_affect_new_source(self):
        app = self.make_app()
        app.clear_preview_cache()
        app._clear_preview_pending = False
        app._finish_clear_preview_cache = Mock()
        app._poll_clear_preview_cache()
        app._finish_clear_preview_cache.assert_not_called()

    def test_export_started_while_waiting_defers_invalidation(self):
        app = self.make_app()
        app.clear_preview_cache()
        app._exporting = True
        app._poll_clear_preview_cache()
        self.assertTrue(app._clear_preview_pending)
        app.display_view.assert_not_called()
        app._exporting = False
        app._poll_clear_preview_cache()
        self.assertFalse(app._clear_preview_pending)


if __name__ == '__main__':
    unittest.main()
