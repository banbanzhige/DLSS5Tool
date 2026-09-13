"""Expanded queue validation and old/new native-capacity negotiation."""
from types import SimpleNamespace
import unittest
from unittest import mock
from dlss5tool import app_settings, dlss_engine, super_resolution
from dlss5tool.host_queue import clamp_in_flight


class HostQueueTests(unittest.TestCase):
    def test_range_and_saved_setting(self):
        for value in (1, 2, 3, 4, 8, 16):
            self.assertEqual(app_settings.validate({'host_in_flight': value})['host_in_flight'], value)
            self.assertEqual(dlss_engine._host_config({'host_in_flight': value})[3], value)
        self.assertEqual(clamp_in_flight(100), 16)
        self.assertEqual(clamp_in_flight(-5), 1)
        self.assertEqual(clamp_in_flight('bad'), 2)

    def live(self, capabilities=3, capacity=None):
        live = dlss_engine.Live.__new__(dlss_engine.Live)
        live.settings = {'host_in_flight': 16}
        live._lib = SimpleNamespace(dlssnr_capabilities=lambda: capabilities)
        if capacity is not None:
            live._lib.dlssnr_queue_capacity = lambda: capacity
        live._refresh_capabilities()
        return live

    def test_actual_capacity_not_requested_prevents_old_dll_overflow(self):
        self.assertEqual(self.live().max_in_flight, 3)
        self.assertEqual(self.live(capacity=16).max_in_flight, 16)
        self.assertEqual(self.live(capacity=5).max_in_flight, 5)
        self.assertEqual(self.live(capabilities=1, capacity=16).max_in_flight, 1)
        self.assertEqual(self.live(capabilities=7, capacity=16).max_in_flight, 1)
        self.assertFalse(self.live(capacity=1).supports_async)

    def test_old_library_receives_bounded_request(self):
        lib = SimpleNamespace(dlssnr_configure=mock.Mock())
        dlss_engine._configure_host(lib, {'host_in_flight': 16})
        self.assertEqual(lib.dlssnr_configure.call_args.args[3], 3)
        lib.dlssnr_queue_capacity = lambda: 16
        dlss_engine._configure_host(lib, {'host_in_flight': 16})
        self.assertEqual(lib.dlssnr_configure.call_args.args[3], 16)

    def test_resource_estimates_scale_above_three_and_large_hdr_stays_bounded(self):
        small = super_resolution.estimate_resources(1920, 1080, 1, in_flight=3)
        large = super_resolution.estimate_resources(1920, 1080, 1, in_flight=16)
        self.assertEqual(large['in_flight'], 16)
        self.assertGreater(large['known_gpu_bytes'], small['known_gpu_bytes'])
        self.assertEqual(super_resolution.select_in_flight(3840, 2160, 2, 16, is_hdr=True), 1)
        self.assertEqual(super_resolution.select_in_flight(1920, 1080, 1, 16), 16)


if __name__ == '__main__':
    unittest.main()
