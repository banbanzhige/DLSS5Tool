import unittest

import numpy as np

from dlss5tool.guidance_visualization import guidance_images


class GuidanceVisualizationTests(unittest.TestCase):
    def test_depth_is_bounded_grayscale_and_mode_filtered(self):
        depth = np.array([[-1.0, 0.5, 2.0]], np.float32)
        motion = np.zeros((1, 3, 2), np.float32)
        images = guidance_images(motion, depth, 2)
        self.assertEqual(set(images), {'depth'})
        self.assertEqual(images['depth'][0, :, 0].tolist(), [0, 128, 255])
        np.testing.assert_array_equal(images['depth'][..., 0], images['depth'][..., 1])

    def test_flow_uses_direction_hue_and_magnitude_brightness(self):
        motion = np.array([[[0, 0], [32, 0], [-32, 0], [0, 32], [0, -32]]], np.float32)
        image = guidance_images(motion, np.zeros((1, 5), np.float32), 1)['flow']
        self.assertEqual(image[0, 0].tolist(), [0, 0, 0])
        self.assertGreater(int(image[0, 1, 2]), 240)  # right: red
        self.assertGreater(int(image[0, 2, 0]), 240)  # left: cyan/blue component
        self.assertGreater(int(image[0, 3, 1]), 200)  # down: yellow-green
        self.assertGreater(int(image[0, 4, 2]), 100)  # up: purple contains red

    def test_nonfinite_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'depth'):
            guidance_images(np.zeros((1, 1, 2), np.float32), np.array([[np.nan]], np.float32), 2)
        with self.assertRaisesRegex(ValueError, 'flow'):
            guidance_images(np.array([[[np.inf, 0]]], np.float32), np.zeros((1, 1), np.float32), 1)


if __name__ == '__main__':
    unittest.main()
