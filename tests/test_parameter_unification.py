"""Common editing/transfer contracts, without native capture or GPU inference."""
from copy import deepcopy
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

from dlss5tool import app_settings, i18n, ui_theme
from dlss5tool.local_adjustments import LocalAdjustments, FIELDS
from dlss5tool.parameter_values import parse_number, format_number
from dlss5tool.parameter_transfer import from_realtime, to_realtime, describe
from dlss5tool.realtime import (NR, NR_SWITCH_PARAMETERS, effective_nr_skin, restore_nr_skin_memory,
    nr_preset, apply_nr_preset, validate_config, RealtimeClient)
from tests import test_realtime_inspector as inspector, test_workbench_ui as workbench


def catalog():
    p = inspector.parameter
    common = [p('style', default=0, integer=True, step=1), p('intensity'),
              p('localToneStrength'), p('localStructureStrength'),
              p('skinStructureStrength', default=0),
              p('useAutoMask', default=0, maximum=1, integer=True, step=1)]
    return {NR: {'available': True, 'parameters': common + [
        dict(item, name=prefix + item['name']) for prefix in ('pass2_', 'pass3_') for item in common] + [
        p('multiPass', default=1, minimum=1, maximum=3, integer=True, step=1)]}}


def config():
    return validate_config({'effects': [dict(name=NR, parameters={
        p['name']: p['default'] for p in catalog()[NR]['parameters']})]})


class ParameterValueTests(unittest.TestCase):
    def test_percentage_finite_and_fine_values(self):
        self.assertEqual(parse_number(' 75% ', percent=True), .75)
        self.assertEqual(format_number('0.35000000000000003'), '0.35')
        self.assertEqual(format_number('.123456'), '.123456')
        for raw in ('NaN', 'inf', '-inf', '1e500', 'abc', ''):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_number(raw, percent=True)
        with self.assertRaises(ValueError):
            parse_number('75%')

    def test_all_switches_preserve_memory_and_are_removed_from_native_config(self):
        draft = config()
        effect = draft['effects'][0]
        effect['parameters'].update({name: .67345 for name in NR_SWITCH_PARAMETERS})
        effect['disabled_parameters'] = list(NR_SWITCH_PARAMETERS)
        original = deepcopy(draft)
        actual = effective_nr_skin(draft)
        self.assertEqual(draft, original)
        self.assertNotIn('disabled_parameters', actual['effects'][0])
        for name in NR_SWITCH_PARAMETERS:
            self.assertEqual(actual['effects'][0]['parameters'][name], 0)
        self.assertEqual(restore_nr_skin_memory(actual, draft), draft)
        self.assertEqual(apply_nr_preset(draft, nr_preset(draft)), draft)
        legacy = nr_preset(config())
        self.assertNotIn('disabled_parameters', apply_nr_preset(draft, legacy)['effects'][0])

    def test_switch_validation_and_unconfirmed_memory(self):
        draft = config()
        for value in ('intensity', ['future'], ['multiPass'], [None]):
            draft['effects'][0]['disabled_parameters'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(draft)
        draft['effects'][0]['disabled_parameters'] = ['intensity']
        actual = config()
        self.assertEqual(restore_nr_skin_memory(actual, draft), actual)
        draft['effects'][0]['name'] = 'ThirdParty/Effect'
        with self.assertRaises(ValueError):
            validate_config(draft)

    def test_client_boundary_never_sends_switch_metadata(self):
        draft = config()
        draft['effects'][0]['disabled_parameters'] = ['intensity']
        client = SimpleNamespace(launch=mock.Mock(), send=mock.Mock(return_value=1))
        with mock.patch('dlss5tool.realtime.os.name', 'posix'):
            RealtimeClient.start(client, 10, draft, 20)
        sent = client.send.call_args.kwargs['config']['effects'][0]
        self.assertNotIn('disabled_parameters', sent)
        self.assertEqual(sent['parameters']['intensity'], 0)
        self.assertEqual(draft['effects'][0]['parameters']['intensity'], 1)


class ParameterTransferTests(unittest.TestCase):
    def test_round_trip_preserves_memory_only_first_pass_and_other_settings(self):
        source = config()
        source['effects'][0]['parameters'].update(intensity=.67345, skinStructureStrength=.8)
        source['effects'][0]['disabled_parameters'] = ['intensity']
        values = from_realtime(source, catalog(), 1)
        self.assertEqual(values['intensity'], ('0.67345', False))
        self.assertEqual(values['skin_struct'], ('0.8', False))
        target = config()
        target['effects'][0]['parameters']['pass2_intensity'] = 1.55
        target['effects'][0]['disabled_parameters'] = ['pass2_intensity']
        target['effects'].append(dict(name='ThirdParty/Color', parameters={'amount': .5}))
        target['quality']['total'] = .25
        target['capture_title_bar'] = True
        original = deepcopy(target)
        index, copied = to_realtime(values, target, catalog())
        self.assertEqual(index, 0)
        self.assertEqual(target, original)
        self.assertEqual(copied['effects'][1], target['effects'][1])
        self.assertEqual(copied['quality'], target['quality'])
        self.assertTrue(copied['capture_title_bar'])
        self.assertEqual(copied['effects'][0]['parameters']['pass2_intensity'], 1.55)
        self.assertEqual(set(copied['effects'][0]['disabled_parameters']), {'intensity', 'pass2_intensity'})
        self.assertEqual(from_realtime(copied, catalog(), 1), values)
        self.assertIn('0.67345', describe(values))

    def test_no_clipping_no_ambiguous_target_and_no_partial_copy(self):
        draft = config()
        draft['effects'][0]['parameters']['intensity'] = 1.5
        with self.assertRaises(ValueError):
            from_realtime(draft, catalog(), 1)
        values = from_realtime(draft, catalog(), 5)
        values['intensity'] = ('3', True)
        before = deepcopy(draft)
        with self.assertRaises(ValueError):
            to_realtime(values, draft, catalog())
        self.assertEqual(draft, before)
        with self.assertRaises(ValueError):
            from_realtime(draft, {}, 5)
        draft['effects'].append(deepcopy(draft['effects'][0]))
        with self.assertRaises(ValueError):
            from_realtime(draft, catalog(), 5)


class LocalAdjustmentTests(unittest.TestCase):
    def test_render_status_never_confirms_invalid_or_newer_draft(self):
        editor = self.editor
        editor.set_operation_state('applied', hdr=True)
        row = editor.rows['nr_residual.residualSaturation']
        row.variable.set('nan')
        editor.changed()
        editor.set_operation_state('applied')
        self.assertEqual(editor.diagnostic_snapshot()['state'], 'invalid')
        row.variable.set('.7')
        editor.changed()
        editor.set_operation_state('applied')
        self.assertEqual(editor.diagnostic_snapshot()['state'], 'dirty')
        self.assertTrue(editor.commit('nr_residual.residualSaturation'))
        self.assertEqual(editor.diagnostic_snapshot()['state'], 'idle')
        self.model.assert_not_called()
        self.output.assert_called_once()
        editor.set_operation_state('pending')
        self.assertEqual(editor.diagnostic_snapshot()['state'], 'pending')
        editor.set_operation_state('applied')
        self.assertEqual(editor.diagnostic_snapshot()['state'], 'applied')
        editor.reset(['nr_residual.residualSaturation'])
        editor.undo()
        self.assertEqual(row.value(), .7)
        self.assertNotEqual(editor.diagnostic_snapshot()['state'], 'applied')

    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry('370x760')
        ui_theme.apply_ttk(self.root, ui_theme.tokens('dark'))
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.settings = dict(v_style=tk.StringVar(self.root, value='Default'),
                             v_enable_5x=tk.BooleanVar(self.root, value=False))
        for name, _, value_key, switch_key, _ in FIELDS:
            self.settings[value_key] = tk.DoubleVar(self.root, value=app_settings.DEFAULTS[name])
            self.settings[switch_key] = tk.BooleanVar(self.root, value=True)
        self.model, self.output = mock.Mock(), mock.Mock()
        self.editor = LocalAdjustments(self.root, self.settings, ui=ui_theme.tokens('dark'),
            styles={'Default': 0, 'Natural': 1, 'Cinematic': 2}, on_model=self.model,
            on_output=self.output, on_copy=mock.Mock())
        self.editor.pack(fill='both', expand=True)
        self.root.update()

    def tearDown(self):
        self.editor.destroy()
        self.root.destroy()
        self.assertEqual(self.errors, [])
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    def test_switch_precision_reset_undo_and_output_only_callback(self):
        row = self.editor.rows['intensity']
        row.number_text.set('67.345%'); row._commit_input()
        self.assertEqual(self.settings['v_intensity'].get(), .67345)
        row.enable_toggle.invoke()
        self.assertEqual(self.settings['v_intensity'].get(), .67345)
        self.assertFalse(self.settings['v_use_intensity'].get())
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=True):
            self.editor.reset_all()
        self.assertEqual(row.value(), 1)
        self.assertTrue(row.enabled_variable.get())
        self.editor.undo()
        self.assertEqual(row.value(), .67345)
        self.assertFalse(row.enabled_variable.get())
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=True):
            self.editor.reset_all()
        self.model.reset_mock()
        out = self.editor.rows['output_mix']
        out.number_text.set('.45'); out._commit_input()
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=True):
            self.editor.reset_all()
        self.editor.undo()
        self.model.assert_not_called()
        self.assertEqual(self.output.call_count, 3)

    def test_invalid_draft_reset_and_undo_do_not_feed_renderer(self):
        row = self.editor.rows['intensity']
        row.number_text.set('nan'); row._commit_input()
        self.assertFalse(self.editor.valid())
        self.assertEqual(self.settings['v_intensity'].get(), 1)
        self.assertIn(i18n.tr('label.strength'), row.feedback.cget('text'))
        self.model.assert_not_called()
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=True):
            self.editor.reset_all()
        self.assertTrue(self.editor.valid())
        self.model.reset_mock()
        self.editor.undo()
        self.assertEqual(row.variable.get(), 'nan')
        self.assertFalse(self.editor.valid())
        self.model.assert_not_called()

    def test_single_menu_preserves_edits_on_cancel_and_reset_is_undoable(self):
        row = self.editor.rows['intensity']
        row.number_text.set('.37'); row._commit_input()
        row.enable_toggle.invoke()
        self.settings['v_enable_5x'].set(True)
        before = self.editor.snapshot()
        undo = deepcopy(self.editor._undo)
        self.assertTrue(all(row.reset_button is None for row in self.editor.rows.values()))
        self.assertEqual(self.editor.summary.cget('text'), '')
        self.assertFalse(self.editor.summary.winfo_manager())
        with mock.patch('tkinter.Menu.tk_popup') as popup, mock.patch('tkinter.Menu.grab_release'):
            self.editor.more_button.invoke()
        menu = self.editor._menu
        self.assertGreaterEqual(popup.call_args.args[0], self.editor.winfo_rootx())
        self.assertLessEqual(popup.call_args.args[0] + menu.winfo_reqwidth(),
                             self.editor.winfo_rootx() + self.editor.winfo_width())
        self.assertEqual(menu.index('end'), 5)
        self.assertEqual(menu.entrycget(0, 'label'), i18n.tr('parameter.copy_from_realtime'))
        self.assertEqual(menu.entrycget(2, 'label'), i18n.tr('parameter.reset_local'))
        menu.invoke(0)
        self.editor._on_copy.assert_called_once()
        self.model.reset_mock()
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=False):
            menu.invoke(2)
        self.assertEqual(self.editor.snapshot(), before)
        self.assertEqual(self.editor._undo, undo)
        self.model.assert_not_called()
        with mock.patch('dlss5tool.local_adjustments.messagebox.askokcancel', return_value=True):
            menu.invoke(2)
        self.assertTrue(self.settings['v_enable_5x'].get())
        self.assertEqual(row.value(), 1)
        self.assertTrue(row.enabled_variable.get())
        self.editor.undo_button.invoke()
        self.assertEqual(self.editor.snapshot(), before)

    def test_reopening_editor_keeps_values_switches_and_hides_valid_summary(self):
        row = self.editor.rows['local_tone']
        row.number_text.set('.42'); row._commit_input()
        row.enable_toggle.invoke()
        before = self.editor.snapshot()
        self.editor.destroy()
        self.editor = LocalAdjustments(self.root, self.settings, ui=ui_theme.tokens('dark'),
            styles={'Default': 0, 'Natural': 1, 'Cinematic': 2}, on_model=self.model,
            on_output=self.output, on_copy=mock.Mock())
        self.editor.pack(fill='both', expand=True)
        self.assertEqual(self.editor.snapshot(), before)
        row = self.editor.rows['intensity']
        row.number_text.set('bad'); row._commit_input()
        self.assertEqual(self.editor.summary.winfo_manager(), 'pack')
        self.assertEqual(self.editor.summary.cget('text'), i18n.tr('parameter.local_invalid'))
        self.editor.undo_button.invoke()
        self.assertEqual(self.editor.snapshot(), before)
        self.assertFalse(self.editor.summary.winfo_manager())

    def test_group_reset_single_callback_and_copy_excludes_composition(self):
        values = from_realtime(config(), catalog(), 1)
        values['intensity'] = ('.32123', False)
        self.editor.rows['output_mix'].number_text.set('.66')
        self.editor.rows['output_mix']._commit_input()
        before = self.editor.snapshot()
        self.editor.replace_common(values)
        self.assertEqual(self.editor.rows['output_mix'].value(), .66)
        self.editor.undo()
        self.assertEqual(self.editor.snapshot(), before)
        self.model.reset_mock()
        self.editor.reset(tuple(self.editor.rows))
        self.assertEqual(self.editor.rows['output_mix'].value(), 1)
        self.output.assert_called()
        self.model.assert_not_called()

    def test_appearance_and_regions_commit_output_only_and_undo_together(self):
        before = self.editor.snapshot()
        self.editor.commit_regions([[.1, .2, .8, .9]])
        self.model.assert_not_called()
        self.output.assert_called_once()
        self.assertTrue(self.editor.appearance_settings()['nr_quality']['protection'])
        self.editor.undo()
        self.assertEqual(self.editor.snapshot(), before)
        row = self.editor.rows['nr_residual.residualSaturation']
        row.number_text.set('1.2345'); row._commit_input()
        self.assertEqual(self.editor.appearance_settings()['nr_residual']['residualSaturation'], 1.2345)
        self.editor.set_limit(1)
        self.assertEqual(row.value(), 1.2345)
        self.model.assert_not_called()

    def test_composition_dependency_survives_edit_feedback_and_theme(self):
        self.editor.set_composition_enabled(False)
        self.editor.rows['output_mix'].variable.set('.7')
        self.editor.rows['intensity'].variable.set('.5')
        for theme in ('dark', 'light'):
            self.editor.apply_theme(ui_theme.tokens(theme))
            self.editor.refresh()
            self.assertFalse(self.editor.rows['output_mix'].enabled)
            self.assertEqual(self.editor.rows['output_mix'].slider.cget('state'), 'disabled')

    def test_bilingual_local_controls_fit_minimum_inspector_width(self):
        for language, width in (('zh_CN', 320), ('en_US', 420)):
            i18n.set_language(language)
            self.editor.destroy()
            self.editor = LocalAdjustments(self.root, self.settings, ui=ui_theme.tokens('dark'),
                styles={'Default': 0, 'Natural': 1, 'Cinematic': 2}, on_model=self.model,
                on_output=self.output, on_copy=mock.Mock())
            self.editor.pack(fill='both', expand=True)
            for section in self.editor.sections.values():
                if section.collapsed:
                    section.toggle()
            for theme in ('dark', 'light'):
                self.editor.apply_theme(ui_theme.tokens(theme))
                self.root.geometry(f'{width}x760')
                self.root.update()
                for name, *_ in FIELDS:
                    row = self.editor.rows[name]
                    switch, field = row.enable_toggle, row.field
                    self.assertGreater(field.winfo_width(), 60)
                    self.assertLessEqual(switch.winfo_rootx() + switch.winfo_width(), field.winfo_rootx())
                    self.assertIsNone(row.reset_button)
                    self.assertLessEqual(field.winfo_rootx() + field.winfo_width(), row.winfo_rootx() + row.winfo_width())
                    self.assertEqual(switch.winfo_width(), switch.winfo_reqwidth())
                self.assertLessEqual(self.editor.undo_button.winfo_rootx() + self.editor.undo_button.winfo_width(),
                                     self.editor.more_button.winfo_rootx())
                self.assertIs(self.editor.pack_slaves()[0], self.editor.actions)
                self.assertLessEqual(self.editor.actions.winfo_rooty() + self.editor.actions.winfo_height(),
                                     self.editor.rows['style'].winfo_rooty())
                self.assertEqual(self.editor.undo_button.pack_info()['side'], 'right')
                self.assertLessEqual(self.editor.heading.winfo_rootx() + self.editor.heading.winfo_width(),
                                     self.editor.undo_button.winfo_rootx())
                self.assertEqual(self.editor.more_button.cget('icon'), 'more')
                self.assertTrue(self.editor.more_button._icon_only)
                self.assertLessEqual(self.editor.more_button.winfo_width(), ui_theme.scale_px(self.root, 40))


class RealtimeSwitchTests(unittest.TestCase):
    setUp = inspector.RealtimeInspectorTests.setUp
    tearDown = inspector.RealtimeInspectorTests.tearDown
    event = inspector.RealtimeInspectorTests.event
    status = inspector.RealtimeInspectorTests.status

    def prepare(self, running=False):
        self.items = catalog()[NR]['parameters']
        self.event(dict(event='catalog', effects=[dict(catalog()[NR], name=NR)]))
        if running:
            self.ui._active = self.ui._render_confirmed = True
            self.ui._run_id = 12
            self.ui._running_config = effective_nr_skin(self.ui.collect())
            self.ui._applied_effects = deepcopy(self.ui._running_config['effects'])
            self.ui._parameter_modes = {(0, p['name']): 1 for p in self.items}
            self.ui._refresh_editor_state()
            self.client.send.reset_mock()

    def test_group_copy_cancels_queued_quality_and_undo_restores_regions(self):
        self.prepare(True)
        before = deepcopy(self.ui.collect())
        values = from_realtime(before, catalog(), 1, ('quality', 'regions'))
        values['nr_quality.total'] = ('.4', None)
        values['nr_quality.protection'] = ('1', None)
        values['_regions'] = ('[[0, 0, 0.5, 1]]', None)
        index, candidate = to_realtime(values, before, catalog(), ('quality', 'regions'))
        self.ui._queued_quality_apply = True
        self.ui.replace_common_parameters(index, candidate, ('quality', 'regions'))
        self.assertFalse(self.ui._queued_quality_apply)
        self.client.send.assert_not_called()
        self.client.start.assert_not_called()
        self.assertEqual(self.ui.collect()['quality'], candidate['quality'])
        self.ui._active = False
        self.ui._undo_parameter_edit()
        self.assertEqual(self.ui.collect(), before)

    def test_live_off_waits_confirmation_and_preserves_memory_on_reopen(self):
        self.prepare(True)
        row = self.ui.parameter_rows['intensity']
        row.enable_toggle.invoke()
        self.assertEqual(self.ui._live_expected, {(0, 'intensity'): 0})
        self.assertTrue(row.unapplied)
        effects = deepcopy(self.ui._applied_effects)
        effects[0]['parameters']['intensity'] = 0
        self.status(effects)
        saved = self.store.data['session']['effects'][0]
        self.assertEqual(saved['parameters']['intensity'], 1)
        self.assertEqual(saved['disabled_parameters'], ['intensity'])
        self.ui.apply_config(self.store.data['session'])
        self.assertFalse(self.ui.parameter_rows['intensity'].enabled_variable.get())
        self.assertEqual(self.ui.parameter_rows['intensity'].value(), 1)

    def test_reset_undo_pass_dependency_and_inline_validation(self):
        self.prepare()
        for name in NR_SWITCH_PARAMETERS:
            self.assertIsNotNone(self.ui.parameter_rows[name].enable_toggle)
        self.assertFalse(self.ui.parameter_rows['pass2_intensity'].enabled)
        row = self.ui.parameter_rows['intensity']
        row.enable_toggle.invoke()
        with mock.patch('dlss5tool.realtime_ui.messagebox.askokcancel', return_value=True):
            self.ui._reset_current_effect()
        self.assertTrue(row.enabled_variable.get())
        self.ui.undo_button.invoke()
        self.assertFalse(row.enabled_variable.get())
        row.enable_toggle.invoke()
        row.number_text.set('nan'); row._commit_input()
        self.assertEqual(self.ui._editor_state, 'invalid')
        self.assertTrue(row._feedback_state[3])
        with mock.patch('dlss5tool.realtime_ui.messagebox.askokcancel', return_value=True):
            self.ui._reset_current_effect()
        self.ui.range_toggle.invoke()
        row.number_text.set('123.456%'); row._commit_input()
        self.assertEqual(row.value(), 1.23456)

    def test_copy_draft_cancels_old_queued_apply_and_undo_restores(self):
        self.prepare(True)
        row = self.ui.parameter_rows['intensity']
        row.number_text.set('.8'); row._commit_input()
        row.number_text.set('.6'); row._commit_input()
        self.assertTrue(self.ui._queued_parameter_apply)
        before = self.ui._parameter_snapshot()
        values = from_realtime(config(), catalog(), 1)
        values['intensity'] = ('.3', False)
        index, candidate = to_realtime(values, self.ui.collect(), catalog())
        self.client.send.reset_mock()
        self.ui.replace_common_parameters(index, candidate)
        self.assertFalse(self.ui._queued_parameter_apply)
        self.client.send.assert_not_called()
        effects = deepcopy(self.ui._applied_effects)
        effects[0]['parameters']['intensity'] = .8
        self.status(effects)
        self.assertEqual(self.ui._editor_state, 'dirty')
        self.assertFalse(self.ui._live_expected)
        self.assertEqual(self.ui.parameter_rows['intensity'].value(), .3)
        self.ui.undo_button.invoke()
        self.assertTrue(self.ui._same_snapshot(self.ui._parameter_snapshot(), before))
        self.client.start.assert_not_called()


class TransferIntegrationTests(unittest.TestCase):
    setUp = workbench.WorkbenchUiTests.setUp
    tearDown = workbench.WorkbenchUiTests.tearDown

    @mock.patch('dlss5tool.appearance_ui.choose_groups', return_value=('basic',))
    def test_cancel_and_confirm_both_directions_without_start(self, _groups):
        self.app._select_workbench_mode('realtime')
        workspace = self.app._realtime_workspace
        workspace.catalog.update(catalog())
        workspace._select_effect()
        local = self.app._local_adjustments
        local.rows['intensity'].number_text.set('.4567')
        local.rows['intensity']._commit_input()
        old = workspace.collect()
        with mock.patch('dlss5tool.gui.messagebox.askyesno', return_value=False):
            self.app._copy_to_realtime()
        self.assertEqual(workspace.collect(), old)
        with mock.patch('dlss5tool.gui.messagebox.askyesno', return_value=True):
            self.app._copy_to_realtime()
        self.assertEqual(workspace.parameter_rows['intensity'].value(), .4567)
        workspace.parameter_rows['intensity'].variable.set('.3333')
        with mock.patch('dlss5tool.gui.messagebox.askyesno', return_value=True):
            self.app._copy_from_realtime()
        self.assertEqual(local.rows['intensity'].value(), .3333)
        local.undo()
        self.assertEqual(local.rows['intensity'].value(), .4567)
        self.client.start.assert_not_called()

    def test_invalid_local_draft_blocks_queue_and_export(self):
        self.app._local_adjustments.rows['intensity'].number_text.set('9')
        with mock.patch('dlss5tool.gui.messagebox.showwarning') as warning, \
             mock.patch('dlss5tool.gui.filedialog.asksaveasfilename') as save:
            self.app.export_dlss()
            self.app._add_paths_to_queue([])
            self.app.apply_current_settings_to_queue()
        self.assertEqual(warning.call_count, 3)
        save.assert_not_called()
        self.assertFalse(self.app._queue_jobs)


if __name__ == '__main__':
    unittest.main()
