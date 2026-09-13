from pathlib import Path
import json
import tempfile
import unittest
from tests.depth_fixture import depth_test_case
from unittest import mock

import numpy as np

from dlss5tool import app_settings
from dlss5tool import dlss_engine
from dlss5tool import guidance_client
from dlss5tool import mod_paths
from dlss5tool import guidance_worker


@depth_test_case
class GuidanceDeviceTests(unittest.TestCase):
    def test_depth_profile_defaults_cpu_policy_and_flow_only(self):
        for mode in (1, 2, 3):
            self.assertEqual(guidance_worker.depth_profile({'guidance_mode': mode}, 'cuda'), 'fp32')
        request = {'guidance_mode': 3, 'guidance_depth_profile': 'sdpa_fp16'}
        self.assertEqual(guidance_worker.depth_profile(request, 'cuda'), 'sdpa_fp16')
        self.assertEqual(guidance_worker.depth_profile({**request, 'guidance_mode': 1}, 'cpu'), 'fp32')
        with self.assertRaises(guidance_worker.ModelConfigurationError):
            guidance_worker.depth_profile(request, 'cpu')
        with self.assertRaises(guidance_worker.ModelConfigurationError):
            guidance_worker.depth_profile({**request, 'guidance_depth_profile': 'bf16'}, 'cuda')
        with self.assertRaisesRegex(ValueError, 'CUDA'):
            guidance_client.validate({**request, 'guidance_device': 'cpu', 'ui_language': 'en_US'})

    def test_acceleration_requires_explicit_component_acknowledgement(self):
        request = {'guidance_mode': 3, 'guidance_depth_profile': 'sdpa_fp16'}
        ready = {'depth_profile': 'sdpa_fp16', 'depth_attention': 'sdpa', 'depth_precision': 'float16_amp'}
        guidance_client.check_depth_handshake(request, ready, 'en_US')
        for key in ready:
            with self.assertRaisesRegex(RuntimeError, 'Original FP32'):
                guidance_client.check_depth_handshake(request, {k: v for k, v in ready.items() if k != key}, 'en_US')
        with self.assertRaisesRegex(RuntimeError, 'Original FP32'):
            guidance_client.check_depth_handshake(request, {}, 'en_US')
        guidance_client.check_depth_handshake({'guidance_mode': 3}, {}, 'en_US')
        guidance_client.check_depth_handshake({**request, 'guidance_mode': 1}, {}, 'en_US')
        with self.assertRaises(RuntimeError):
            guidance_client.check_depth_handshake({'guidance_mode': 3}, ready, 'en_US')

    def test_profile_persistence_contract_and_safe_defaults(self):
        self.assertEqual(app_settings.validate({})['guidance_depth_profile'], 'sdpa_fp16')
        self.assertEqual(app_settings.validate({'guidance_depth_profile': 'bad'})['guidance_depth_profile'], 'sdpa_fp16')
        settings = app_settings.validate({'guidance_depth_profile': 'sdpa_fp16'})
        self.assertEqual(settings['guidance_depth_profile'], 'sdpa_fp16')
        self.assertNotEqual(guidance_client.contract(settings), guidance_client.contract({**settings, 'guidance_depth_profile': 'fp32'}))

    def test_parent_rejects_silent_cpu_fallback_before_processing(self):
        # Exercise the real handshake policy without loading torch or an EXE.
        ready = {'ok': True, 'protocol': 1, 'device': 'cpu'}
        connection = mock.Mock()
        connection.recv_bytes.return_value = json.dumps(ready).encode()
        listener = mock.Mock()
        listener.accept.return_value = connection
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(guidance_client, 'validate', return_value={'worker': 'fixture.exe'}), \
                mock.patch.object(guidance_client, 'Listener', return_value=listener), \
                mock.patch.object(guidance_client.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'CUDA'):
                guidance_client.GuidanceSession({'guidance_mode': 1, 'guidance_device': 'auto'}, 8, 6)
        process.terminate.assert_called_once()

    def test_auto_requires_gpu_and_cpu_is_explicit(self):
        self.assertEqual(guidance_worker.select_device('auto', True), 'cuda')
        self.assertEqual(guidance_worker.select_device('cuda', True), 'cuda')
        self.assertEqual(guidance_worker.select_device('cpu', False), 'cpu')
        self.assertEqual(guidance_worker.select_device('cpu', True), 'cpu')
        for device in ('auto', 'cuda'):
            with self.assertRaises(guidance_worker.ModelConfigurationError) as error:
                guidance_worker.select_device(device, False)
            self.assertEqual(error.exception.key, 'guidance.error.cuda')
        with self.assertRaises(guidance_worker.ModelConfigurationError):
            guidance_worker.select_device('invalid', True)


@depth_test_case
class ModPathsTests(unittest.TestCase):
    def setUp(self):
        self.actual_app_root = mod_paths.app_root
        # The real development mods directory may contain private test models.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(mod_paths, 'app_root', return_value=Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def component(self, root):
        component = root / 'enhancement'
        (component / '_internal').mkdir(parents=True, exist_ok=True)
        (component / 'guidance_worker.exe').touch()
        (component / '_internal/python313.dll').touch()
        (component / 'enhancement.json').write_text(json.dumps({
            'id': 'dlss5-guidance', 'protocol': 1,
            'architectures': ['raft_large', 'depth_anything_v2']}), encoding='utf-8')
        return component

    def test_component_contract_and_incomplete_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = {'mods_directory': str(root), 'ui_language': 'en_US'}
            component = self.component(root)
            self.assertEqual(mod_paths.enhancement_info(settings)['worker'], str(component / 'guidance_worker.exe'))
            manifest = component / 'enhancement.json'
            for payload in ([], {'id': 'other'}, {'id': 'dlss5-guidance', 'protocol': 2},
                            {'id': 'dlss5-guidance', 'protocol': True}, {'executable': '../../evil.exe'}):
                manifest.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'incompatible'):
                    mod_paths.enhancement_info(settings)
            self.component(root)
            (component / '_internal/python313.dll').unlink()
            with self.assertRaises(ValueError):
                mod_paths.enhancement_info(settings)

    def test_runtime_detection_custom_priority_and_fallback(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(mod_paths, 'app_root', return_value=Path(directory)), \
                mock.patch.object(mod_paths.paths, 'runtime_root', return_value=Path(directory) / 'runtime'):
            root = Path(directory)
            self.assertEqual(mod_paths.runtime_path(), str(root / 'runtime/nvngx_dlssnr.dll'))
            (root / 'mods').mkdir()
            (root / 'mods/nvngx_dlssnr.dll').touch()
            self.assertEqual(mod_paths.runtime_path(), str(root / 'mods/nvngx_dlssnr.dll'))
            self.assertEqual(mod_paths.runtime_path({'dlss_runtime': '__bundled__'}), str(root / 'runtime/nvngx_dlssnr.dll'))
            self.assertEqual(mod_paths.runtime_path({'dlss_runtime': 'nvngx_dlssnr.dll'}), str(root / 'mods/nvngx_dlssnr.dll'))
            chosen = root / 'custom.dll'
            chosen.touch()
            self.assertEqual(mod_paths.runtime_path({'dlss_runtime': str(chosen)}), str(chosen))
            fallback = mod_paths.runtime_info({'dlss_runtime': 'missing.dll'})
            self.assertTrue(fallback['fallback'])
            self.assertEqual(fallback['path'], str(root / 'mods/nvngx_dlssnr.dll'))

    def test_frozen_mods_beside_exe_not_internal(self):
        with mock.patch.object(mod_paths, 'app_root', self.actual_app_root), mock.patch.object(mod_paths.sys, 'frozen', True, create=True), mock.patch.object(mod_paths.sys, 'executable', 'F:/portable/DLSS5Tool.exe'), mock.patch.object(mod_paths.sys, '_MEIPASS', 'F:/portable/_internal', create=True):
            self.assertEqual(mod_paths.mods_root(), Path('F:/portable/mods'))

    def test_off_does_not_check_component_or_models(self):
        with mock.patch.object(mod_paths, 'enhancement_info', side_effect=AssertionError('must not inspect dependencies')):
            self.assertEqual(guidance_client.validate({'guidance_mode': 0}), {})
            self.assertEqual(guidance_client.validate({}), {})

    def test_only_selected_models_required(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(mod_paths, 'mods_root', return_value=Path(directory)):
            root = Path(directory)
            self.component(root)
            (root / 'models').mkdir()
            with self.assertRaisesRegex(FileNotFoundError, 'raft_large'):
                mod_paths.guidance_files({'guidance_mode': 1})
            (root / 'models/raft_large_C_T_SKHT_V2-ff5fadd5.pth').touch()
            self.assertNotIn('depth_weights', mod_paths.guidance_files({'guidance_mode': 1}))
            with self.assertRaisesRegex(FileNotFoundError, 'depth_anything'):
                mod_paths.guidance_files({'guidance_mode': 3})

    def test_component_defaults_and_user_weights_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = self.component(root)
            (component / 'models').mkdir()
            name = 'raft_large_C_T_SKHT_V2-ff5fadd5.pth'
            (component / 'models' / name).touch()
            settings = {'mods_directory': str(root), 'guidance_mode': 1}
            self.assertEqual(guidance_client.validate(settings)['flow_weights'], str(component / 'models' / name))
            (root / 'models').mkdir()
            (root / 'models' / name).touch()
            self.assertEqual(guidance_client.validate(settings)['flow_weights'], str(root / 'models' / name))

    def test_broken_selected_component_never_executes_default(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(mod_paths, 'app_root', return_value=Path(directory)):
            root = Path(directory)
            self.component(root / 'mods')
            (root / 'selected/enhancement').mkdir(parents=True)
            with self.assertRaises(ValueError):
                mod_paths.enhancement_info({'mods_directory': str(root / 'selected')})

    def test_auto_depth_size_from_installed_weights_and_explicit_override(self):
        root = mod_paths.mods_root()
        component = self.component(root)
        (component / 'models').mkdir()
        (component / 'models/depth_anything_v2_vits.pth').touch()
        settings = app_settings.validate({'guidance_mode': 2, 'guidance_depth_encoder': 'auto'})
        self.assertEqual(settings['guidance_depth_encoder'], 'auto')
        self.assertEqual(mod_paths.depth_encoder(settings), 'vits')
        self.assertIn('vits.pth', guidance_client.validate(settings)['depth_weights'])
        (root / 'models').mkdir()
        (root / 'models/depth_anything_v2_vitb.pth').touch()
        self.assertEqual(mod_paths.depth_encoder(settings), 'vitb')
        self.assertEqual(mod_paths.depth_encoder({**settings, 'guidance_depth_encoder': 'vits'}), 'vits')
        with self.assertRaises(FileNotFoundError):
            guidance_client.validate({**settings, 'guidance_depth_encoder': 'vitl'})

    def test_component_default_size_and_custom_weight_filename(self):
        root = mod_paths.mods_root()
        component = self.component(root)
        manifest = component / 'enhancement.json'
        payload = json.loads(manifest.read_text())
        payload['default_depth_encoder'] = 'vits'
        manifest.write_text(json.dumps(payload))
        for name in ('vits', 'vitl'):
            (root / f'depth_anything_v2_{name}.pth').touch()
        settings = {'guidance_mode': 2}
        self.assertEqual(mod_paths.depth_encoder(settings), 'vits')
        self.assertEqual(mod_paths.depth_encoder({**settings,
            'guidance_depth_weights': 'depth_anything_v2_vitl.pth'}), 'vitl')
        self.assertEqual(mod_paths.depth_encoder({**settings,
            'guidance_depth_weights': 'depth_anything_v2_{encoder}.pth'}), 'vits')
        (root / 'renamed.pth').touch()
        self.assertEqual(mod_paths.depth_encoder({**settings, 'guidance_depth_weights': 'renamed.pth'}), 'vits')

    def test_automatic_depth_selection_does_not_change_saved_settings(self):
        root = mod_paths.mods_root()
        component = self.component(root)
        (component / 'models').mkdir()
        (component / 'models/depth_anything_v2_vits.pth').touch()
        settings = app_settings.validate({'guidance_mode': 2, 'guidance_depth_encoder': 'auto'})
        before = dict(settings)
        guidance_client.validate(settings)
        self.assertEqual(settings, before)

    def test_optional_settings_roundtrip_and_range(self):
        settings = app_settings.validate({'guidance_mode': 3, 'guidance_edge': 99999,
            'guidance_python': 'mods/python.exe', 'dlss_runtime': 'custom.dll',
            'guidance_device': 'cpu', 'guidance_flow_direction': 'forward_negated'})
        self.assertEqual(settings['guidance_edge'], 1280)
        self.assertEqual(settings['guidance_mode'], 3)
        self.assertEqual(settings['guidance_device'], 'cpu')
        self.assertEqual(app_settings.validate({})['guidance_mode'], 1)
        for extra in ({'frame_format': 'rgba16f'}, {'host_tiled_mode': True}):
            with self.assertRaises(ValueError):
                guidance_client.validate({'guidance_mode': 1, **extra})

    def test_custom_module_and_model_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flow = root / 'flow.pth'
            depth = root / 'custom-vitb.pth'
            component = self.component(root)
            for file in (flow, depth):
                file.touch()
            settings = app_settings.validate({'guidance_mode': 3, 'mods_directory': str(root),
                'guidance_flow_weights': flow.name,
                'guidance_depth_weights': 'custom-{encoder}.pth', 'guidance_depth_encoder': 'vitb'})
            files = guidance_client.validate(settings)
            self.assertEqual(files['flow_weights'], str(flow))
            self.assertEqual(files['depth_weights'], str(depth))
            self.assertEqual(files['worker'], str(component / 'guidance_worker.exe'))
            self.assertNotIn('depth_code', files)
            self.assertNotIn('python', files)
            with mock.patch.object(mod_paths.sys, 'frozen', True, create=True), mock.patch.object(mod_paths.sys, '_MEIPASS', str(root / '_internal'), create=True):
                self.assertEqual(mod_paths.runtime_path(), str(root / '_internal/nvngx_dlssnr.dll'))

    def test_auto_runtime_unique_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(mod_paths, 'app_root', return_value=Path(directory)):
            root = Path(directory)
            (root / 'mods/dlss/rtx30').mkdir(parents=True)
            (root / 'mods/dlss/rtx30/nvngx_dlssnr.dll').touch()
            self.assertEqual(mod_paths.runtime_info()['source'], 'detected')
            (root / 'mods/dlss/rtx50').mkdir()
            (root / 'mods/dlss/rtx50/nvngx_dlssnr.dll').touch()
            info = mod_paths.runtime_info()
            self.assertTrue(info['ambiguous'])
            self.assertEqual(info['source'], 'bundled')

    def test_missing_custom_paths_find_reference_layout_without_other_encoder(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(mod_paths, 'app_root', return_value=Path(directory)):
            root = Path(directory) / 'mods'
            self.component(root)
            for relative in ('torch_home/hub/checkpoints/raft_large_C_T_SKHT_V2-ff5fadd5.pth',
                             'models/checkpoints/depth_anything_v2_vitl.pth'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            settings = {'guidance_mode': 3, 'mods_directory': str(root / 'missing'),
                        'guidance_python': 'missing.exe', 'guidance_flow_weights': 'missing.pth',
                        'guidance_depth_code': 'missing', 'guidance_depth_weights': 'missing.pth'}
            found = guidance_client.validate(settings)
            self.assertEqual(found['worker'], str(root / 'enhancement/guidance_worker.exe'))
            self.assertIn('torch_home', found['flow_weights'])
            self.assertIn('checkpoints', found['depth_weights'])
            self.assertNotIn('depth_code', found)
            with self.assertRaises(FileNotFoundError):
                guidance_client.validate({**settings, 'guidance_depth_encoder': 'vitb'})


@depth_test_case
class EngineGuidanceTests(unittest.TestCase):
    def make_live(self, mode):
        live = dlss_engine.Live.__new__(dlss_engine.Live)
        live.settings = {'guidance_mode': mode}
        live._w, live._h = 8, 6
        live._guidance = None
        live._reset_next = True
        live._lib = mock.Mock()
        live._lib.dlssnr_process.return_value = 1
        live._lib.dlssnr_enqueue.return_value = 1
        live._lib.dlssnr_pending.return_value = 0
        live.supports_async, live.max_in_flight = True, 2
        live._allocate_buffers()
        return live

    def test_off_never_starts_worker_and_preserves_fast_path(self):
        live = self.make_live(0)
        with mock.patch.object(guidance_client, 'GuidanceSession', side_effect=AssertionError('off')):
            live.process(np.zeros((6, 8, 4), np.uint8))
        self.assertTrue(dlss_engine._host_config({})[0])
        self.assertFalse(dlss_engine._host_config({'guidance_mode': 1, 'host_zero_fast_path': True})[0])

    def test_motion_depth_pointers_reach_native_call(self):
        live = self.make_live(3)
        worker = mock.Mock()
        mv = np.full((6, 8, 2), 2.5, np.float32)
        dp = np.full((6, 8), 0.75, np.float32)
        worker.process.return_value = (mv, dp, True)
        with mock.patch.object(guidance_client, 'GuidanceSession', return_value=worker):
            live.process(np.zeros((6, 8, 4), np.uint8))
        args = live._lib.dlssnr_process.call_args.args
        self.assertEqual(args[1].value, mv.ctypes.data)
        self.assertEqual(args[2].value, dp.ctypes.data)
        self.assertEqual(args[4], 1)
        self.assertEqual(live._lib.dlssnr_set_options.call_args.args[8], 3)
        self.assertFalse(worker.process.call_args.kwargs['copy_outputs'])

    def test_closing_guidance_discards_borrowed_views_and_restores_zero_inputs(self):
        live = self.make_live(3)
        live._guidance = worker = mock.Mock()
        motion = np.ones((6, 8, 2), np.float32)
        depth = np.ones((6, 8), np.float32)
        live._mv, live._dp = motion, depth
        def check_detached():
            self.assertIsNot(live._mv, motion)
            self.assertIsNot(live._dp, depth)
            np.testing.assert_array_equal(live._mv, 0)
            np.testing.assert_array_equal(live._dp, 0)
        worker.close.side_effect = check_detached
        live.close_guidance()
        self.assertIsNone(live._guidance)

    def test_compact_depth_is_null_only_for_capable_native_hosts(self):
        for capable in (False, True):
            live = self.make_live(1)
            live.supports_optional_depth = capable
            live._allocate_buffers()
            if capable:
                self.assertIsNone(live._dp)
            worker = mock.Mock()
            motion = np.ones((6, 8, 2), np.float32)
            worker.process.return_value = (motion, None if capable else np.zeros((6, 8), np.float32), True)
            with mock.patch.object(guidance_client, 'GuidanceSession', return_value=worker):
                for process in (live.process, live.enqueue):
                    process(np.ones((6, 8, 4), np.uint8))
            self.assertEqual(worker.process.call_args.kwargs['allow_missing_depth'], capable)
            for native_call in (live._lib.dlssnr_process, live._lib.dlssnr_enqueue):
                self.assertEqual(native_call.call_args.args[2] is None, capable)
                self.assertEqual(native_call.call_args.args[1].value, motion.ctypes.data)
            live.close_guidance()
            if capable:
                self.assertIsNone(live._dp)

    def test_full_async_queue_does_not_advance_model(self):
        live = self.make_live(1)
        live._lib.dlssnr_pending.return_value = 2
        with mock.patch.object(guidance_client, 'GuidanceSession', side_effect=AssertionError('full queue')):
            self.assertFalse(live.enqueue(np.zeros((6, 8, 4), np.uint8)))

    def test_model_error_closes_worker_and_requires_reset(self):
        live = self.make_live(1)
        live._guidance = worker = mock.Mock()
        worker.process.side_effect = RuntimeError('model error')
        with self.assertRaisesRegex(RuntimeError, 'model error'):
            live.process(np.zeros((6, 8, 4), np.uint8))
        worker.close.assert_called_once()
        self.assertIsNone(live._guidance)
        self.assertTrue(live._reset_next)


if __name__ == '__main__':
    unittest.main()
