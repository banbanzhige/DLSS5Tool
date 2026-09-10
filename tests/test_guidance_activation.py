"""Default tuning and opt-in readiness checks, without downloading models."""
import threading
import unittest
from tests.depth_fixture import depth_test_case
from unittest import mock

import numpy as np
from dlss5tool import app_settings
from dlss5tool import guidance_client
from dlss5tool import i18n
from tests import test_gui_module_reload as reload_tests


@depth_test_case
class DefaultTuningTests(unittest.TestCase):
    def test_product_defaults_and_lightweight_startup(self):
        values = app_settings.validate({})
        expected = dict(guidance_mode=0, guidance_flow_edge=512, guidance_depth_edge=512,
                        guidance_flow_updates=6, guidance_flow_backend='raft', guidance_flow_grid=4,
                        guidance_depth_encoder='vitl',
                        guidance_depth_profile='sdpa_fp16', guidance_execution='raft_streams',
                        guidance_flow_range=3.0, local_tone=1.0, local_struct=1.0,
                        nvenc_preset='p5', quality_profile='high', host_submission='compatibility')
        for key, value in expected.items():
            self.assertEqual(values[key], value, key)
        self.assertTrue(values['host_zero_fast_path'])
        saved = {**values, 'guidance_mode': 3, 'guidance_flow_edge': 960,
                 'guidance_depth_profile': 'fp32', 'guidance_execution': 'serial'}
        startup = app_settings.startup_settings(saved)
        self.assertEqual(startup, {**saved, 'guidance_mode': 0})
        self.assertEqual(app_settings.validate(saved), saved)  # queues retain opt-in


@depth_test_case
class PreflightTests(unittest.TestCase):
    def test_off_does_not_start_component(self):
        with mock.patch.object(guidance_client, 'GuidanceSession') as factory:
            self.assertEqual(guidance_client.preflight({'guidance_mode': 0}), {})
        factory.assert_not_called()

    def test_missing_files_rejected_before_start(self):
        with mock.patch.object(guidance_client, 'validate', side_effect=FileNotFoundError('weights')), \
                mock.patch.object(guidance_client, 'GuidanceSession') as factory:
            with self.assertRaises(FileNotFoundError):
                guidance_client.preflight({'guidance_mode': 1})
        factory.assert_not_called()

    def test_each_mode_exercises_two_frames_and_closes(self):
        for mode in (1, 2, 3):
            with self.subTest(mode=mode), mock.patch.object(guidance_client, 'validate'), \
                    mock.patch.object(guidance_client, 'GuidanceSession') as factory:
                session = factory.return_value
                session.info = {'device': 'cuda'}
                self.assertEqual(guidance_client.preflight({'guidance_mode': mode}), session.info)
                self.assertEqual(factory.call_args.args, ({'guidance_mode': mode, 'guidance_cache_mb': 0}, 128, 128))
                first, second = session.process.call_args_list
                self.assertTrue(first.kwargs['reset'])
                self.assertFalse(second.kwargs['reset'])
                self.assertEqual(first.args[0].dtype, np.uint8)
                self.assertFalse(np.array_equal(first.args[0], second.args[0]))
                session.close.assert_called_once()

    def test_kernel_failure_closes_component(self):
        with mock.patch.object(guidance_client, 'validate'), \
                mock.patch.object(guidance_client, 'GuidanceSession') as factory:
            factory.return_value.process.side_effect = RuntimeError('CUDA kernel')
            with self.assertRaisesRegex(RuntimeError, 'CUDA kernel'):
                guidance_client.preflight({'guidance_mode': 3})
            factory.return_value.close.assert_called_once()

    def test_gui_requires_shared_budget_capability(self):
        with mock.patch.object(guidance_client, 'validate'), \
                mock.patch.object(guidance_client, 'GuidanceSession') as factory:
            factory.return_value.info = {'cache_version': 'raw_lru_v1'}
            with self.assertRaises(RuntimeError):
                guidance_client.preflight({'guidance_mode': 1}, require_shared_cache=True)
            factory.return_value.process.assert_not_called()
            factory.return_value.close.assert_called_once()


@depth_test_case
class ActivationTests(unittest.TestCase):
    setUp = reload_tests.ModuleReloadTests.setUp
    wait_reload = reload_tests.ModuleReloadTests.wait_reload

    def configure_mode(self, mode):
        variable = mock.Mock()
        variable.get.return_value = i18n.tr('guidance.mode.' + str(mode))
        variable.set.side_effect = lambda value: setattr(variable.get, 'return_value', value)
        self.app._host_settings = {'v_guidance': variable}
        self.app._collect_host_settings = lambda: {
            'guidance_mode': next(i for i in range(4) if variable.get() == i18n.tr('guidance.mode.' + str(i)))
        }
        self.app._last_module_settings = {'guidance_mode': 0}
        return variable

    def test_activation_not_committed_while_checking(self):
        selected = self.configure_mode(3)
        entered, release = threading.Event(), threading.Event()
        def probe(settings, **kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            self.assertTrue(kwargs['require_shared_cache'])
            self.assertEqual(settings['guidance_mode'], 3)
            entered.set()
            release.wait(3)
            return {'device': 'cuda'}
        with mock.patch.object(guidance_client, 'preflight', side_effect=probe) as check:
            try:
                self.app._on_mod_settings_change()
                self.assertTrue(entered.wait(1))
                self.assertEqual(selected.get(), i18n.tr('guidance.mode.0'))
                self.assertTrue(self.app._switching_backend)
                self.app._on_mod_settings_change()
                check.assert_called_once()
                self.app.root.tick()
            finally:
                release.set()
                self.wait_reload()
        self.assertEqual(selected.get(), i18n.tr('guidance.mode.3'))
        self.assertEqual(self.app._guidance_preflight_info, {'device': 'cuda'})

    def test_failure_stays_off_and_allows_retry_without_media(self):
        self.app.video = None
        selected = self.configure_mode(2)
        with mock.patch.object(guidance_client, 'preflight', side_effect=RuntimeError('missing runtime')):
            self.app._on_mod_settings_change()
            self.wait_reload()
        self.assertEqual(selected.get(), i18n.tr('guidance.mode.0'))
        self.assertIn('missing runtime', self.app._guidance_preflight_error)
        self.assertFalse(self.app._switching_backend)
        selected.set(i18n.tr('guidance.mode.1'))
        with mock.patch.object(guidance_client, 'preflight', return_value={'device': 'cuda'}):
            self.app._on_mod_settings_change()
            self.wait_reload()
        self.assertEqual(selected.get(), i18n.tr('guidance.mode.1'))
        self.assertEqual(self.app._guidance_preflight_error, '')

    def test_disable_needs_no_environment(self):
        self.configure_mode(0)
        self.app._last_module_settings = {'guidance_mode': 3}
        with mock.patch.object(guidance_client, 'preflight') as check:
            self.app._on_mod_settings_change()
            self.wait_reload()
        check.assert_not_called()
