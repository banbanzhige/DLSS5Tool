import os
import tempfile
import unittest
from unittest import mock

import super_resolution


class SuperResolutionSizingTests(unittest.TestCase):
    def test_only_supported_scales_are_accepted(self):
        self.assertEqual(super_resolution.normalize_scale("2"), 2)
        self.assertEqual(super_resolution.normalize_scale(4), 4)
        self.assertEqual(super_resolution.normalize_scale(3), 1)
        self.assertEqual(super_resolution.normalize_scale("bad"), 1)

    def test_target_size_has_no_artificial_8k_limit(self):
        self.assertEqual(super_resolution.target_size(3840, 2160, 4), (15360, 8640))

    def test_estimate_reduces_dlss_in_flight_above_4k(self):
        estimate = super_resolution.estimate_resources(3840, 2160, 2, is_hdr=True)
        self.assertEqual((estimate["output_width"], estimate["output_height"]), (7680, 4320))
        self.assertEqual(estimate["in_flight"], 1)
        self.assertEqual(estimate["single_frame_bytes"], 7680 * 4320 * 8)
        self.assertGreater(estimate["recommended_gpu_bytes"], estimate["known_gpu_bytes"])
        self.assertGreater(estimate["recommended_ram_bytes"], estimate["known_ram_bytes"])

    def test_resource_risk_uses_current_free_vram(self):
        estimate = {"output_width": 3840, "output_height": 2160, "recommended_gpu_bytes": 8_000}
        self.assertEqual(
            super_resolution.classify_resource_risk(estimate, {"free_bytes": 20_000}),
            "low",
        )
        self.assertEqual(
            super_resolution.classify_resource_risk(estimate, {"free_bytes": 10_000}),
            "medium",
        )
        self.assertEqual(
            super_resolution.classify_resource_risk(estimate, {"free_bytes": 8_500}),
            "high",
        )

    def test_dimensions_over_8192_warn_but_are_not_rejected(self):
        estimate = {
            "output_width": 15360, "output_height": 8640,
            "recommended_gpu_bytes": 1,
        }
        self.assertEqual(
            super_resolution.classify_resource_risk(estimate, {"free_bytes": 10_000}),
            "extreme",
        )


class RuntimeDiscoveryTests(unittest.TestCase):
    def test_runtime_status_lists_only_missing_components(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            host = os.path.join(temp_dir, "vsr_host.dll")
            runtime = os.path.join(temp_dir, "nvngx_vsr.dll")
            with open(host, "wb") as handle:
                handle.write(b"host")
            with mock.patch.object(super_resolution, "VSR_HOST_DLL", host), mock.patch.object(
                super_resolution, "VSR_RUNTIME_DLL", runtime,
            ):
                status = super_resolution.runtime_status()
            self.assertFalse(status["available"])
            self.assertEqual(status["missing"], ["nvngx_vsr.dll"])


class SessionPolicyTests(unittest.TestCase):
    def test_large_targets_receive_a_longer_watchdog(self):
        self.assertGreaterEqual(
            super_resolution.operation_timeout(7680, 4320), 270.0,
        )


if __name__ == "__main__":
    unittest.main()
