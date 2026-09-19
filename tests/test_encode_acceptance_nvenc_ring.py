import unittest

from scripts.encode_acceptance_nvenc_ring import nv12_to_yuv420p, pack_yv12, surface_count, yuv420p_to_nv12


class NvencRingHarnessTests(unittest.TestCase):
    def test_surface_count_matches_ffmpeg_formula(self):
        self.assertEqual(surface_count(1, 0), 4)
        self.assertEqual(surface_count(3, 0), 12)
        self.assertEqual(surface_count(3, 8), 16)
        self.assertEqual(surface_count(1, 40), 32)

    def test_nv12_to_yuv420p_splits_uv(self):
        width, height = 2, 2
        y = bytes([1, 2, 3, 4])
        uv = bytes([10, 20])
        planar = nv12_to_yuv420p(y + uv, width, height)
        self.assertEqual(planar, y + bytes([10]) + bytes([20]))
        self.assertEqual(yuv420p_to_nv12(planar, width, height), y + uv)

    def test_pack_yv12_places_v_before_u(self):
        import numpy as np
        width, height, pitch = 2, 2, 4
        blob = bytes([1, 2, 3, 4, 9, 8])
        packed = pack_yv12(blob, width, height, pitch)
        self.assertEqual(packed[:8].tolist(), [1, 2, 0, 0, 3, 4, 0, 0])
        chroma = packed[8:]
        self.assertEqual(chroma[0], 8)
        self.assertEqual(chroma[pitch // 2], 9)

    def test_ring_source_keeps_preset_lookahead(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "scripts" / "gpu_nvenc_ring.cpp").read_text(encoding="utf-8")
        self.assertIn("NV_ENC_TUNING_INFO_HIGH_QUALITY", text)
        self.assertIn("enableLookahead", text)
        self.assertIn("NV_ENC_PIC_FLAG_EOS", text)
        self.assertNotIn("zeroReorderDelay = 1", text)


if __name__ == "__main__":
    unittest.main()
