"""Still-only flow preference; no real models or GPU needed."""
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import numpy as np
import cv2

from dlss5tool import app_settings, guidance_client, gui, i18n
from dlss5tool.export_queue import ExportJob
from dlss5tool.guidance_public import still_image_settings
from dlss5tool.guidance_cache import RawGuidanceCache
from dlss5tool.guidance_worker import Models
from tests import test_gui_module_reload as reload_tests


class StillFlowPolicyTests(unittest.TestCase):
    def test_default_migration_and_boolean_validation(self):
        for saved in ({}, {'guidance_skip_still_flow': 'false'}, {'guidance_skip_still_flow': None}):
            self.assertTrue(app_settings.validate(saved)['guidance_skip_still_flow'])
        self.assertFalse(app_settings.validate({'guidance_skip_still_flow': False})['guidance_skip_still_flow'])

    def test_all_modes_and_immutable_queue_preferences(self):
        for mode in range(4):
            for skip in (True, False):
                saved = {'guidance_mode': mode, 'guidance_skip_still_flow': skip}
                job = ExportJob.from_dict(dict(source_path='in.png', output_path='out.png', settings=saved))
                result = still_image_settings(job.settings)
                self.assertEqual(result['guidance_mode'], {1: 0, 3: 2}.get(mode, mode) if skip else mode)
                self.assertEqual(bool(result.get('_still_flow_skipped')), skip and mode in (1, 3))
                self.assertEqual(job.settings, saved)

    def test_still_plan_off_restores_both_flow_backends(self):
        for backend in ('raft', 'nvofa'):
            saved = {'guidance_mode': 1, 'guidance_skip_still_flow': False, 'guidance_flow_backend': backend}
            with mock.patch.object(gui, 'cached_gpu_memory', return_value=None):
                self.assertEqual(gui._large_image_host_settings(512, 512, saved), saved)

    def test_skipped_still_needs_neither_files_nor_worker(self):
        settings = still_image_settings({'guidance_mode': 1})
        with mock.patch.object(guidance_client.mod_paths, 'guidance_files') as files, \
                mock.patch.object(guidance_client, 'GuidanceSession') as worker:
            self.assertEqual(guidance_client.preflight(settings), {})
        files.assert_not_called()
        worker.assert_not_called()

    def test_tiling_does_not_silently_override_disabled_skip(self):
        with mock.patch.object(gui, 'cached_gpu_memory', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Skip flow'):
                gui._large_image_host_settings(12000, 1000, {
                    'guidance_mode': 1, 'guidance_skip_still_flow': False, 'ui_language': 'en_US'})

    def test_image_export_passes_effective_mode_and_logs_only_when_skipped(self):
        for skip in (True, False):
            app = gui.App.__new__(gui.App)
            app._live_lock = threading.RLock()
            app.logln = mock.Mock()
            app._ensure_live = mock.Mock()
            rgba = np.zeros((12, 16, 4), np.uint8)
            app._ensure_live.return_value.process.return_value = rgba
            saved = {'guidance_mode': 1, 'guidance_skip_still_flow': skip}
            with mock.patch.object(gui, 'cached_gpu_memory', return_value=None):
                app._process_still_image(rgba[..., :3], saved)
            effective = app._ensure_live.call_args.args[2]
            self.assertEqual(effective['guidance_mode'], 0 if skip else 1)
            self.assertEqual(app.logln.call_count, int(skip))
            self.assertEqual(saved['guidance_mode'], 1)


class StillActivationTests(unittest.TestCase):
    setUp = reload_tests.ModuleReloadTests.setUp
    wait_reload = reload_tests.ModuleReloadTests.wait_reload

    def test_image_skips_preflight_and_video_keeps_it(self):
        for kind, skip in (('image', True), ('image', False), ('video', True)):
            self.setUp()
            app = self.app
            app._source_kind = kind
            app._image_bgr = np.zeros((12, 16, 3), np.uint8) if kind == 'image' else None
            selected = mock.Mock()
            selected.get.return_value = i18n.tr('guidance.mode.1')
            selected.set.side_effect = lambda value: setattr(selected.get, 'return_value', value)
            app._host_settings = {'v_guidance': selected}
            app._last_module_settings = {'guidance_mode': 0}
            app._collect_host_settings = lambda: {'guidance_mode': 1, 'guidance_skip_still_flow': skip}
            with mock.patch.object(guidance_client, 'preflight', return_value={'device': 'cuda'}) as probe:
                app._on_mod_settings_change()
                self.wait_reload()
            self.assertEqual(probe.call_count, 0 if kind == 'image' and skip else 1)
            self.assertEqual(selected.get(), i18n.tr('guidance.mode.1'))
            self.assertFalse(app._switching_backend)


class WithdrawnDepthTests(unittest.TestCase):
    def test_public_flow_frames_never_invoke_depth(self):
        model = Models.__new__(Models)
        model.cv2, model.np = cv2, np
        model.torch = SimpleNamespace(inference_mode=nullcontext)
        model.settings = {'guidance_mode': 1, 'guidance_flow_edge': 128, 'guidance_depth_edge': 256}
        model.mode, model.device = 1, 'cpu'
        model.flow, model.depth = object(), None
        model.flow_stream = model.depth_stream = None
        model.execution_info = {'schedule': 'serial'}
        model.raw_cache = RawGuidanceCache(0)
        model.prev = model.prev_thumb = model.prev_digest = model.depth_range = None
        model._flow_input = mock.Mock(return_value=None)
        model._infer_flow = mock.Mock(return_value=None)
        model._finish_flow = mock.Mock()
        model._depth_input = mock.Mock(side_effect=AssertionError('depth input must stay inactive'))
        model._infer_depth = mock.Mock(side_effect=AssertionError('depth model must stay inactive'))
        image = np.zeros((128, 128, 4), np.uint8)
        for reset in (True, False):
            with mock.patch.object(cv2, 'resize', wraps=cv2.resize) as resize:
                _motion, depth, _reset = model._process_frame(image, reset)
            self.assertEqual([call.args[1] for call in resize.call_args_list], [(64, 36), (128, 128)])
            self.assertFalse(depth.any())
            self.assertEqual(model.last_metrics['depth_model_calls'], 0)
            self.assertIsNone(model.last_metrics['depth_size'])
        model._depth_input.assert_not_called()
        model._infer_depth.assert_not_called()
        model._infer_flow.assert_called_once()
        from dlss5tool.guidance_transport import FLOW_ONLY
        model.settings['guidance_output_layout'] = FLOW_ONLY
        self.assertIsNone(model._process_frame(image, True)[1])
        motion = np.empty((128, 128, 2), np.float32)
        self.assertIsNone(model._process_frame(image, True, outputs=(motion, None))[1])


if __name__ == '__main__':
    unittest.main()
