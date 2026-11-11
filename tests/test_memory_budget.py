"""Disposable caches must honor both RAM and commit pressure without saving it."""
import unittest
from unittest import mock
from types import SimpleNamespace

import numpy as np

from dlss5tool import memory_budget as mb
from dlss5tool.guidance_cache import RawGuidanceCache
from dlss5tool.render_cache import RenderSession
from dlss5tool.shared_cache_budget import SharedCacheBudget

GIB = 1024**3


def memory(total=32, free=16, commit=20):
    return dict(total_physical_bytes=int(total*GIB), free_physical_bytes=int(free*GIB),
                free_commit_bytes=int(commit*GIB))


class MemoryBudgetTests(unittest.TestCase):
    def test_small_ram_caps_large_saved_budget_and_keeps_user_upper_bound(self):
        self.assertEqual(mb.cache_limit_bytes(32*GIB, memory=memory()), 8*GIB)
        self.assertEqual(mb.cache_limit_bytes(8*GIB, memory=memory(total=8, free=6)), 2*GIB)
        self.assertEqual(mb.cache_limit_bytes(GIB, memory=memory()), GIB)

    def test_existing_cache_is_credited_once_and_shrinks_by_headroom_deficit(self):
        self.assertEqual(mb.cache_limit_bytes(8*GIB, 8*GIB, memory=memory(free=1)), 5*GIB)
        self.assertEqual(mb.cache_limit_bytes(8*GIB, 2*GIB, memory=memory(free=1)), 0)

    def test_commit_pressure_also_limits_despite_free_physical_ram(self):
        self.assertEqual(mb.cache_limit_bytes(8*GIB, 6*GIB, memory=memory(commit=1)), 3*GIB)

    def test_pressure_can_recover_without_changing_preference(self):
        self.assertEqual(mb.cache_limit_bytes(8*GIB, memory=memory(free=1)), 0)
        self.assertEqual(mb.cache_limit_bytes(8*GIB, memory=memory(free=16)), 8*GIB)

    def test_unavailable_snapshot_preserves_existing_upper_bound(self):
        with mock.patch.object(mb, 'system_memory', return_value=None):
            self.assertEqual(mb.cache_limit_bytes(1234), 1234)
        self.assertEqual(mb.cache_limit_bytes(1234, memory=memory(total=0)), 1234)
        self.assertEqual(mb.cache_limit_bytes(-1, memory=memory()), 0)

    def test_frame_queue_reduces_by_actual_entry_bytes_and_can_deny_one(self):
        with mock.patch.object(mb, 'system_memory', return_value=memory(free=8)):
            self.assertEqual(mb.bounded_frame_count(16, 256*1024**2), 4)
        with mock.patch.object(mb, 'system_memory', return_value=memory(free=3)):
            self.assertEqual(mb.bounded_frame_count(16, 1024), 0)
        with mock.patch.object(mb, 'system_memory', return_value=None):
            self.assertEqual(mb.bounded_frame_count(16, 1024), 16)

    def test_shared_pool_credits_all_leases_once_and_preserves_configured_limit(self):
        owner = SharedCacheBudget(limit_bytes=8*GIB)
        other = SharedCacheBudget(owner.name)
        try:
            with owner.locked():
                owner.publish_locked(3*GIB)
            with other.locked():
                other.publish_locked(3*GIB)
            with mock.patch.object(mb, 'system_memory', return_value=memory(free=3)):
                # Total cache 6 GiB, reserve 4 GiB, free 3 GiB => 5 GiB total.
                with owner.locked():
                    self.assertEqual(owner.allowance_locked(), 2*GIB)
                self.assertEqual(owner.snapshot()['effective_limit_bytes'], 5*GIB)
                self.assertEqual(owner.limit_bytes, 8*GIB)
        finally:
            other.close()
            owner.close()

    def test_unpooled_raw_cache_releases_under_pressure_and_can_fill_again(self):
        # This case explicitly exercises the unpooled policy, independently of
        # a default pool left by earlier process/GUI tests and its hysteresis.
        with mock.patch('dlss5tool.guidance_cache.current_pool', return_value=None):
            cache = RawGuidanceCache(1024)
        with mock.patch.object(mb, 'system_memory', return_value=memory()):
            cache.put('first', np.zeros(128, np.float32))
            self.assertEqual(cache.bytes, 512)
        with mock.patch.object(mb, 'system_memory', return_value=memory(free=0)):
            cache.trim()
            self.assertEqual(cache.bytes, 0)
            cache.put('denied', np.zeros(128, np.float32))
            self.assertEqual(cache.bytes, 0)
        with mock.patch.object(mb, 'system_memory', return_value=memory()):
            cache.put('new', np.zeros(128, np.float32))
            self.assertEqual(cache.bytes, 512)
        cache.close()

    def test_unpooled_render_cache_trims_with_safe_memory_allowance(self):
        session = RenderSession('synthetic', {}, 1024)
        pixels = np.zeros((8, 8, 4), np.uint8)
        session.cache[0] = (pixels, pixels.copy())
        session.bytes = 512
        with mock.patch.object(mb, 'system_memory', return_value=memory(free=0)):
            with session.condition:
                self.assertEqual(session._trim(), 0)
            self.assertEqual(session.bytes, 0)
            self.assertEqual(len(session.cache), 0)
        session.close()

    def test_decoder_caps_queue_and_stops_cleanly_when_one_frame_will_not_fit(self):
        from dlss5tool import gui
        queue_type = gui.queue.Queue
        for capacity in (2, 0):
            with self.subTest(capacity=capacity):
                capture = mock.Mock()
                capture.isOpened.return_value = True
                pixels = np.zeros((8, 8, 3), np.uint8)
                capture.read.return_value = (True, pixels)
                queues = []
                def make_queue(*args, **kwargs):
                    item = queue_type(*args, **kwargs)
                    queues.append(item)
                    return item
                app = SimpleNamespace(video='synthetic', _video_color_info={})
                with mock.patch.object(gui, 'open_capture', return_value=capture), \
                        mock.patch.object(gui, 'bounded_frame_count', return_value=capacity), \
                        mock.patch.object(gui.queue, 'Queue', side_effect=make_queue):
                    frames = gui.App._iter_frames(app, buffer_size=16)
                    try:
                        if capacity:
                            self.assertEqual(next(frames)[0], 0)
                            self.assertEqual(queues[0].maxsize, capacity)
                        else:
                            with self.assertRaises(MemoryError):
                                next(frames)
                    finally:
                        frames.close()
                capture.release.assert_called_once_with()

    def test_legacy_gui_cache_evicts_under_pressure_without_saving_new_budget(self):
        from dlss5tool.gui import App
        app = App.__new__(App)
        pixels = np.zeros((8, 8, 3), np.uint8)
        app._shared_cache_pool = None
        app._preview_cache_bytes = lambda: 1024
        app._dlss_cache_bytes, app._source_cache_bytes = pixels.nbytes, pixels.nbytes
        app._dlss_frame_cache = {(0, 8, 8): ('settings', pixels)}
        app._source_frame_cache = {0: pixels.copy()}
        app._frame, app._active_preview_size = 0, (8, 8)
        app._source_size = lambda: (8, 8)
        app._prerender_target_frames = lambda: 1
        app._live_cache = app._last_shown_dlss = None
        with mock.patch.object(mb, 'system_memory', return_value=memory(free=0)):
            app._evict_preview_cache_locked()
        self.assertEqual(app._dlss_cache_bytes + app._source_cache_bytes, 0)
        self.assertEqual(app._preview_cache_bytes(), 1024)
