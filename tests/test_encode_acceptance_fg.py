import unittest
import numpy as np

from scripts.encode_acceptance_fg import timeline_events, nvofa_motion


class FrameGenerationAcceptanceTests(unittest.TestCase):
    def test_complete_timeline_has_originals_intermediates_and_endpoint_holds(self):
        events = timeline_events(4, 2, cuts=(2,))
        self.assertEqual([e['kind'] for e in events], [
            'original', 'generated', 'original', 'hold', 'original',
            'generated', 'original', 'hold'])
        self.assertEqual(sum(e['kind'] == 'original' for e in events), 4)
        self.assertEqual(sum(e['kind'] == 'generated' for e in events), 2)

    def test_timeline_multiplier_four(self):
        events = timeline_events(4, 4)
        self.assertEqual(len(events), 4 * 4)
        self.assertEqual(events[-1]['kind'], 'hold')

    def test_motion_uses_padded_rgb_and_preserves_shape(self):
        class FakeFlow:
            def calculate(self, first, second):
                self.shapes = (first.shape, second.shape)
                return np.zeros((first.shape[0], first.shape[1], 2), np.float32)
        flow = FakeFlow()
        previous = np.zeros((3, 5, 4), np.uint8)
        current = np.full_like(previous, 1)
        result = nvofa_motion(flow, current, previous, (8, 4))
        self.assertEqual(flow.shapes, ((4, 8, 3), (4, 8, 3)))
        self.assertEqual(result.shape, (3, 5, 2))
        self.assertEqual(result.dtype, np.float32)

    def test_invalid_timeline_rejected(self):
        with self.assertRaises(ValueError): timeline_events(0, 2)
        with self.assertRaises(ValueError): timeline_events(2, 1)


if __name__ == '__main__':
    unittest.main()
