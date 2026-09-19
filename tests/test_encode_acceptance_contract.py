import unittest
from pathlib import Path

from scripts.encode_acceptance_nvenc_contract import (
    FMT_ABGR, FMT_ARGB, FMT_NV12, pack_nv12, nv12_pitch,
)


class NvencContractHarnessTests(unittest.TestCase):
    def test_format_constants_match_native_probe(self):
        text = (Path(__file__).resolve().parents[1] / "scripts" / "gpu_nvenc_probe.cpp").read_text(encoding="utf-8")
        self.assertIn("NV_ENC_BUFFER_FORMAT_NV12", text)
        self.assertIn("NV_ENC_TUNING_INFO_HIGH_QUALITY", text)
        self.assertIn("targetQuality", text)
        self.assertEqual((FMT_ABGR, FMT_NV12, FMT_ARGB), (0, 1, 2))

    def test_nv12_pack_preserves_planes_on_aligned_pitch(self):
        import numpy as np
        width, height = 8, 4
        pitch = nv12_pitch(width)
        self.assertGreaterEqual(pitch, width)
        y = np.arange(width * height, dtype=np.uint8)
        uv = np.arange(width * height // 2, dtype=np.uint8)
        packed = pack_nv12(y.tobytes() + uv.tobytes(), width, height, pitch)
        self.assertEqual(packed.shape, (height + height // 2, pitch))
        self.assertEqual(packed[:height, :width].reshape(-1).tolist(), y.tolist())
        self.assertEqual(packed[height:, :width].reshape(-1).tolist(), uv.tolist())
        self.assertTrue((packed[:height, width:] == 0).all())

    def test_rejects_wrong_nv12_size(self):
        with self.assertRaises(ValueError):
            pack_nv12(b"\x00" * 3, 8, 4, 256)


if __name__ == "__main__":
    unittest.main()
