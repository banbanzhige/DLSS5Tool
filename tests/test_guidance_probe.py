"""CPU-only checks for the isolated experiment, without loading a native DLL."""
import argparse
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from scripts.guidance_probe import differences, prepare, temporal_residual


class GuidanceProbeTests(unittest.TestCase):
    def test_affine_backward_flow_and_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = np.zeros((160, 256, 3), np.uint8)
            cv2.circle(source, (120, 80), 40, (60, 150, 220), -1)
            cv2.imwrite(str(root / "source.png"), source)
            args = argparse.Namespace(output=root / "run", image=root / "source.png",
                                      width=256, height=160, frames=3)
            prepare(args)
            with self.assertRaises(FileExistsError):
                prepare(args)  # Never overwrite a prior experiment.
            with np.load(args.output / "inputs.npz") as data:
                self.assertEqual(data["frames"].shape, (3, 160, 256, 4))
                self.assertTrue(np.isfinite(data["flow"]).all())
                self.assertEqual(float(np.abs(data["flow"][0]).max()), 0.0)
                self.assertGreater(float(np.abs(data["flow"][1]).mean()), 0.1)
                matrix = cv2.getRotationMatrix2D((128, 80), 0.35, 1.006)
                matrix[:, 2] += (2.5, 0.6)
                inv = np.linalg.inv(np.vstack((matrix, [0, 0, 1])))
                previous = inv @ np.array([120.0, 80.0, 1.0])
                np.testing.assert_allclose(data["flow"][1, 80, 120], previous[:2] - (120, 80), atol=1e-5)
                metrics = temporal_residual(data["frames"], data["frames"], data["flow"], data["masks"])
                self.assertEqual(metrics["mean_255"], 0.0)

    def test_difference_counts_do_not_wrap_uint8(self):
        a = np.zeros((2, 2, 2, 4), np.uint8)
        b = np.full_like(a, 255)
        result = differences(a, b)
        self.assertFalse(result["exact_equal"])
        self.assertEqual(result["rgb_mae_255"], 255.0)
        self.assertEqual(result["changed_rgb_fraction"], 1.0)
        self.assertTrue(differences(a, a)["exact_equal"])


if __name__ == "__main__":
    unittest.main()
