import os
import unittest

import numpy as np

import dlss_engine
from dlss_host_process import HostProcessError, ProcessLive


class FakeLive:
    """Pickleable stand-in used to exercise the real spawn/Pipe/shared-memory path."""

    def __init__(self, width, height, settings):
        self.width = width
        self.height = height
        self.settings = dict(settings)
        self.backend = self.settings.get("host_backend", "legacy")
        if self.settings.get("fake_fail_backend") == self.backend:
            raise RuntimeError("requested fake backend failure: " + self.backend)
        self.max_in_flight = 2 if self.backend == "v2" else 1
        self.supports_async = self.max_in_flight > 1
        self._pending = []

    def update(self, settings):
        requested = settings.get("host_backend", self.backend)
        if requested not in ("auto", self.backend):
            raise RuntimeError("fake backend cannot switch in process")
        self.settings.update(settings)

    def _render(self, rgba):
        increment = 2 if self.backend == "v2" else 1
        return np.clip(rgba.astype(np.uint16) + increment, 0, 255).astype(np.uint8)

    def process(self, rgba, reset=False):
        return self._render(rgba)

    def enqueue(self, rgba, reset=False):
        self._pending.append(self._render(rgba.copy()))
        return True

    def dequeue(self):
        return self._pending.pop(0)

    @property
    def pending(self):
        return len(self._pending)


class ProcessLiveTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
