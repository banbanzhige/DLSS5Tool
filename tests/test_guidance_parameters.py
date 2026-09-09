import unittest
from unittest import mock
import importlib.util

import numpy as np
from dlss5tool import app_settings
from dlss5tool import guidance_client
from dlss5tool.guidance_parameters import parameters, analysis_parameters, check_parameter_handshake, analysis_edge
from dlss5tool.guidance_visualization import guidance_images
from dlss5tool.guidance_worker import Models


class ParameterTests(unittest.TestCase):
    def test_legacy_migration_and_independent_settings(self):
        old = app_settings.validate({'guidance_edge': 512})
        self.assertEqual(old['guidance_flow_edge'], 512)
        self.assertEqual(old['guidance_depth_edge'], 512)
        self.assertEqual(old['guidance_flow_updates'], 6)
        new = app_settings.validate({**old, 'guidance_depth_edge': 1024})
        self.assertEqual(new['guidance_flow_edge'], 512)
        self.assertEqual(new['guidance_depth_edge'], 1024)
        self.assertEqual(analysis_edge({**new, 'guidance_mode': 3}), 1024)
        self.assertEqual(analysis_edge({**new, 'guidance_mode': 1}), 512)
        self.assertEqual(new, app_settings.validate(new))

    def test_validation_and_contract(self):
        for value in ('bad', 'nan', float('inf'), 0, 33, 6.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parameters({'guidance_flow_updates': value}, strict=True)
        for key in ('guidance_flow_updates', 'guidance_depth_edge', 'guidance_flow_edge',
                    'guidance_depth_smoothing', 'guidance_depth_low', 'guidance_depth_high'):
            baseline = parameters({})
            changed = {**baseline, key: baseline[key] - (0.1 if 'smoothing' in key else 1)}
            self.assertNotEqual(guidance_client.contract(baseline), guidance_client.contract(changed))
        self.assertEqual(guidance_client.contract({}), guidance_client.contract({'guidance_flow_range': 8}))

    def test_legacy_component_only_allowed_for_legacy_values(self):
        settings = {'guidance_mode': 3, 'guidance_edge': 512}
        check_parameter_handshake(settings, {})
        for changed in ({'guidance_flow_updates': 12}, {'guidance_depth_edge': 720},
                        {'guidance_depth_smoothing': 0.5}, {'guidance_depth_low': 2}):
            requested = {**settings, **changed}
            with self.assertRaisesRegex(ValueError, 'parameters_component'):
                check_parameter_handshake(requested, {})
            check_parameter_handshake(requested, {'analysis_parameters': analysis_parameters(requested)})
            with self.assertRaises(ValueError):
                check_parameter_handshake(requested, {'analysis_parameters': {}})
        check_parameter_handshake({'guidance_mode': 1, 'guidance_depth_edge': 1024}, {})

    def test_visualization_only_and_default_identity(self):
        flow = np.full((8, 8, 2), 4, np.float32)
        depth = np.linspace(0, 1, 64, dtype=np.float32).reshape(8, 8)
        originals = flow.copy(), depth.copy()
        baseline = guidance_images(flow, depth, 3)
        explicit = guidance_images(flow, depth, 3, parameters({}))
        for key in baseline:
            np.testing.assert_array_equal(baseline[key], explicit[key])
        changed = guidance_images(flow, depth, 3, {'guidance_flow_range': 8,
            'guidance_depth_palette': 'turbo', 'guidance_depth_invert': True})
        for key in baseline:
            self.assertFalse(np.array_equal(baseline[key], changed[key]))
        np.testing.assert_array_equal(flow, originals[0])
        np.testing.assert_array_equal(depth, originals[1])

    def test_depth_range_parameters_and_reset(self):
        import cv2
        model = Models.__new__(Models)
        model.cv2, model.np = cv2, np
        model.settings = {'guidance_depth_smoothing': 0.5, 'guidance_depth_low': 5, 'guidance_depth_high': 95}
        model._depth_key = None
        model.depth_range = (0, 100)
        prediction = np.arange(100, dtype=np.float32).reshape(10, 10)
        output = np.empty_like(prediction)
        bounds = model._finish_depth(prediction, output, False, 10, 10)
        np.testing.assert_allclose(bounds, (2.475, 97.025))
        bounds = model._finish_depth(prediction, output, True, 10, 10)
        np.testing.assert_allclose(bounds, (4.95, 94.05))
        self.assertTrue(np.isfinite(output).all())

    def test_iterations_forwarded_to_model(self):
        model = Models.__new__(Models)
        model.settings, model.device = {'guidance_flow_updates': 12}, 'cpu'
        model.flow = mock.Mock(return_value=['first', 'last'])
        first, second = mock.Mock(), mock.Mock()
        self.assertEqual(model._infer_flow((first, second)), 'last')
        self.assertEqual(model.flow.call_args.kwargs['num_flow_updates'], 12)


@unittest.skipUnless(importlib.util.find_spec('torch'), 'requires Torch')
class ResolutionTests(unittest.TestCase):
    def test_depth_uses_original_when_flow_resolution_is_lower(self):
        from tests.test_guidance_cache import ModelCacheTests
        model = ModelCacheTests().model()
        model.settings.update(guidance_flow_edge=128, guidance_depth_edge=256)
        frame = np.random.default_rng(4).integers(0, 255, (512, 512, 4), dtype=np.uint8)
        model.process(frame, True)
        depth_input = model._depth_input.call_args.args[0]
        self.assertEqual(depth_input.shape, (256, 256, 3))
        np.testing.assert_array_equal(depth_input, model.cv2.resize(frame[..., :3], (256, 256)))
        self.assertEqual(model.prev.shape, (128, 128, 3))
        self.assertEqual(model.last_metrics['depth_size'], [252, 252])
        model.close()


if __name__ == '__main__':
    unittest.main()
