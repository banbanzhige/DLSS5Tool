import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dlss5tool import app_settings, guidance_client, mod_paths
from dlss5tool.guidance_public import normalize_public_settings, public_mode, public_targets
from dlss5tool.export_queue import ExportJob
from dlss5tool.guidance_export import export_guidance


class PublicPolicyTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {'DLSS5TOOL_ENABLE_DEPTH': ''})
        patch.start()
        self.addCleanup(patch.stop)

    def test_mapping_dynamic_opt_in_and_invalid(self):
        self.assertEqual([public_mode(i) for i in range(4)], [0, 1, 0, 1])
        with mock.patch.dict(os.environ, {'DLSS5TOOL_ENABLE_DEPTH': '1'}):
            self.assertEqual([public_mode(i) for i in range(4)], [0, 1, 2, 3])
        self.assertEqual(public_mode(3), 1)
        for value in (-1, 4, 'bad'):
            with self.assertRaises(ValueError):
                public_mode(value)

    def test_settings_and_queue_snapshot_unchanged(self):
        original = {'guidance_mode': 3, 'guidance_preview_view': 'depth',
                    'guidance_compare_target': 'depth', 'guidance_depth_edge': 896}
        effective = app_settings.validate(original)
        self.assertEqual(effective['guidance_mode'], 1)
        self.assertEqual(effective['guidance_preview_view'], 'flow')
        self.assertEqual(effective['guidance_compare_target'], 'flow')
        self.assertEqual(effective['guidance_depth_edge'], 896)
        job = ExportJob.from_dict(dict(source_path='fixture.png', output_path='out.png', settings=original))
        self.assertEqual(normalize_public_settings(job.settings)['guidance_mode'], 1)
        self.assertEqual(job.settings, original)
        self.assertEqual(app_settings.startup_settings(original)['guidance_mode'], 1)

    def test_depth_only_needs_no_component_and_no_preflight(self):
        with mock.patch.object(mod_paths, 'enhancement_info') as files, \
                mock.patch.object(guidance_client, 'GuidanceSession') as session:
            self.assertEqual(guidance_client.validate({'guidance_mode': 2}), {})
            self.assertEqual(guidance_client.preflight({'guidance_mode': 2}), {})
        files.assert_not_called()
        session.assert_not_called()

    def test_actual_preflight_and_contract_use_public_mode(self):
        request = {'guidance_mode': 3}
        with mock.patch.object(guidance_client, 'validate'), \
                mock.patch.object(guidance_client, 'GuidanceSession') as factory:
            factory.return_value.info = {}
            guidance_client.preflight(request)
            self.assertEqual(factory.call_args.args[0]['guidance_mode'], 1)
        self.assertEqual(request['guidance_mode'], 3)
        self.assertEqual(guidance_client.contract(request), guidance_client.contract({'guidance_mode': 1}))

    def test_candidates_only_flow_and_direct_depth_export_rejected(self):
        candidates = mod_paths.guidance_candidates({'guidance_mode': 3})
        self.assertIn('flow_weights', candidates)
        self.assertNotIn('depth_weights', candidates)
        self.assertEqual(public_targets(), ('flow',))
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            export_guidance('missing.png', 'out.png', {'guidance_mode': 3}, 'depth')

    def test_public_gui_hides_depth_and_preserves_saved_parameters(self):
        import tkinter as tk
        from dlss5tool import gui
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
                'DLSS5TOOL_SETTINGS_PATH': str(Path(directory) / 'settings.json'),
                'DLSS5TOOL_QUEUE_PATH': str(Path(directory) / 'queue.json')}):
            app_settings.save({'guidance_mode': 3, 'guidance_depth_edge': 896})
            root = tk.Tk()
            root.withdraw()
            try:
                with mock.patch.object(guidance_client, 'preflight', return_value={'device': 'cuda'}):
                    app = gui.App(root)
                    import time
                    deadline = time.monotonic() + 5
                    while app._module_reload_thread is not None and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                self.assertIsNone(app._module_reload_thread)
                self.assertEqual(list(app._host_settings['w_guidance'].cget('values')),
                                 [gui.tr('guidance.mode.0'), gui.tr('guidance.mode.1')])
                self.assertEqual(app._guidance_depth_advanced.winfo_manager(), '')
                self.assertEqual(app._host_settings['guidance_controls']['profile'].master.winfo_manager(), '')
                self.assertNotIn('depth', app._host_settings['module_summaries'])
                self.assertNotIn('guidance_depth_weights', app._host_settings['path_entries'])
                self.assertEqual(app._collect_settings()['guidance_depth_edge'], 896)
                self.assertEqual(list(app._guidance_export_widgets['target'].cget('values')), [gui.tr('view.flow')])
                app.workspace_tabs.select(app._guidance_page)
                self.assertNotIn('depth', [value for value, _ in app.view_bar._group._choices])
                app._host_settings['v_flow_backend'].set(gui.tr('guidance.option.nvofa'))
                app._host_settings['v_guidance'].set(gui.tr('guidance.mode.1'))
                app._update_host_control_states()
                self.assertTrue(app._host_settings['guidance_controls']['guidance_flow_updates'].instate(['disabled']))
                self.assertFalse(bool(app._host_settings['guidance_controls']['guidance_flow_updates'].master.grid_info()))
                self.assertTrue(bool(app._host_settings['guidance_controls']['flow_grid'].master.grid_info()))
                self.assertIn('NVOFA', app._host_settings['module_summaries']['flow'].cget('text'))
                import time
                with mock.patch.object(guidance_client, 'preflight', return_value={
                        'device': 'cuda', 'flow_backend': 'raft', 'flow_fallback_reason': 'fixture driver unavailable'}):
                    app._on_mod_settings_change()
                    deadline = time.monotonic() + 5
                    while app._module_reload_thread is not None and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.005)
                self.assertIsNone(app._module_reload_thread)
                self.assertEqual(app._collect_host_settings()['guidance_flow_backend'], 'raft')
                self.assertEqual(app._host_settings['w_guidance_status'].cget('text'),
                                 gui.tr('guidance.status.checked'))
                self.assertIn('fixture driver unavailable',
                              app._host_settings['guidance_status_tooltip'].text)
                for handle in root.tk.splitlist(root.tk.call('after', 'info')):
                    root.after_cancel(handle)
                app._on_close()
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass
