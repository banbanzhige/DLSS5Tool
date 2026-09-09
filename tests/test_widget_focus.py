import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

import ui_theme
from ui_widgets import (
    AccentSlider, CheckToggle, ChipGroup, ChromeButton, ChromeCombobox, ChromeSpinbox,
    CollapsibleSection, _has_focus,
)


class WidgetFocusTests(unittest.TestCase):
    def test_fields_ignore_real_wheel_events_but_keep_explicit_edits(self):
        root = tk.Tk()
        root.withdraw()
        try:
            value = tk.IntVar(root, 6)
            choice = tk.StringVar(root, 'B')
            changed, scrolled = [], []
            value.trace_add('write', lambda *_: changed.append('number'))
            choice.trace_add('write', lambda *_: changed.append(choice.get()))
            spin = ChromeSpinbox(root, textvariable=value, from_=1, to=32)
            combo = ChromeCombobox(root, textvariable=choice, values=['A', 'B', 'C'], state='readonly')
            spin.pack()
            combo.pack()
            root.bind_all('<MouseWheel>', lambda event: scrolled.append(event.delta))
            root.geometry('320x140')
            root.deiconify()
            root.update()
            for theme in ('light', 'dark'):
                ui_theme.apply_ttk(root, ui_theme.tokens(theme))
                for control in (spin.spin, combo.combo):
                    for focused in (False, True):
                        (control if focused else root).focus_force()
                        root.update()
                        for delta in (-120, 120):
                            count = len(scrolled)
                            control.event_generate('<MouseWheel>', delta=delta)
                            root.update()
                            self.assertEqual((value.get(), choice.get()), (6, 'B'))
                            self.assertEqual(changed, [])
                            self.assertEqual(len(scrolled), count + 1)
                            self.assertEqual(scrolled[-1], delta)
                for cls in ('TSpinbox', 'TCombobox'):
                    for sequence in ui_theme._COMBOBOX_WHEEL_SEQUENCES:
                        self.assertEqual(root.bind_class(cls, sequence), '')
            # The same native virtual action used by arrow clicks/keys survives.
            spin.spin.event_generate('<<Increment>>')
            root.update()
            self.assertEqual(value.get(), 7)
            spin.spin.event_generate('<<Decrement>>')
            root.update()
            self.assertEqual(value.get(), 6)
            spin.spin.delete(0, 'end')
            spin.spin.insert(0, '9')
            self.assertEqual(value.get(), 9)
            combo.combo.current(2)
            combo.combo.event_generate('<<ComboboxSelected>>')
            root.update()
            self.assertEqual(choice.get(), 'C')
        finally:
            for handle in root.tk.splitlist(root.tk.call('after', 'info')):
                root.after_cancel(handle)
            root.destroy()

    def test_focus_path_comparison_handles_native_popup_and_shutdown(self):
        widget = mock.MagicMock()
        widget.__str__.return_value = '.slider'
        for path, expected in (('.slider', True), ('.combo.popdown.f.l', False), ('', False), ('none', False)):
            widget.tk.call.return_value = path
            self.assertEqual(_has_focus(widget), expected)
        widget.focus_get.assert_not_called()
        widget.tk.call.side_effect = tk.TclError('application has been destroyed')
        self.assertFalse(_has_focus(widget))

    def test_native_combobox_popup_does_not_break_redraw_or_keyboard_focus(self):
        root = tk.Tk()
        root.withdraw()
        errors = []
        root.report_callback_exception = lambda *error: errors.append(error)
        try:
            number = tk.DoubleVar(root, 0.2)
            checked = tk.BooleanVar(root, False)
            choice = tk.StringVar(root, 'A')
            combo = ttk.Combobox(root, values=['A', 'B'], state='readonly')
            combo.current(0)
            combo.pack()
            slider = AccentSlider(root, variable=number)
            button = ChromeButton(root, text='Test')
            chips = ChipGroup(root, choice, ['A', 'B'], connected=True)
            toggle = CheckToggle(root, 'Test', checked)
            section = CollapsibleSection(root, 'Test')
            for widget in (slider, button, chips, toggle, section):
                widget.pack(fill='x')
            root.geometry('420x360')
            root.deiconify()
            root.update()
            for theme in ('light', 'dark'):
                root.tk.call('ttk::combobox::Post', str(combo))
                popup = str(root.tk.call('ttk::combobox::PopdownWindow', str(combo)))
                root.tk.call('focus', '-force', popup + '.f.l')
                root.update()
                self.assertTrue(str(root.tk.call('focus')).startswith(popup))
                # Prove the underlying Tkinter lookup still reproduces the old bug.
                with self.assertRaises(KeyError):
                    root.focus_get()
                number.set(0.6)
                checked.set(not checked.get())
                choice.set('B')
                for widget in (slider, button, chips, toggle, section):
                    widget.apply_theme(ui_theme.tokens(theme))
                root.update()
                self.assertFalse(_has_focus(slider))
                self.assertEqual(slider.itemcget(slider._items['focus'], 'outline'), '')
                root.tk.call('ttk::combobox::Unpost', str(combo))
                slider.focus_force()
                root.update()
                slider._redraw()
                self.assertTrue(_has_focus(slider))
                self.assertNotEqual(slider.itemcget(slider._items['focus'], 'outline'), '')
                slider.config(state='disabled')
                self.assertEqual(slider.itemcget(slider._items['focus'], 'outline'), '')
                slider.config(state='normal')
            self.assertEqual(errors, [])
        finally:
            root.destroy()
