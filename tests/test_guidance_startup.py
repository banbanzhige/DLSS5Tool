"""Remembered GUI modes are restored only after a non-blocking readiness check."""
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from dlss5tool import app_settings, gui


class GuidanceStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            'DLSS5TOOL_SETTINGS_PATH': str(directory / 'settings.json'),
            'DLSS5TOOL_QUEUE_PATH': str(directory / 'queue.json'),
            'DLSS5TOOL_ENABLE_DEPTH': '',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.wait_for_check()
        for handle in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(handle)
        if self.app is not None:
            self.app._on_close()
        else:
            self.root.destroy()
        self.assertEqual(self.errors, [])

    def wait_for_check(self):
        deadline = time.monotonic() + 5
        while self.app._module_reload_thread is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertIsNone(self.app._module_reload_thread)

    def save_mode(self, mode=1, backend='raft'):
        app_settings.save({'guidance_mode': mode, 'guidance_flow_backend': backend,
                           'guidance_flow_edge': 768, 'guidance_flow_updates': 12,
                           'guidance_flow_grid': 2})

    def test_saved_mode_checks_in_background_and_survives_autosave(self):
        self.save_mode()
        entered, release = threading.Event(), threading.Event()

        def preflight(settings, **kwargs):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            self.assertEqual(settings['guidance_mode'], 1)
            self.assertEqual(settings['guidance_flow_edge'], 768)
            self.assertTrue(kwargs['require_shared_cache'])
            entered.set()
            release.wait(5)
            return {'device': 'cuda', 'flow_backend': 'raft'}

        with mock.patch.object(gui.guidance_client, 'preflight', side_effect=preflight) as check:
            try:
                self.app = gui.App(self.root)
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.app._switching_backend)
                self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 0)
                self.assertEqual(self.app._host_settings['w_guidance_status'].cget('text'),
                                 gui.tr('guidance.status.loading'))
                self.app._save_settings_now()
                self.assertEqual(app_settings.load()['guidance_mode'], 1)
                self.root.update()
            finally:
                release.set()
                if self.app is not None:
                    self.wait_for_check()
            check.assert_called_once()
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 1)
        self.app._save_settings_now()
        saved = app_settings.load()
        self.assertEqual(saved['guidance_mode'], 1)
        self.assertEqual(saved['guidance_flow_updates'], 12)

    def test_nvofa_mode_and_grid_are_restored(self):
        self.save_mode(backend='nvofa')
        with mock.patch.object(gui.guidance_client, 'preflight', return_value={
                'device': 'cuda', 'flow_backend': 'nvofa'}) as check:
            self.app = gui.App(self.root)
            self.wait_for_check()
        self.assertEqual(check.call_args.args[0]['guidance_flow_backend'], 'nvofa')
        host = self.app._collect_host_settings()
        self.assertEqual(host['guidance_mode'], 1)
        self.assertEqual(host['guidance_flow_backend'], 'nvofa')
        self.assertEqual(host['guidance_flow_grid'], 2)

    def test_missing_environment_disables_mode_but_preserves_tuning(self):
        self.save_mode()
        with mock.patch.object(gui.guidance_client, 'preflight',
                               side_effect=FileNotFoundError('fixture missing component')):
            self.app = gui.App(self.root)
            self.wait_for_check()
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 0)
        self.assertFalse(self.app._switching_backend)
        self.assertIn('fixture missing component', self.app._guidance_preflight_error)
        self.app._save_settings_now()
        saved = app_settings.load()
        self.assertEqual(saved['guidance_mode'], 0)
        self.assertEqual(saved['guidance_flow_edge'], 768)
        self.assertEqual(saved['guidance_flow_updates'], 12)

    def test_first_run_checks_current_default_flow(self):
        with mock.patch.object(gui.guidance_client, 'preflight', return_value={
                'device': 'cuda', 'flow_backend': 'raft'}) as check:
            self.app = gui.App(self.root)
            self.wait_for_check()
        check.assert_called_once()
        settings = check.call_args.args[0]
        self.assertEqual(settings['guidance_mode'], 1)
        self.assertEqual(settings['guidance_flow_backend'], 'raft')
        self.assertEqual(settings['guidance_flow_edge'], 512)
        self.assertEqual(settings['guidance_flow_updates'], 6)
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 1)

    def test_saved_off_does_not_probe_or_enable_installed_models(self):
        self.save_mode(0)
        with mock.patch.object(gui.guidance_client, 'preflight') as check:
            self.app = gui.App(self.root)
            self.root.update()
        check.assert_not_called()
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 0)
        self.assertFalse(self.app._switching_backend)
