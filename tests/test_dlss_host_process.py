import os
import unittest
from tests.depth_fixture import depth_test_case

import numpy as np

from dlss5tool import dlss_engine
from dlss5tool.dlss_host_process import HostProcessError, ProcessLive


class FakeLive:
    """Pickleable stand-in used to exercise the real spawn/Pipe/shared-memory path."""

    def __init__(self, width, height, settings):
        self.width = width
        self.height = height
        self.settings = dict(settings)
        self.backend = self.settings.get("host_backend", "legacy")
        if self.settings.get("fake_fail_backend") == self.backend:
            raise RuntimeError("requested fake backend failure: " + self.backend)
        self.tiled = bool(self.settings.get("host_tiled_mode", False))
        self.max_in_flight = 2 if self.backend == "v2" and not self.tiled else 1
        self.supports_async = self.max_in_flight > 1
        self._pending = []
        self.guidance_info = {}
        self._guidance_previous = None

    def update(self, settings):
        requested = settings.get("host_backend", self.backend)
        if requested not in ("auto", self.backend):
            raise RuntimeError("fake backend cannot switch in process")
        self.settings.update(settings)
        self.tiled = bool(self.settings.get("host_tiled_mode", False))
        self.max_in_flight = 2 if self.backend == "v2" and not self.tiled else 1
        self.supports_async = self.max_in_flight > 1

    def _render(self, rgba):
        if self.settings.get('fake_guidance'):
            self.guidance_info = {'device': 'cuda', 'device_name': 'Fixture GPU', 'precision': 'float32'}
        increment = 2 if self.backend == "v2" else 1
        if rgba.dtype == np.float16:
            return (rgba.astype(np.float32) + increment / 100.0).astype(np.float16)
        return np.clip(rgba.astype(np.uint16) + increment, 0, 255).astype(np.uint8)

    def process(self, rgba, reset=False):
        return self._render(rgba)

    def guidance_preview(self, rgba, reset=False, final=True):
        reset = bool(reset or self._guidance_previous is None)
        motion = np.zeros((*rgba.shape[:2], 2), np.float32)
        if not reset:
            motion[..., 0] = rgba[..., 0].astype(np.float32) - self._guidance_previous[..., 0]
        depth = rgba[..., 0].astype(np.float32) / 255.0
        self._guidance_previous = rgba.copy()
        return motion, depth, reset

    def enqueue(self, rgba, reset=False):
        self._pending.append(self._render(rgba.copy()))
        return True

    def dequeue(self):
        return self._pending.pop(0)

    @property
    def pending(self):
        return len(self._pending)


@depth_test_case
class ProcessLiveTests(unittest.TestCase):
    def test_incompatible_guidance_fails_before_large_shared_allocations(self):
        from unittest import mock
        from dlss5tool import dlss_host_process
        for extra in ({'host_tiled_mode': True}, {'frame_format': 'rgba16f'}):
            with mock.patch.object(dlss_host_process, '_open_shared_memory') as memory:
                with self.assertRaisesRegex(HostProcessError, 'SDR'):
                    ProcessLive(11637,5120,{'guidance_mode':3, 'host_backend':'v2', **extra})
                memory.assert_not_called()

    def test_guidance_preview_crosses_process_boundary_without_dlss_output(self):
        live = self.make_live('v2', guidance_mode=3)
        try:
            previous = np.zeros((6, 8, 4), np.uint8)
            current = np.zeros((6, 8, 4), np.uint8)
            current[..., 0] = 32
            images, reset = live.guidance_preview(current, previous)
            self.assertFalse(reset)
            self.assertEqual(set(images), {'depth', 'flow'})
            self.assertEqual(images['depth'].shape, (6, 8, 3))
            self.assertEqual(images['flow'].shape, (6, 8, 3))
            self.assertGreater(int(images['flow'].max()), 0)
        finally:
            live.close()
    def test_appearance_keeps_guidance_session_budget_change_replaces(self):
        live=self.make_live('v2',guidance_cache_mb=1024)
        try:
            original=live._session
            live.update({'intensity':.55,'style':1,'local_tone':.3,'skin_struct':.8})
            self.assertIs(live._session,original)
            live.update({'guidance_cache_mb':512})
            self.assertIsNot(live._session,original)
            self.assertTrue(original._closed)
        finally:live.close()

    def test_guidance_device_crosses_process_boundary_once_per_session(self):
        seen = []
        live = ProcessLive(8, 6, {'host_backend': 'v2', 'fake_guidance': True},
                           _live_factory=FakeLive, _on_guidance_ready=seen.append)
        try:
            self.assertEqual(live.guidance_info, {})
            frame = np.zeros((6, 8, 4), np.uint8)
            live.process(frame)
            live.enqueue(frame)
            live.dequeue()
            self.assertEqual(live.guidance_info['device'], 'cuda')
            self.assertEqual(len(seen), 1)
            live.resize(8, 6)
            live.process(frame)
            self.assertEqual(len(seen), 2)
        finally:
            live.close()
    def test_guidance_and_runtime_selection_replace_worker(self):
        live = self.make_live('v2')
        try:
            previous = live._session
            live.update({'guidance_mode': 1})
            self.assertIsNot(previous, live._session)
            self.assertTrue(previous._closed)
            previous = live._session
            live.update({'guidance_mode': 0, 'dlss_runtime': 'selected.dll'})
            self.assertIsNot(previous, live._session)
            self.assertTrue(previous._closed)
        finally:
            live.close()

    def make_live(self, backend="legacy", **extra):
        settings = {
            "host_backend": backend,
            "host_auto_fallback": True,
            **extra,
        }
        return ProcessLive(8, 6, settings, _live_factory=FakeLive)

    def test_process_and_hot_switch_use_shared_memory(self):
        live = self.make_live("legacy")
        try:
            frame = np.full((6, 8, 4), 10, np.uint8)
            np.testing.assert_array_equal(live.process(frame, reset=True), frame + 1)
            self.assertEqual(live.backend, "legacy")

            live.update({"host_backend": "v2"})
            self.assertEqual(live.backend, "v2")
            self.assertEqual(live.preference, "v2")
            self.assertTrue(live.supports_async)
            np.testing.assert_array_equal(live.process(frame), frame + 2)
        finally:
            live.close()

    def test_failed_switch_keeps_previous_worker_alive(self):
        live = self.make_live("legacy")
        try:
            with self.assertRaises(HostProcessError):
                live.update({
                    "host_backend": "v2",
                    "fake_fail_backend": "v2",
                })
            self.assertEqual(live.backend, "legacy")
            self.assertEqual(live.preference, "legacy")
            frame = np.zeros((6, 8, 4), np.uint8)
            np.testing.assert_array_equal(live.process(frame), frame + 1)
        finally:
            live.close()

    def test_auto_fallback_uses_a_fresh_legacy_process(self):
        original_v2_path = dlss_engine.HOST_DLL_V2
        dlss_engine.HOST_DLL_V2 = os.path.abspath(__file__)
        try:
            live = self.make_live("auto", fake_fail_backend="v2")
            try:
                self.assertEqual(live.preference, "auto")
                self.assertEqual(live.backend, "legacy")
            finally:
                live.close()
        finally:
            dlss_engine.HOST_DLL_V2 = original_v2_path

    def test_async_queue_and_resize(self):
        live = self.make_live("v2")
        try:
            first = np.full((6, 8, 4), 20, np.uint8)
            second = np.full((6, 8, 4), 30, np.uint8)
            self.assertTrue(live.enqueue(first, reset=True))
            self.assertTrue(live.enqueue(second))
            self.assertEqual(live.pending, 2)
            np.testing.assert_array_equal(live.dequeue(), first + 2)
            np.testing.assert_array_equal(live.dequeue(), second + 2)

            live.resize(5, 4)
            resized = np.full((4, 5, 4), 7, np.uint8)
            np.testing.assert_array_equal(live.process(resized), resized + 2)
        finally:
            live.close()

    def test_resize_applies_tiled_settings_in_replacement_process(self):
        live = self.make_live("v2")
        try:
            live.resize(5, 4, settings={"host_tiled_mode": True})
            self.assertTrue(live.tiled)
            self.assertFalse(live.supports_async)
            resized = np.full((4, 5, 4), 7, np.uint8)
            np.testing.assert_array_equal(live.process(resized), resized + 2)
        finally:
            live.close()

    def test_rgba16f_contract_uses_float_shared_memory(self):
        live = self.make_live(
            "v2", frame_format="rgba16f", color_profile="hdr10_pq",
        )
        try:
            frame = np.full((6, 8, 4), 0.25, np.float16)
            output = live.process(frame, reset=True)
            self.assertEqual(output.dtype, np.float16)
            np.testing.assert_allclose(output, 0.27, atol=0.001)
        finally:
            live.close()


if __name__ == "__main__":
    unittest.main()
