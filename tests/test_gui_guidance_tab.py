"""Guidance navigation and settings contracts, without media or GPU inference."""
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

import app_settings
import gui
import numpy as np
import ui_theme


class GuidanceTabTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {
            'DLSS5TOOL_SETTINGS_PATH': str(directory / 'settings.json'),
            'DLSS5TOOL_QUEUE_PATH': str(directory / 'queue.json'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        app_settings.save({
            'guidance_mode': 3, 'guidance_device': 'cuda',
            'guidance_depth_encoder': 'vitb', 'guidance_edge': 512,
            'guidance_flow_direction': 'forward_negated',
            'guidance_depth_profile': 'sdpa_fp16', 'guidance_execution': 'raft_streams',
        })
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        self.app = gui.App(self.root)
        self.assertEqual(self.app._collect_host_settings()['guidance_mode'], 0)
        preflight = mock.patch.object(gui.guidance_client, 'preflight', return_value={'device': 'cuda'})
        preflight.start()
        self.addCleanup(preflight.stop)
        # Rendering/navigation fixtures explicitly opt in; startup no longer
        # activates saved modes. GPU readiness is tested separately.
        self.app._host_settings['v_guidance'].set(gui.tr('guidance.mode.3'))
        self.app._on_mod_settings_change()
        self.wait_for_reload()
        self.root.update_idletasks()

    def tearDown(self):
        self.wait_for_reload()
        for handle in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(handle)
        self.assertEqual(self.errors, [])

    def wait_for_reload(self):
        deadline = time.monotonic() + 3
        while self.app._module_reload_thread is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.005)
        self.assertIsNone(self.app._module_reload_thread)

    def test_navigation_uses_one_guidance_page_and_does_not_change_settings(self):
        app = self.app
        self.assertEqual([app.workspace_tabs.tab(i, 'text') for i in range(4)],
                         [gui.tr('tab.' + name) for name in ('adjust', 'guidance', 'export', 'queue')])
        before = app._collect_settings()
        app.workspace_tabs.select(app._guidance_page)
        self.assertNotIn('w_guidance_settings', app._settings)
        self.assertNotIn('w_guidance_summary', app._settings)
        self.root.update_idletasks()
        self.assertIs(app.workspace_tabs._current, app._guidance_page)
        self.assertTrue(app._guidance_advanced.collapsed)
        self.assertIs(app._modules_section.master, app._export_inner)
        self.assertIs(app._settings['w_guidance'], app._host_settings['w_guidance'])
        for page in (app._export_page, app.queue_tab, app._preview_page, app._guidance_page):
            app.workspace_tabs.select(page)
        self.assertEqual(before, app._collect_settings())

    def test_failed_activation_is_inline_and_persists_off_in_both_themes(self):
        app = self.app
        for theme in ('light', 'dark'):
            with self.subTest(theme=theme):
                app._apply_ui_theme(theme, persist=False)
                app._host_settings['v_guidance'].set(gui.tr('guidance.mode.2'))
                with mock.patch.object(gui.guidance_client, 'preflight', side_effect=RuntimeError('fixture: missing weights')):
                    app._on_mod_settings_change()
                    self.wait_for_reload()
                status = app._host_settings['w_guidance_status'].cget('text')
                self.assertIn('fixture: missing weights', status)
                self.assertEqual(app._collect_persisted_settings()['guidance_mode'], 0)
                self.assertEqual(app._collect_host_settings()['guidance_depth_encoder'], 'vitb')
                self.assertEqual(str(app._host_settings['w_guidance'].cget('state')), 'readonly')
                self.assertFalse(app.import_btn.instate(['disabled']))

    def test_failed_gpu_activation_can_be_reconfigured_for_cpu_while_off(self):
        app = self.app
        app._host_settings['v_guidance'].set(gui.tr('guidance.mode.2'))
        with mock.patch.object(gui.guidance_client, 'preflight', side_effect=RuntimeError('CUDA unavailable')):
            app._on_mod_settings_change()
            self.wait_for_reload()
        for key, code in (('v_depth_profile', 'fp32'), ('v_guidance_device', 'cpu')):
            control = 'profile' if key == 'v_depth_profile' else 'device'
            self.assertFalse(app._host_settings['guidance_controls'][control].instate(['disabled']))
            app._host_settings[key].set(gui.tr('guidance.option.' + code))
            with mock.patch.object(gui.guidance_client, 'preflight') as check:
                app._on_mod_settings_change()
                self.wait_for_reload()
                check.assert_not_called()
            self.assertEqual(app._collect_host_settings()['guidance_mode'], 0)
        app._host_settings['v_guidance'].set(gui.tr('guidance.mode.2'))
        with mock.patch.object(gui.guidance_client, 'preflight', return_value={'device': 'cpu'}):
            app._on_mod_settings_change()
            self.wait_for_reload()
        self.assertEqual(app._collect_host_settings()['guidance_mode'], 2)
        self.assertEqual(app._guidance_edit_mode, 0)

    def test_context_switches_view_choices_and_preserves_each_selection(self):
        app = self.app
        app.view_var.set('dlss')
        app.preview_selector.set('dlss')
        app.workspace_tabs.select(app._guidance_page)
        self.assertEqual([value for value, _label in app.view_bar._group._choices],
                         ['original', 'depth', 'flow', 'compare'])
        app.preview_selector.set('flow')
        app._on_preview_selection()
        app.workspace_tabs.select(app._export_page)
        self.assertEqual(app.view_var.get(), 'dlss')
        self.assertEqual(app.preview_selector.get(), 'dlss')
        self.assertEqual([value for value, _label in app.view_bar._group._choices],
                         ['original', 'dlss', 'compare'])
        app.workspace_tabs.select(app._guidance_page)
        self.assertEqual(app.preview_selector.get(), 'flow')

    def test_compare_layout_is_shared_and_side_by_side_draws_two_labeled_views(self):
        app = self.app
        app.video = 'fixture.png'
        app._source_kind = 'image'
        app._video_color_info = {'is_hdr': False}
        app._media_w, app._media_h = 32, 16
        app._image_bgr = np.zeros((16, 32, 3), np.uint8)
        app._split_orig = np.zeros((16, 32, 3), np.uint8)
        app._split_dlss = np.full((16, 32, 3), 180, np.uint8)
        app.view_var.set('compare')
        app.preview_selector.set('compare')
        app.compare_layout.set('side')
        app._sync_comparison_controls()
        app.root.geometry('1000x700')
        app.root.update_idletasks()
        width, height = app._canvas_size()
        app._blit_split(width, height)
        self.assertEqual(len(app._compare_photos), 2)
        self.assertEqual(app.canvas.find_withtag('split'), ())
        app.compare_layout.set('wipe')
        app._blit_split(width, height)
        self.assertTrue(app.canvas.find_withtag('split'))
        app._center_split()
        self.assertEqual(app.split_x, 0.5)

    def test_unavailable_guidance_never_starts_inference(self):
        app = self.app
        app.video = 'fixture.png'
        app._source_kind = 'image'
        app._video_color_info = {'is_hdr': False}
        app._media_w, app._media_h = 32, 16
        app._image_bgr = np.zeros((16, 32, 3), np.uint8)
        app._host_settings['v_guidance'].set(gui.tr('guidance.mode.1'))
        app.workspace_tabs.select(app._guidance_page)
        app._guidance_view = 'depth'
        with mock.patch.object(app, '_request_guidance_preview') as request:
            app._display_guidance(0)
            request.assert_not_called()
        self.assertIn(gui.tr('view.depth'), app.canvas.itemcget(app.canvas.find_all()[-1], 'text'))

    def test_stale_guidance_frame_is_not_displayed_or_compared(self):
        app = self.app
        app.video = 'fixture.png'
        app._source_kind = 'image'
        app._video_color_info = {'is_hdr': False}
        app._media_w, app._media_h = 32, 16
        app._image_bgr = np.zeros((16, 32, 3), np.uint8)
        app.workspace_tabs.select(app._guidance_page)
        app._guidance_view = 'compare'
        app._frame = 2
        stale_key = (app.video, 1, app._guidance_generation,
                     app._guidance_preview_epoch, app._settings_hash())
        app._guidance_result = (stale_key, app._image_bgr, {'depth': app._image_bgr}, False, '')
        with mock.patch.object(app, '_request_guidance_preview') as request:
            app._display_guidance(2)
            request.assert_called_once_with(app._guidance_preview_key())
        self.assertIsNone(app._split_orig)
        self.assertIsNone(app._split_dlss)

    def test_modes_disable_only_inactive_controls_and_preserve_values(self):
        app = self.app
        before = {key: value for key, value in app._collect_host_settings().items()
                  if key.startswith('guidance_')}
        active_by_mode = {
            0: {'mode'},
            1: {'mode', 'device', 'flow', 'guidance_flow_edge', 'guidance_flow_updates',
                'guidance_flow_range'},
            2: {'mode', 'device', 'depth', 'profile', 'guidance_depth_edge',
                'guidance_depth_smoothing', 'guidance_depth_low', 'guidance_depth_high', 'palette', 'invert'},
            3: set(app._host_settings['guidance_controls']),
        }
        for mode, active in active_by_mode.items():
            with self.subTest(mode=mode):
                app._host_settings['v_guidance'].set(gui.tr('guidance.mode.' + str(mode)))
                app._on_mod_settings_change()
                self.wait_for_reload()
                self.root.update_idletasks()
                for name, widget in app._host_settings['guidance_controls'].items():
                    self.assertEqual(widget.instate(['disabled']), name not in active, name)
                collected = app._collect_host_settings()
                self.assertEqual({key: collected[key] for key in before}, {**before, 'guidance_mode': mode})
                self.assertNotIn('w_guidance_summary', app._settings)

    def _comparison_fixture(self, target='depth', layout='wipe'):
        app = self.app
        app.workspace_tabs.select(app._guidance_page)
        app.video = 'synthetic-video.mp4'
        app._source_kind = 'video'
        app._video_color_info = {'is_hdr': False}
        app.nframes, app.fps = 6, 24
        app._media_w, app._media_h = 64, 32
        app._guidance_view = 'compare'
        app.preview_selector.set('compare')
        app.compare_target.set(target)
        app.compare_layout.set(layout)
        app.timeline.set_range(0, 5)
        app._sync_comparison_controls()
        app._canvas_size = lambda: (640, 360)
        return app

    def _result(self, frame):
        source = np.full((32, 64, 3), 30 + frame, np.uint8)
        images = {'depth': np.full_like(source, 130 + frame), 'flow': np.full_like(source, 230 + frame)}
        return (self.app._guidance_preview_key(frame), source, images, False, '')

    def test_waiting_playback_keeps_canvas_and_frame_until_complete_pair_ready(self):
        for target in ('depth', 'flow'):
            for layout in ('wipe', 'side'):
                with self.subTest(target=target, layout=layout):
                    app = self._comparison_fixture(target, layout)
                    app._frame = 0
                    app._guidance_result = self._result(0)
                    app._guidance_ready = None
                    app._guidance_display_time = time.perf_counter() - 1
                    app._display_guidance(0)
                    original_canvas = app.canvas.find_all()
                    app.playing = True
                    with mock.patch.object(app, '_request_guidance_preview') as request:
                        app._guidance_play_tick()
                        self.assertEqual(app._frame, 0)
                        self.assertEqual(app.canvas.find_all(), original_canvas)
                        request.assert_called_once_with(app._guidance_preview_key(1))
                        next_result = self._result(1)
                        app._guidance_preview_busy = True
                        app._guidance_preview_queue.put(next_result)
                        app._poll_guidance_preview()
                        self.assertEqual(app._frame, 0)
                        self.assertEqual(app.canvas.find_all(), original_canvas)
                        app._guidance_play_tick()
                        self.assertEqual(app._frame, 1)
                        self.assertEqual(app.timeline.get(), 1)
                        self.assertIs(app._split_orig, next_result[1])
                        self.assertIs(app._split_dlss, next_result[2][target])
                    app.pause()
                    self.assertFalse(any('正在生成' in app.canvas.itemcget(item, 'text')
                                         for item in app.canvas.find_all() if app.canvas.type(item) == 'text'))

    def test_seek_holds_complete_pair_and_puts_waiting_status_outside_canvas(self):
        app = self._comparison_fixture()
        app._guidance_result = self._result(0)
        app._display_guidance(0)
        previous_canvas = app.canvas.find_all()
        source, depth = app._split_orig, app._split_dlss
        app._frame = 3
        with mock.patch.object(app, '_request_guidance_preview'):
            app._display_guidance(3)
        self.assertIs(app._split_orig, source)
        self.assertIs(app._split_dlss, depth)
        self.assertEqual(app.canvas.find_all(), previous_canvas)
        self.assertEqual(app.eta_label.cget('text'), gui.tr('guidance.preview_waiting', frame=3, shown=0))

    def test_paused_playback_does_not_accept_inflight_next_frame(self):
        app = self._comparison_fixture()
        app._guidance_result = self._result(0)
        app._display_guidance(0)
        next_result = self._result(1)
        app.pause()
        app._guidance_preview_queue.put(next_result)
        app._poll_guidance_preview()
        self.assertEqual(app._frame, 0)
        self.assertEqual(app._guidance_result[0][1], 0)
        self.assertIsNone(app._guidance_ready)

    def test_late_result_from_old_settings_never_replaces_display(self):
        app = self._comparison_fixture()
        retired = self._result(1)
        app._guidance_generation += 1
        app._guidance_result = self._result(0)
        app._display_guidance(0)
        current = app._guidance_result
        app.playing = True
        app._guidance_preview_queue.put(retired)
        with mock.patch.object(app, '_request_guidance_preview'):
            app._poll_guidance_preview()
        self.assertIs(app._guidance_result, current)
        self.assertIsNone(app._guidance_ready)
        app.pause()

    def test_comparison_menu_replaces_all_top_controls_and_legend(self):
        app = self.app
        before = app._preview_host.pack_slaves()
        app.workspace_tabs.select(app._guidance_page)
        app.preview_selector.set('compare')
        app._on_preview_selection()
        self.assertEqual(before, app._preview_host.pack_slaves())
        self.assertFalse(hasattr(app, 'compare_bar'))
        self.assertFalse(hasattr(app, 'compare_legend'))
        menu = app._build_comparison_menu(app.view_bar)
        self.addCleanup(menu.destroy)
        labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
        self.assertIn(gui.tr('compare.wipe'), labels)
        self.assertIn(gui.tr('compare.side'), labels)
        self.assertIn(gui.tr('guidance.legend.title'), labels)
        menu.invoke(1)  # flow comparison target
        self.assertEqual(app.compare_target.get(), 'flow')
        menu.invoke(4)  # side-by-side layout
        self.assertEqual(app.compare_layout.get(), 'side')
        with mock.patch('preview_comparison.messagebox.showinfo') as dialog:
            menu.invoke(menu.index('end'))
            self.assertEqual(dialog.call_args.args[1], gui.tr('guidance.legend.flow'))

    def test_dlss_menu_has_no_guidance_options_and_keeps_export_settings(self):
        app = self.app
        before = app._collect_settings()
        app.view_var.set('compare')
        app.preview_selector.set('compare')
        app._sync_comparison_controls()
        menu = app._build_comparison_menu(app.view_bar)
        self.addCleanup(menu.destroy)
        self.assertEqual(menu.index('end'), 2)
        menu.invoke(1)
        self.assertEqual(app.compare_layout.get(), 'side')
        self.assertEqual(app._collect_settings(), before)

    def test_guidance_export_footer_is_fixed_and_mode_aware(self):
        app = self.app
        self.assertIs(app._guidance_export_footer.master, app._guidance_page)
        self.assertEqual(int(app._guidance_export_footer.grid_info()['row']), 1)
        self.assertNotIn('w_guidance_settings', app._settings)
        app.video = 'fixture.png'
        app._source_kind = 'image'
        app._image_bgr = np.zeros((12, 16, 3), np.uint8)
        app._update_guidance_export_controls()
        widgets = app._guidance_export_widgets
        self.assertEqual(app._guidance_export_scope.get(), gui.tr('guidance.export.frame'))
        self.assertTrue(widgets['scope'].instate(['disabled']))
        self.assertFalse(widgets['button'].instate(['disabled']))
        app._host_settings['v_guidance'].set(gui.tr('guidance.mode.1'))
        app._update_guidance_export_controls()
        self.assertTrue(widgets['button'].instate(['disabled']))
        app._guidance_export_target.set(gui.tr('view.flow'))
        app._update_guidance_export_controls()
        self.assertFalse(widgets['button'].instate(['disabled']))
        app._guidance_export_active = app._exporting = True
        app._update_guidance_export_controls()
        self.assertEqual(widgets['button'].winfo_manager(), '')
        self.assertEqual(widgets['cancel'].winfo_manager(), 'grid')
        app._cancel_guidance_export()
        self.assertTrue(app._export_cancel_event.is_set())
        self.assertTrue(widgets['cancel'].instate(['disabled']))
        app._guidance_export_active = app._exporting = False

    def test_export_dialog_cancel_has_no_side_effects(self):
        app = self.app
        app.video = 'fixture.mp4'
        before = app._collect_settings()
        with mock.patch('guidance_export_ui.guidance_client.validate'), \
                mock.patch('guidance_export_ui.filedialog.asksaveasfilename', return_value=''), \
                mock.patch('guidance_export_ui.export_guidance') as exporter:
            app._start_guidance_export()
        exporter.assert_not_called()
        self.assertFalse(app._exporting)
        self.assertEqual(app._collect_settings(), before)

    def test_busy_states_lock_all_guidance_controls(self):
        app = self.app
        for flag in ('_exporting', '_queue_running', '_switching_backend', '_diagnosing'):
            with self.subTest(flag=flag):
                setattr(app, flag, True)
                app._update_host_control_states()
                self.assertTrue(all(control.instate(['disabled'])
                                    for control in app._host_settings['guidance_controls'].values()))
                setattr(app, flag, False)
                app._update_host_control_states()
                self.assertTrue(all(not control.instate(['disabled'])
                                    for control in app._host_settings['guidance_controls'].values()))

    def test_edits_reach_preview_export_queue_and_saved_settings(self):
        app = self.app
        app._host_settings['analysis_vars']['guidance_flow_edge'].set('640')
        app._host_settings['analysis_vars']['guidance_depth_edge'].set('896')
        app._host_settings['analysis_vars']['guidance_flow_updates'].set('12')
        app._host_settings['analysis_vars']['guidance_depth_smoothing'].set('0.5')
        app._host_settings['v_guidance_device'].set(gui.tr('guidance.option.cpu'))
        app._host_settings['v_depth_profile'].set(gui.tr('guidance.option.fp32'))
        app._host_settings['v_guidance_execution'].set(gui.tr('guidance.option.serial'))
        app._on_mod_settings_change()
        self.wait_for_reload()
        expected = {key: value for key, value in app._collect_host_settings().items()
                    if key.startswith('guidance_')}
        self.assertEqual(expected['guidance_flow_edge'], 640)
        self.assertEqual(expected['guidance_depth_edge'], 896)
        self.assertEqual(expected['guidance_flow_updates'], 12)
        self.assertEqual(expected['guidance_depth_smoothing'], 0.5)
        self.assertEqual(expected['guidance_device'], 'cpu')
        self.assertEqual(expected['guidance_depth_profile'], 'fp32')
        self.assertEqual(expected['guidance_execution'], 'serial')
        for collected in (app._collect_settings(), app._collect_persisted_settings()):
            self.assertEqual({key: collected[key] for key in expected}, expected)
        source = Path(self.temporary.name) / 'fixture.mp4'
        source.touch()
        with mock.patch.object(app, '_probe_queue_media', return_value=(
                {'frames': 48, 'fps': 24.0, 'width': 1920, 'height': 1080},
                {'is_hdr': False, 'label': 'SDR'})):
            self.assertEqual(app._add_paths_to_queue([str(source)], switch_tab=False), 1)
        job = app._queue_jobs[0]
        self.assertEqual({key: job.settings[key] for key in expected}, expected)
        app._host_settings['analysis_vars']['guidance_flow_edge'].set('720')
        self.assertEqual(job.settings['guidance_flow_edge'], 640)
        app_settings.save(app._collect_persisted_settings())
        self.assertEqual(app_settings.load()['guidance_flow_edge'], 720)

    def test_display_changes_do_not_reload_or_invalidate_enhancement(self):
        app = self.app
        before = app._settings_hash()
        generation = app._guidance_generation
        with mock.patch.object(app, '_close_live') as close, mock.patch.object(app, '_cache_clear') as clear:
            app._host_settings['analysis_vars']['guidance_flow_range'].set('8')
            app._host_settings['v_depth_palette'].set(gui.tr('guidance.option.turbo'))
            app._host_settings['v_depth_invert'].set(gui.tr('guidance.option.inverted'))
            app._on_mod_settings_change()
            close.assert_not_called()
            clear.assert_not_called()
        self.assertEqual(before, app._settings_hash())
        self.assertEqual(generation, app._guidance_generation)
        self.assertTrue(app._collect_persisted_settings()['guidance_depth_invert'])

    def test_invalid_numeric_input_restores_previous_value(self):
        app = self.app
        key = 'guidance_flow_updates'
        widget = app._host_settings['guidance_controls'][key]
        app._host_settings['analysis_vars'][key].set('nan')
        self.assertEqual(app._collect_settings()[key], 6)
        self.root.tk.call(widget.spin.cget('command'))
        self.assertEqual(app._host_settings['analysis_vars'][key].get(), '6')
        self.assertIsNone(app._module_reload_thread)

    def test_compact_iterations_and_consistent_section_insets(self):
        app = self.app
        self.assertFalse(any(key.startswith('preset_') for key in app._host_settings['guidance_controls']))
        self.assertIn('guidance_flow_updates', app._host_settings['guidance_controls'])
        expected = app._guidance_settings_frame.pack_info()['padx']
        for section in (app._modules_section, app._module_editor, app._guidance_advanced,
                        app._preview_section, app._host_section, app._export_section):
            self.assertEqual(section.pack_info()['padx'], expected)
            self.assertIs(section.master, app._export_inner)
        for section in (app._guidance_flow_advanced, app._guidance_depth_advanced,
                        app._guidance_display_section):
            self.assertEqual(int(section.pack_info()['padx']), 0)
            self.assertIs(section.master, app._guidance_settings_frame)
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        labels = [str(widget.cget('text')) for widget in descendants(app._guidance_settings_frame)
                  if isinstance(widget, gui.ttk.Label)]
        self.assertNotIn(gui.tr('guidance.iterations_hint'), labels)

    def test_guidance_canvas_uses_active_theme(self):
        for theme in ('light', 'dark'):
            self.app._apply_ui_theme(theme, persist=False)
            self.assertEqual(self.app._guidance_canvas.cget('bg'), ui_theme.tokens(theme)['panel'])

    def test_mousewheel_routes_to_guidance_without_changing_selection(self):
        app = self.app
        app._guidance_scrollbar_visible = True
        control = app._host_settings['guidance_controls']['depth']
        before = control.get()
        event = type('Event', (), {'widget': control, 'delta': -120})()
        with mock.patch.object(app._guidance_canvas, 'yview_scroll') as guidance_scroll, \
                mock.patch.object(app._export_canvas, 'yview_scroll') as export_scroll:
            self.assertEqual(app._on_workspace_mousewheel(event), 'break')
            guidance_scroll.assert_called_once_with(3, 'units')
            export_scroll.assert_not_called()
        self.assertEqual(control.get(), before)

    def test_real_wheel_events_do_not_edit_any_guidance_field(self):
        app = self.app
        app.workspace_tabs.select(app._guidance_page)
        for section in (app._guidance_flow_advanced, app._guidance_depth_advanced,
                        app._guidance_display_section):
            if section.collapsed:
                section.toggle()
        self.root.deiconify()
        self.root.update()
        controls = [app._host_settings['w_guidance']]
        controls += [widget for key, widget in app._host_settings['guidance_controls'].items()
                     if key not in ('device', 'profile', 'execution')]
        snapshot = app._collect_settings()
        for scrollable in (False, True):
            app._guidance_scrollbar_visible = scrollable
            for field in controls:
                inner = getattr(field, 'inner', field)
                if inner.winfo_class() not in ('TSpinbox', 'TCombobox'):
                    continue
                for focused in (False, True):
                    (inner if focused else self.root).focus_force()
                    for delta in (-120, 120):
                        with self.subTest(field=str(inner), focused=focused,
                                          scrollable=scrollable, delta=delta), \
                                mock.patch.object(app._guidance_canvas, 'yview_scroll') as scroll, \
                                mock.patch.object(app._export_canvas, 'yview_scroll') as other:
                            before = field.get()
                            inner.event_generate('<MouseWheel>', delta=delta)
                            self.assertEqual(field.get(), before)
                            self.assertEqual(app._collect_settings(), snapshot)
                            if scrollable:
                                scroll.assert_called_once_with(-3 if delta > 0 else 3, 'units')
                            else:
                                scroll.assert_not_called()
                            other.assert_not_called()

    def test_component_editor_shortcut_reveals_settings_paths(self):
        app = self.app
        self.assertIs(app._module_editor.master, app._export_inner)
        self.assertTrue(app._module_editor.collapsed)
        self.assertFalse(hasattr(app, '_module_advanced'))
        self.assertTrue(app._modules_section.collapsed)
        app._show_module_editor('depth')
        self.root.update_idletasks()
        self.assertIs(app.workspace_tabs._current, app._export_page)
        self.assertTrue(app._modules_section.collapsed)
        self.assertFalse(app._module_editor.collapsed)

    def test_settings_controls_move_without_duplicates_and_scroll_correctly(self):
        app = self.app
        def ancestor(widget, parent):
            while widget is not None:
                if widget is parent:
                    return True
                widget = getattr(widget, 'master', None)
            return False
        for key, widget in app._host_settings['guidance_controls'].items():
            page = app._export_page if key in ('device', 'profile', 'execution') else app._guidance_page
            self.assertTrue(ancestor(widget, page), key)
        stable = app._host_settings['guidance_controls']['guidance_depth_smoothing']
        self.assertTrue(ancestor(stable, app._guidance_depth_advanced.body))
        app._export_scrollbar_visible = True
        widget = app._host_settings['guidance_controls']['device']
        value = widget.get()
        event = type('Event', (), {'widget': widget, 'delta': -120})()
        with mock.patch.object(app._export_canvas, 'yview_scroll') as scroll, \
                mock.patch.object(app._guidance_canvas, 'yview_scroll') as wrong_scroll:
            self.assertEqual(app._on_workspace_mousewheel(event), 'break')
            scroll.assert_called_once_with(3, 'units')
            wrong_scroll.assert_not_called()
        self.assertEqual(widget.get(), value)
        snapshot = app._collect_settings()
        app._open_module_settings()
        self.root.update_idletasks()
        self.assertIs(app.workspace_tabs._current, app._export_page)
        self.assertFalse(app._modules_section.collapsed)
        self.assertEqual(app._collect_settings(), snapshot)


if __name__ == '__main__':
    unittest.main()
