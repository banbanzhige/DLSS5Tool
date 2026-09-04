import unittest

import numpy as np

from video_export import (
    _hlg_eotf,
    _hlg_oetf,
    _pq_eotf,
    _pq_oetf,
    _zscale_decode_filter,
    classify_color_info,
    compose_hdr_frame,
    tone_map_hdr_preview,
)


class HdrMetadataTests(unittest.TestCase):
    def test_pq_and_hlg_are_detected(self):
        pq = classify_color_info({"color_transfer": "smpte2084"})
        hlg = classify_color_info({"color_transfer": "arib-std-b67"})
        self.assertEqual(pq["profile"], "hdr10_pq")
        self.assertEqual(hlg["profile"], "hdr10_hlg")
        self.assertTrue(pq["is_hdr"])
        self.assertEqual(pq["color_primaries"], "bt2020")
        self.assertFalse(classify_color_info({"color_transfer": "bt709"})["is_hdr"])

    def test_missing_hdr_chromaticity_uses_bt2020_contract(self):
        info = classify_color_info({
            "color_transfer": "smpte2084",
            "color_primaries": "unknown",
            "color_space": "unknown",
        })
        self.assertEqual(info["color_primaries"], "bt2020")
        self.assertEqual(info["color_space"], "bt2020nc")

    def test_decode_filter_keeps_yuv_matrix_until_rgb_conversion(self):
        value = _zscale_decode_filter(classify_color_info({
            "color_transfer": "smpte2084",
            "color_space": "bt2020nc",
        }))
        self.assertIn("matrixin=bt2020nc:matrix=bt2020nc", value)
        self.assertNotIn("matrix=gbr", value)

    def test_transfer_functions_round_trip(self):
        values = np.linspace(0.0, 1.0, 128, dtype=np.float32)
        np.testing.assert_allclose(_pq_oetf(_pq_eotf(values)), values, atol=2e-5)
        np.testing.assert_allclose(_hlg_oetf(_hlg_eotf(values)), values, atol=2e-5)


class HdrFrameTests(unittest.TestCase):
    def test_endpoints_are_exact(self):
        original = np.full((2, 4, 4), 0.2, np.float16)
        processed = np.full((2, 4, 4), 0.8, np.float16)
        np.testing.assert_array_equal(
            compose_hdr_frame(original, processed, mix=0.0), original,
        )
        np.testing.assert_array_equal(
            compose_hdr_frame(original, processed, mix=1.0), processed,
        )

    def test_pq_mix_is_linear_light_not_code_value(self):
        original = np.full((1, 1, 4), 0.2, np.float16)
        processed = np.full((1, 1, 4), 0.8, np.float16)
        mixed = compose_hdr_frame(original, processed, mix=0.5, profile="hdr10_pq")
        self.assertGreater(float(mixed[0, 0, 0]), 0.5)

    def test_pq_mix_above_one_amplifies_in_linear_light(self):
        original = np.full((1, 1, 4), 0.3, np.float16)
        processed = np.full((1, 1, 4), 0.5, np.float16)
        original[..., 3] = processed[..., 3] = 1.0
        boosted = compose_hdr_frame(original, processed, mix=5.0, profile="hdr10_pq")
        self.assertGreater(float(boosted[0, 0, 0]), float(processed[0, 0, 0]))
        self.assertLessEqual(float(boosted.max()), 1.0)

    def test_preview_tone_map_returns_sdr_bgr(self):
        source = np.full((4, 6, 3), 180, np.uint8)
        preview = tone_map_hdr_preview(
            source, classify_color_info({"color_transfer": "smpte2084"}),
        )
        self.assertEqual(preview.shape, source.shape)
        self.assertEqual(preview.dtype, np.uint8)
        self.assertTrue(preview.flags.c_contiguous)


if __name__ == "__main__":
    unittest.main()
