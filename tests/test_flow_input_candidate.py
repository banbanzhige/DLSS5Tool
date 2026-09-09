import importlib.util
import unittest
from types import SimpleNamespace

import numpy as np


@unittest.skipUnless(importlib.util.find_spec('torch'), 'requires Torch')
class FlowInputCandidateTests(unittest.TestCase):
    def test_bitwise_identity_directions_noncontiguous_and_discontinuous_frames(self):
        import torch
        from torchvision.models.optical_flow import Raft_Large_Weights
        from dlss5tool.guidance_inputs import prepare_flow, clear_flow_inputs
        from scripts.flow_input_candidate import original_flow_input
        for direction in ('backward', 'forward_negated'):
            model = SimpleNamespace(np=np, torch=torch, transforms=Raft_Large_Weights.DEFAULT.transforms(),
                                    settings={'guidance_flow_direction': direction}, prev=None)
            rng = np.random.default_rng(2)
            frames = [rng.integers(0, 256, (129, 131, 4), dtype=np.uint8)[..., :3] for _ in range(4)]
            model.prev = frames[0]
            for i in (1, 2, 0, 3):
                actual = prepare_flow(model, frames[i])
                expected = original_flow_input(model, frames[i])
                for a, b in zip(actual, expected):
                    self.assertTrue(torch.equal(a, b))
                    self.assertTrue(a.is_contiguous())
                    self.assertEqual(a.dtype, torch.float32)
                model.prev = frames[i]
            # Full byte range must match, not just a natural image sample.
            ramp = np.broadcast_to(np.arange(256, dtype=np.uint8)[None, :, None], (8, 256, 3))
            model.prev = ramp
            for a, b in zip(prepare_flow(model, ramp), original_flow_input(model, ramp)):
                self.assertTrue(torch.equal(a, b))
            clear_flow_inputs(model)
            self.assertIsNone(model._flow_prepared_source)
            self.assertIsNone(model._flow_prepared_tensor)
            self.assertIsNone(model._flow_normalization_lut)

    def test_worker_cache_seek_cut_resize_and_failure_preserve_reference(self):
        from unittest import mock
        import types
        import torch
        from torchvision.models.optical_flow import Raft_Large_Weights
        from dlss5tool.guidance_worker import Models
        from tests.test_guidance_cache import ModelCacheTests
        from scripts.flow_input_candidate import original_flow_input
        for direction in ('backward', 'forward_negated'):
            for mode in (1, 2, 3):
                models = [ModelCacheTests().model(budget=8, mode=mode) for _ in range(2)]
                for i, model in enumerate(models):
                    model.settings['guidance_flow_direction'] = direction
                    model.transforms = Raft_Large_Weights.DEFAULT.transforms()
                    model._flow_input = types.MethodType(Models._flow_input if i else original_flow_input, model)
                    model._infer_flow = mock.Mock(side_effect=lambda inputs: (inputs[0] - inputs[1])[:, :2])
                frame = np.random.default_rng(3).integers(0, 100, (128, 136, 4), dtype=np.uint8)
                # Cold continuous pair, cache hits, explicit seek, cut, resize.
                sequence = [(frame, True), (np.roll(frame, 1, axis=1), False), (frame, True),
                            (np.roll(frame, 1, axis=1), False), (np.roll(frame, 2, axis=1), True),
                            (np.full_like(frame, 255), False),
                            (np.zeros((152, 128, 4), np.uint8), False)]
                for data, reset in sequence:
                    baseline = models[0].process(data, reset)
                    candidate = models[1].process(data, reset)
                    np.testing.assert_array_equal(baseline[0], candidate[0])
                    np.testing.assert_array_equal(baseline[1], candidate[1])
                    self.assertEqual(baseline[2], candidate[2])
                models[1]._flow_prepared_source = frame
                models[1]._flow_prepared_tensor = torch.zeros(1)
                models[1]._process_frame = mock.Mock(side_effect=RuntimeError('test failure'))
                with self.assertRaises(RuntimeError):
                    models[1].process(frame, False)
                self.assertIsNone(models[1]._flow_prepared_source)
                for model in models:
                    model.close()
                    self.assertIsNone(model._flow_prepared_tensor)
