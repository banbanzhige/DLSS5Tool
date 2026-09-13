"""Fast-path defaults and checked mode transitions, without GPU/model work."""
import unittest
from unittest import mock

from dlss5tool import app_settings, dlss_engine, guidance_client, i18n
from tests import test_gui_module_reload as reload_tests


class ZeroGuidanceDefaultTests(unittest.TestCase):
    def test_default_depends_on_effective_mode_and_preserves_manual_override(self):
        self.assertTrue(app_settings.validate({'guidance_mode': 0})['host_zero_fast_path'])
        self.assertFalse(app_settings.validate({'guidance_mode': 1})['host_zero_fast_path'])
        self.assertFalse(app_settings.validate({})['host_zero_fast_path'])
        self.assertFalse(app_settings.validate({
            'guidance_mode': 0, 'host_zero_fast_path': False})['host_zero_fast_path'])

    def test_real_flow_never_uses_zero_motion_even_with_explicit_fast_flag(self):
        for mode in (1, 3):
            self.assertFalse(dlss_engine._host_config({
                'guidance_mode': mode, 'host_zero_fast_path': True})[0])


class ZeroGuidanceTransitionTests(unittest.TestCase):
    setUp = reload_tests.ModuleReloadTests.setUp
    wait_reload = reload_tests.ModuleReloadTests.wait_reload

    def configure(self, previous_mode, selected_mode, fast=False):
        def variable(initial):
            value = mock.Mock()
            value.get.return_value = initial
            value.set.side_effect = lambda new: setattr(value.get, 'return_value', new)
            return value
        mode = variable(i18n.tr('guidance.mode.' + str(selected_mode)))
        flag = variable(fast)
        self.app._host_settings = {'v_guidance': mode, 'v_zero_fast': flag}
        def collect():
            selected = int(mode.get() == i18n.tr('guidance.mode.1'))
            return {'guidance_mode': selected,
                    'host_zero_fast_path': bool(flag.get()) and not selected,
                    'host_submission': 'compatibility', 'host_in_flight': 3}
        self.app._collect_host_settings = collect
        self.app._last_module_settings = {**collect(), 'guidance_mode': previous_mode}
        return mode, flag

    def test_disabling_enables_fast_before_reload_and_persistence(self):
        _, flag = self.configure(1, 0)
        with mock.patch.object(guidance_client, 'preflight') as check:
            self.app._on_mod_settings_change()
            self.assertTrue(flag.get())
            self.wait_reload()
        check.assert_not_called()
        self.assertTrue(self.app._last_module_settings['host_zero_fast_path'])
        self.assertEqual(self.app._last_module_settings['host_submission'], 'compatibility')
        self.assertEqual(self.app._last_module_settings['host_in_flight'], 3)
        self.app._schedule_settings_save.assert_called()

    def test_enabling_clears_visible_and_effective_flag_then_disabling_restores(self):
        mode, flag = self.configure(0, 1, fast=True)
        with mock.patch.object(guidance_client, 'preflight', return_value={'device': 'cuda'}):
            self.app._on_mod_settings_change()
            self.wait_reload()
        self.assertFalse(flag.get())
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 1)
        self.assertFalse(self.app._collect_host_settings()['host_zero_fast_path'])
        mode.set(i18n.tr('guidance.mode.0'))
        self.app._on_mod_settings_change()
        self.wait_reload()
        self.assertTrue(flag.get())

    def test_failed_activation_returns_to_fast_base_rendering(self):
        _, flag = self.configure(0, 1)
        with mock.patch.object(guidance_client, 'preflight', side_effect=RuntimeError('fixture')):
            self.app._on_mod_settings_change()
            self.wait_reload()
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 0)
        self.assertTrue(flag.get())

    def test_unrelated_edit_and_busy_callback_do_not_override_manual_opt_out(self):
        _, flag = self.configure(0, 0)
        self.app._last_module_settings['host_in_flight'] = 2
        self.app._on_mod_settings_change()
        self.wait_reload()
        self.assertFalse(flag.get())
        self.configure(1, 0)
        self.app._exporting = True
        self.app._on_mod_settings_change()
        self.app._host_settings['v_zero_fast'].set.assert_not_called()


if __name__ == '__main__':
    unittest.main()
