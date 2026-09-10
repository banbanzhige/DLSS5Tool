"""No GPU or driver DLL required: policy, ABI, real worker orchestration."""
import contextlib
import ctypes
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from dlss5tool import app_settings, guidance_client, mod_paths, nvofa
from dlss5tool.guidance_flow import flow_backend, flow_contract, check_flow_handshake
from dlss5tool.guidance_parameters import analysis_parameters
from dlss5tool.guidance_worker import Models, ModelConfigurationError
from dlss5tool.guidance_cache import RawGuidanceCache


class NvofaTests(unittest.TestCase):
    def test_fixed_point_grid_and_abi(self):
        values = np.empty((2, 3, 2), np.int16)
        values[:] = (256, -128)
        result = nvofa.decode_flow(values, 144, 128)
        self.assertEqual(result.dtype, np.float32)
        np.testing.assert_array_equal(result, np.broadcast_to([8, -4], result.shape))
        self.assertEqual(nvofa.align_size(136, 128), (144, 128))
        self.assertEqual(nvofa.output_shape(144, 128, 4), (32, 36))
        self.assertEqual(nvofa.output_shape(144, 128, 2), (64, 72))
        self.assertEqual(nvofa.output_shape(144, 128, 1), (128, 144))
        self.assertEqual(ctypes.sizeof(nvofa.Init), 48)
        self.assertEqual(ctypes.sizeof(nvofa.ExecuteIn), 56)
        self.assertEqual(nvofa.Init.private.offset, 32)
        self.assertEqual(nvofa.ExecuteIn.private.offset, 32)
        self.assertEqual(ctypes.sizeof(nvofa.Copy2D), 128)

    def test_selection_inactive_cpu_and_missing_weights(self):
        values = {'guidance_mode': 1, 'guidance_flow_backend': 'nvofa'}
        self.assertEqual(app_settings.validate(values)['guidance_flow_backend'], 'nvofa')
        self.assertNotIn('flow_weights', mod_paths.guidance_candidates(values))
        self.assertNotIn('guidance_flow_updates', analysis_parameters(values))
        self.assertEqual(guidance_client.contract(values), guidance_client.contract({**values, 'guidance_flow_updates': 12}))
        self.assertNotEqual(guidance_client.contract(values), guidance_client.contract({**values, 'guidance_flow_backend': 'raft'}))
        self.assertEqual(app_settings.validate({**values, 'guidance_flow_grid': 1})['guidance_flow_grid'], 1)
        self.assertEqual(app_settings.validate({**values, 'guidance_flow_grid': 3})['guidance_flow_grid'], 4)
        self.assertEqual(flow_contract(values)['flow_grid'], 4)
        self.assertEqual(flow_contract({**values, 'guidance_flow_grid': 1})['flow_grid'], 1)
        self.assertIsNone(flow_contract({'guidance_mode': 1, 'guidance_flow_grid': 1})['flow_grid'])
        self.assertNotEqual(guidance_client.contract(values),
                            guidance_client.contract({**values, 'guidance_flow_grid': 1}))
        self.assertEqual(guidance_client.contract({'guidance_mode': 1}),
                         guidance_client.contract({'guidance_mode': 1, 'guidance_flow_grid': 1}))
        with self.assertRaisesRegex(ValueError, 'nvofa_cuda'):
            flow_backend({**values, 'guidance_device': 'cpu'}, strict=True)
        self.assertEqual(flow_backend({**values, 'guidance_mode': 0, 'guidance_device': 'cpu'}, strict=True), 'nvofa')
        with self.assertRaises(ValueError):
            flow_backend({'guidance_flow_backend': 'nvo'})
        with self.assertRaisesRegex(ValueError, 'nvofa_mixed'):
            flow_backend({**values, 'guidance_mode': 3}, strict=True)

    def test_legacy_manifest_rejects_nvofa_before_worker_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = root / 'enhancement'
            (component / '_internal').mkdir(parents=True)
            (component / 'guidance_worker.exe').touch()
            (component / '_internal/python313.dll').touch()
            manifest = {
                'id': 'dlss5-guidance', 'protocol': 1,
                'architectures': ['raft_large', 'depth_anything_v2'],
            }
            (component / 'enhancement.json').write_text(json.dumps(manifest), encoding='utf-8')
            settings = {'guidance_mode': 1, 'guidance_flow_backend': 'nvofa',
                        'mods_directory': str(root), 'ui_language': 'en_US'}
            self.assertEqual(mod_paths.flow_backends(settings), ('raft',))
            self.assertNotIn('flow_weights', mod_paths.guidance_candidates(settings))
            with self.assertRaisesRegex(ValueError, 'complete component'):
                guidance_client.validate(settings)
            manifest['flow_backends'] = ['raft', 'nvofa']
            (component / 'enhancement.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual(mod_paths.flow_backends(settings), ('raft', 'nvofa'))
            files = guidance_client.validate(settings)
            self.assertNotIn('flow_weights', files)
            self.assertEqual(files['worker'], str(component / 'guidance_worker.exe'))

    def test_handshake_all_fields_and_old_worker(self):
        settings = {'guidance_mode': 1, 'guidance_flow_backend': 'nvofa'}
        ready = flow_contract(settings)
        check_flow_handshake(settings, ready)
        for key in ready:
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_flow_handshake(settings, {**ready, key: 'wrong'})
        with self.assertRaisesRegex(ValueError, 'flow_grid_component'):
            check_flow_handshake({**settings, 'guidance_flow_grid': 1}, ready)
        with self.assertRaises(ValueError):
            check_flow_handshake(settings, {})
        check_flow_handshake({'guidance_mode': 1}, {})

    def test_initialization_fallback_is_reported_and_can_be_disabled(self):
        attempts = []
        def initialize(session, settings, w, h):
            attempts.append(settings['guidance_flow_backend'])
            if settings['guidance_flow_backend'] == 'nvofa':
                raise RuntimeError('fixture: OF unavailable')
            session.info = {'device': 'cuda'}
        request = {'guidance_mode': 1, 'guidance_flow_backend': 'nvofa'}
        with mock.patch.object(guidance_client.GuidanceSession, '_initialize', initialize):
            session = guidance_client.GuidanceSession(request, 128, 128)
            self.assertEqual(attempts, ['nvofa', 'raft'])
            self.assertEqual(session.info['flow_backend'], 'raft')
            self.assertIn('OF unavailable', session.info['flow_fallback_reason'])
            self.assertEqual(request['guidance_flow_backend'], 'nvofa')
            attempts.clear()
            with self.assertRaises(RuntimeError):
                guidance_client.GuidanceSession({**request, 'guidance_flow_fallback': False}, 128, 128)
            self.assertEqual(attempts, ['nvofa'])

    def test_runtime_failure_closes_without_reinitialization(self):
        session = guidance_client.GuidanceSession.__new__(guidance_client.GuidanceSession)
        session.width = session.height = 128
        session.language = 'en_US'
        session._sequence = 0
        session._buffers = None
        session._send = mock.Mock(side_effect=RuntimeError('runtime failure'))
        session.close = mock.Mock()
        with mock.patch.object(session, '_initialize') as initialize:
            with self.assertRaisesRegex(RuntimeError, 'runtime failure'):
                session.process(np.zeros((128, 128, 4), np.uint8))
            initialize.assert_not_called()
        session.close.assert_called_once()

    def model(self, direction='backward'):
        m = Models.__new__(Models)
        m.cv2, m.np = cv2, np
        m.torch = types.SimpleNamespace(inference_mode=contextlib.nullcontext, empty=lambda *a, **k: None)
        m.settings = {'guidance_mode': 1, 'guidance_flow_backend': 'nvofa',
                      'guidance_flow_edge': 512, 'guidance_flow_direction': direction}
        m.mode, m.flow_backend, m.device = 1, 'nvofa', 'cpu'
        m.flow = m.depth = m.flow_stream = m.depth_stream = m._nvof = None
        m._closed = m._failed = False
        m._process_lock = threading.Lock()
        m.prev = m.prev_thumb = m.prev_digest = m.depth_range = None
        m.raw_cache = RawGuidanceCache(4 * 1048576)
        m.execution_info = {'schedule': 'serial'}
        return m

    def test_worker_real_process_direction_cache_reset_resize_and_failure(self):
        class FakeFlow:
            def __init__(self, w, h, **kwargs):
                self.w, self.h, self.calls, self.closed = w, h, 0, False
                self.grid = kwargs.get('grid', 4)
            def calculate(self, first, second):
                self.calls += 1
                assert first.dtype == np.uint8 and first.flags.c_contiguous
                self.pair_means = (float(first.mean()), float(second.mean()))
                return np.full((self.h, self.w, 2), float(second.mean() - first.mean()), np.float32)
            def close(self):
                self.closed = True
        for direction in ('backward', 'forward_negated'):
            with self.subTest(direction=direction), mock.patch.object(nvofa, 'OpticalFlow', FakeFlow):
                m = self.model(direction)
                a = np.full((128, 136, 4), 10, np.uint8)
                b = np.full_like(a, 11)
                self.assertFalse(m.process(a, True)[0].any())
                motion, depth, reset = m.process(b, False)
                self.assertFalse(reset)
                self.assertFalse(depth.any())
                np.testing.assert_allclose(motion[0, 0], [-136 / 144, -1])
                self.assertEqual(m.last_metrics['flow_size'], [144, 128])
                self.assertIsNone(m.last_metrics['flow_updates'])
                engine = m._nvof
                self.assertEqual(engine.pair_means,
                                 (11.0, 10.0) if direction == 'backward' else (10.0, 11.0))
                m.process(a, True)
                np.testing.assert_array_equal(m.process(b, False)[0], motion)
                self.assertTrue(m.last_metrics['cache_flow_hit'])
                self.assertEqual(engine.calls, 1)
                m.process(np.full((128, 160, 4), 10, np.uint8), False)
                m.process(np.full((128, 160, 4), 11, np.uint8), False)
                self.assertTrue(engine.closed)
                m._nvof.calculate = mock.Mock(side_effect=RuntimeError('driver lost'))
                with self.assertRaises(ModelConfigurationError):
                    m.process(np.full((128, 160, 4), 12, np.uint8), False)
                self.assertTrue(m._failed)
                self.assertEqual(m.raw_cache.bytes, 0)
                m.close()
                m.close()

    def test_partial_initialization_releases_context_and_all_buffers(self):
        captured = []
        def load(engine, require_current):
            captured.append(engine)
            engine.context = nvofa.P(123)
            engine._retained = True
            engine.device = nvofa.I(0)
            engine.push = mock.Mock(return_value=0)
            engine.pop = mock.Mock(return_value=0)
            engine.release = mock.Mock(return_value=0)
            engine.free = mock.Mock(side_effect=[RuntimeError('free failed'), 0])
            engine.destroy = mock.Mock(return_value=0)
        def initialize(engine):
            engine.handle = nvofa.P(321)
            engine.buffers = [nvofa.P(1), nvofa.P(2)]
            raise RuntimeError('init failed')
        with mock.patch.object(nvofa.OpticalFlow, '_load', load), mock.patch.object(nvofa.OpticalFlow, '_initialize', initialize):
            with self.assertRaisesRegex(RuntimeError, 'init failed'):
                nvofa.OpticalFlow(128, 128)
        engine = captured[0]
        self.assertEqual(engine.free.call_count, 2)
        engine.destroy.assert_called_once()
        engine.release.assert_called_once()
        self.assertEqual(engine.push.call_count, engine.pop.call_count)
        engine.close()
        engine.release.assert_called_once()
