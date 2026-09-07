"""Current-frame latency regressions, without Tk windows or GPU dependencies."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from gui import App


class PreviewLatencyTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.video = "preview.mp4"
        app._source_kind = "video"
        app.playing = app._exporting = app._hold_original = False
        app._preview_cache_frozen = app._dlss_pending = False
        app._frame = 67
        app.nframes = 243
        app.fps = 24
        app._media_w, app._media_h = 8, 4
        app._active_preview_size = (8, 4)
        app._precise_preview_size = lambda: (8, 4)
        app._settings_hash = lambda: ("new-settings",)
        app._cache_lock = threading.RLock()
        app._live_lock = threading.RLock()
        app._dlss_frame_cache = {}
        app._source_frame_cache = {}
        app._queued_preview_frames = set()
        app._dlss_cache_bytes = app._source_cache_bytes = 0
        app._live_cache = app._last_shown_dlss = None
        app._preview_processed_frames = 0
        app._preview_process_t0 = None
        app._evict_preview_cache_locked = lambda: None
        app._prerender_target_frames = lambda: 176
        app._pre_rendering = True
        app._prefetch_stop = threading.Event()
        app.view_var = SimpleNamespace(get=lambda: "对比")
        app.display_view = Mock()
        app._schedule_preview_decode = Mock()
        app._update_preview_timeline_and_status = Mock()
        return app

    def test_current_frame_presented_before_filling_ahead_even_if_queue_full(self):
        for accepted in (True, False):
            with self.subTest(queue_accepts=accepted):
                app = self.make_app()
                source = np.zeros((4, 8, 3), np.uint8)
                app._source_cache_store(67, source)
                app._source_cache_store(68, source)
                app._cache_store(67, app._settings_hash(), source.copy())
                events = []
                app.display_view = lambda **kw: events.append("present")
                app._queue_preview_frame = lambda *args: events.append("queue") or accepted
                app._read_frame = Mock(side_effect=AssertionError("source already cached"))

                app._preview_decode_tick()

                self.assertEqual(events, ["present", "queue"])
                app._update_preview_timeline_and_status.assert_called()
                app._schedule_preview_decode.assert_called()

    def test_unchanged_current_frame_is_not_repainted_every_background_tick(self):
        app = self.make_app()
        app._cache_store(67, app._settings_hash(), np.zeros((4, 8, 3), np.uint8))
        self.assertTrue(app._present_current_cached_preview())
        self.assertFalse(app._present_current_cached_preview())
        app.display_view.assert_called_once_with(quality="full")
        # A pending surface must still be replaced even for the same frame/key.
        app._dlss_pending = True
        self.assertTrue(app._present_current_cached_preview())
        self.assertEqual(app.display_view.call_count, 2)

    def test_worker_completion_during_final_scan_is_still_presented(self):
        app = self.make_app()
        source = np.zeros((4, 8, 3), np.uint8)
        app._source_cache_store(67, source)
        app._prerender_target_frames = lambda: 1
        # Not ready at tick entry, then completes before the cache scan ends.
        app._cached_dlss_sk = Mock(side_effect=[None, source, source])

        app._preview_decode_tick()

        app.display_view.assert_called_once_with(quality="full")
        self.assertFalse(app._pre_rendering)
        self.assertTrue(app._prefetch_stop.is_set())

    def test_cache_invalidation_allows_same_frame_to_be_presented_again(self):
        app = self.make_app()
        source = np.zeros((4, 8, 3), np.uint8)
        app._cache_store(67, app._settings_hash(), source)
        self.assertTrue(app._present_current_cached_preview())
        app._cache_clear(keep_source=True)
        app._cache_store(67, app._settings_hash(), source.copy())
        self.assertTrue(app._present_current_cached_preview())
        self.assertEqual(app.display_view.call_count, 2)

    def test_proxy_cache_does_not_trigger_synchronous_precise_processing(self):
        app = self.make_app()
        app._active_preview_size = (4, 2)
        app._cache_store(67, app._settings_hash(), np.zeros((2, 4, 3), np.uint8))
        self.assertFalse(app._present_current_cached_preview())
        app.display_view.assert_not_called()

    def test_missing_or_stale_frame_is_not_presented(self):
        app = self.make_app()
        self.assertFalse(app._present_current_cached_preview())
        app._cache_store(67, ("old-settings",), np.zeros((4, 8, 3), np.uint8))
        self.assertFalse(app._present_current_cached_preview())
        app.display_view.assert_not_called()

    def test_playing_frozen_or_exporting_does_not_present_from_background(self):
        for state in ("playing", "_preview_cache_frozen", "_exporting"):
            with self.subTest(state=state):
                app = self.make_app()
                app._cache_store(67, app._settings_hash(), np.zeros((4, 8, 3), np.uint8))
                setattr(app, state, True)
                self.assertFalse(app._present_current_cached_preview())
                app.display_view.assert_not_called()

    def test_parameter_refresh_preserves_decoded_sources_and_clears_dlss(self):
        app = self.make_app()
        source = np.zeros((4, 8, 3), np.uint8)
        for frame in (67, 68, 100):
            app._source_cache_store(frame, source)
            app._cache_store(frame, ("old-settings",), source.copy())
        used = app._source_cache_bytes
        app._queued_preview_frames.add(100)
        app._live = Mock()
        app._collect_settings = lambda: {"intensity": 0.5}
        app.timeline = Mock()
        app._schedule_preview_cache_resume = Mock()

        app._refresh_dlss()

        self.assertEqual(set(app._source_frame_cache), {67, 68, 100})
        self.assertIs(app._source_cache_get(100), source)
        self.assertEqual(app._source_cache_bytes, used)
        self.assertEqual(app._dlss_frame_cache, {})
        self.assertEqual(app._dlss_cache_bytes, 0)
        self.assertEqual(app._queued_preview_frames, set())
        self.assertIsNone(app._live_cache)
        self.assertEqual(app._preview_processed_frames, 0)
        app._live.update.assert_called_once_with({"intensity": 0.5})
        app._schedule_preview_cache_resume.assert_called_once()

    def test_full_cache_clear_still_discards_source_for_media_changes(self):
        app = self.make_app()
        source = np.zeros((4, 8, 3), np.uint8)
        app._source_cache_store(67, source)
        app._cache_store(67, app._settings_hash(), source.copy())
        app._cache_clear()
        self.assertEqual(app._source_frame_cache, {})
        self.assertEqual(app._source_cache_bytes, 0)
        self.assertEqual(app._dlss_frame_cache, {})
        self.assertEqual(app._dlss_cache_bytes, 0)

    def test_parameter_refresh_waits_for_retiring_worker_without_updating_host(self):
        app = self.make_app()
        app.root = Mock()
        app._play_dlss_thread = Mock()
        app._play_dlss_thread.is_alive.return_value = True
        app._live = Mock()
        app._refresh_dlss()
        app.root.after.assert_called_once()
        app._live.update.assert_not_called()
        app._play_dlss_thread.join.assert_not_called()


if __name__ == "__main__":
    unittest.main()
