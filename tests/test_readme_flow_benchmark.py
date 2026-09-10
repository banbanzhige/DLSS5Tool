import unittest

import cv2
import numpy as np

from scripts.readme_flow_benchmark import common_mask, photo_mae, windows
from scripts.nvofa_integration_probe import parser


class ReadmeFlowBenchmarkTests(unittest.TestCase):
    def test_disjoint_windows(self):
        self.assertEqual(windows(165), [0, 74, 149])
        self.assertEqual(windows(243), [0, 113, 227])
        with self.assertRaises(ValueError):
            windows(47)

    def test_shared_bounds_exclude_either_backend_outside(self):
        a = np.zeros((80, 80, 2), np.float32)
        b = a.copy()
        b[..., 0] = 40
        mask = common_mask([a, b])
        self.assertTrue(mask[30, 30])
        self.assertFalse(mask[30, 40])
        np.testing.assert_array_equal(mask, common_mask([b, a]))

    def test_backward_translation(self):
        prev = np.random.default_rng(42).integers(0, 256, (80, 80, 3), dtype=np.uint8)
        cur = cv2.warpAffine(prev, np.float32([[1, 0, 4], [0, 1, 2]]), (80, 80))
        backward = np.full((80, 80, 2), (-4, -2), np.float32)
        mask = common_mask([backward])
        self.assertEqual(photo_mae(prev, cur, backward, mask), 0)
        self.assertGreater(photo_mae(prev, cur, -backward, mask), 0)

    def test_signed_residuals_do_not_wrap_uint8(self):
        residual = np.full((80, 80, 3), -15, np.float32)
        flow = np.zeros((80, 80, 2), np.float32)
        self.assertEqual(photo_mae(residual, residual+3, flow, common_mask([flow])), 3)

    def test_grid_cli_preserves_default(self):
        base = ['--mods', 'mods', '--output', 'unused']
        self.assertEqual(parser().parse_args(base).grid, 4)
        self.assertEqual(parser().parse_args(base + ['--grid', '1']).grid, 1)


if __name__ == '__main__':
    unittest.main()
