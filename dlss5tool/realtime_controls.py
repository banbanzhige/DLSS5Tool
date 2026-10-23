"""Metadata-backed inspector rows using the local-media studio controls."""
import math
import tkinter as tk
from tkinter import ttk

from dlss5tool import ui_theme
from dlss5tool.i18n import tr
from dlss5tool.parameter_values import parse_number, format_number, clean_step
from dlss5tool.parameter_contract import feedback_state
from dlss5tool.realtime_labels import effect_text, parameter_label
from dlss5tool.ui_widgets import (AccentSlider, CheckToggle, ChipGroup,
                                  ChromeButton, ChromeCombobox, ChromeSpinbox, CollapsibleSection, Tooltip)


class InspectorChoice(ChromeCombobox):
    def current(self, index=None):
        return self.combo.current() if index is None else self.combo.current(index)


class EffectChainCards(tk.Frame):
    """Focusable ordered steps, with list-like selection for the editor."""
    def __init__(self, parent, *, ui, on_edit, on_replace, on_remove):
        super().__init__(parent, takefocus=True, borderwidth=0)
        self._ui = dict(ui)
        self._items = []
        self._cards = []
        self._selected = None
        self._actions = (on_edit, on_replace, on_remove)
        self.bind('<Up>', lambda _: self._nudge(-1))
        self.bind('<Down>', lambda _: self._nudge(1))
        self.bind('<Return>', lambda _: on_edit())
        self.bind('<space>', lambda _: on_edit())
        self.apply_theme(ui)

    def set_items(self, items, selected=0):
        for card, *_ in self._cards:
            card.destroy()
        self._items = items
        self._cards = []
        self._selected = min(max(selected, 0), len(items) - 1) if items else None
        for index, (title, subtitle) in enumerate(items):
            card = tk.Frame(self, highlightthickness=1, padx=8, pady=6)
            card.pack(fill='x', pady=3)
            heading = tk.Label(card, text=f'{index + 1}  {title}', anchor='w', justify='left', font=ui_theme.UI_FONT_BOLD)
            heading.pack(fill='x')
            detail = tk.Label(card, text=subtitle, anchor='w', justify='left', font=ui_theme.UI_FONT)
            detail.pack(fill='x', pady=(2, 0))
            for widget in (card, heading, detail):
                widget.bind('<Button-1>', lambda _, i=index: self._choose(i))
            for widget in (heading, detail):
                widget.bind('<Configure>', lambda e, w=widget: w.configure(wraplength=max(30, e.width - 4)))
            actions = tk.Frame(card)
            buttons = []
            for key, action in zip(('adjust', 'replace', 'remove'), self._actions):
                button = ChromeButton(actions, text=tr('realtime.workflow.' + key), variant='tool', ui=self._ui,
                                      command=lambda a=action, i=index: self._act(i, a))
                button.pack(side='left', padx=(0, 4))
                buttons.append(button)
            self._cards.append((card, heading, detail, actions, buttons))
        self.apply_theme(self._ui)

    def _act(self, index, action):
        self._choose(index)
        if self._selected == index:
            action()

    def _choose(self, index):
        self.selection_set(index)
        self.focus_set()
        self.event_generate('<<ListboxSelect>>')

    def _nudge(self, delta):
        if self._items:
            self._choose(max(0, min(len(self._items) - 1, (self._selected or 0) + delta)))
        return 'break'

    def curselection(self):
        return () if self._selected is None else (self._selected,)

    def selection_clear(self, first, last=None):
        self._selected = None

    def selection_set(self, index):
        self._selected = int(index) if 0 <= int(index) < len(self._items) else None
        self.apply_theme(self._ui)

    def get(self, first, last=None):
        values = [title + ' · ' + subtitle for title, subtitle in self._items]
        if last is None:
            return values[first]
        return tuple(values[first:] if last == 'end' else values[first:last + 1])

    def update_subtitles(self, subtitles):
        for index, subtitle in enumerate(subtitles):
            if index < len(self._items) and self._items[index][1] != subtitle:
                self._items[index] = (self._items[index][0], subtitle)
                self._cards[index][2].configure(text=subtitle)

    def apply_theme(self, ui):
        self._ui = dict(ui)
        self.configure(bg=ui['panel'])
        for index, (card, title, detail, actions, buttons) in enumerate(self._cards):
            selected = index == self._selected
            color = ui['select_bg'] if selected else ui['entry_bg']
            card.configure(bg=color, highlightbackground=ui['accent'] if selected else ui['line'])
            title.configure(bg=color, fg=ui['text'])
            detail.configure(bg=color, fg=ui['muted'])
            actions.configure(bg=color)
            if selected:
                actions.pack(fill='x', pady=(4, 0))
            else:
                actions.pack_forget()
            for button in buttons:
                button.apply_theme(ui)
            buttons[-1].configure(state='disabled' if len(self._items) == 1 else 'normal')


class HoverHelp(Tooltip):
    """Help on existing controls, also available to keyboard focus."""
    def __init__(self, widget, text):
        super().__init__(widget, text)
        widget.bind('<FocusIn>', self._show, add='+')
        for event in ('<FocusOut>', '<Escape>', '<Unmap>', '<Destroy>'):
            widget.bind(event, self._hide, add='+')

    def apply_theme(self, ui):
        self._hide()


class InlineHelp(ttk.Frame):
    """A stable hint row; details appear only in a hover/focus tooltip."""
    def __init__(self, parent, short, details, *, ui, variable=None, heading=False):
        stage = str(parent.cget('style')) == 'Stage.TFrame'
        frame_style = 'Stage.TFrame' if stage else 'Panel.TFrame'
        super().__init__(parent, style=frame_style)
        row = self.row = ttk.Frame(self, style=frame_style)
        row.pack(fill='x')
        self.label = ttk.Label(row, text=short, textvariable=variable,
                               style='RealtimeStageMuted.TLabel' if stage else 'Hint.TLabel',
                               justify='left', takefocus=True)
        if heading:
            self.label.configure(style='RealtimeStage.TLabel' if stage else 'TLabel', font=ui_theme.UI_FONT_BOLD)
        self.label.pack(side='left', fill='x', expand=True)
        self.label.bind('<Configure>', lambda e: self.label.configure(wraplength=max(30, e.width)))
        self.tooltip = HoverHelp(self.label, details)

    def set_details(self, text):
        if self.tooltip.text == text:
            return
        self.tooltip._hide()
        self.tooltip.text = text

    def apply_theme(self, ui):
        # ttk owns the row colors. Close any popup using the old palette.
        self.tooltip._hide()

    def destroy(self):
        self.tooltip._hide()
        super().destroy()


class InspectorGroup(ttk.Frame):
    """A visible heading, not another disclosure level around related controls."""
    def __init__(self, parent, title):
        super().__init__(parent, style='Panel.TFrame')
        self._header = ttk.Label(self, text=title, style='TLabel', font=ui_theme.UI_FONT_BOLD)
        self._header.pack(fill='x', pady=(0, 6))
        self._header.bind('<Configure>', lambda e: self._header.configure(wraplength=max(40, e.width)))
        self.body = ttk.Frame(self, style='Panel.TFrame')
        self.body.pack(fill='x')


class InspectorSection(CollapsibleSection):
    def _redraw_header(self):
        """Wrap long translated/native titles without hiding their switches."""
        header = self._header
        width = header.winfo_width()
        if width <= 1:
            width = ui_theme.scale_px(header, 360)
        header.configure(bg=self._ui.get('hover' if self._hover else 'panel', '#161d24'))
        header.delete('all')
        text = header.create_text(8, 0, anchor='nw', justify='left',
            text=('▸' if self._collapsed else '▾') + '  ' + self._title,
            width=max(40, width - 16), fill=self._ui.get('text', '#e8eef3'), font=ui_theme.UI_FONT)
        bounds = header.bbox(text)
        height = max(ui_theme.control_height(header), bounds[3] - bounds[1] + 12)
        header.move(text, 0, (height - (bounds[3] - bounds[1])) / 2 - bounds[1])
        if int(header.cget('height')) != height:
            header.configure(height=height)
        if str(header.tk.call('focus')) == str(header):
            header.create_rectangle(1, 1, width - 1, height - 1,
                                    outline=self._ui.get('accent', '#4aa8b8'))

    def add_enable_toggle(self, variable, command, hint):
        """Keep expansion and enabling as separate keyboard-accessible actions."""
        header_row = ttk.Frame(self, style='Panel.TFrame')
        header_row.pack(fill='x', before=self._header)
        self._header.pack_forget()
        toggle = CheckToggle(header_row, tr('realtime.editor.enable_pass'), variable, command=command, ui=self._ui)
        toggle.pack(side='right', padx=(4, 0))
        # Reserve the switch before the canvas (whose requested width may be
        # larger than the narrow inspector) takes the remaining cavity.
        self._header.pack(in_=header_row, side='left', fill='x', expand=True)
        self._header.configure(width=1)
        # pack(in_=...) does not reparent the Canvas. Raise it above its newer
        # sibling container, otherwise the opaque frame hides the pass title.
        self._header.tk.call('raise', self._header._w, header_row._w)
        Tooltip(toggle, hint)
        return toggle

    def set_title(self, title):
        if self._title != title:
            self._title = title
            self._redraw_header()


class InspectorChips(ChipGroup):
    """Disabled choices keep selection, but ignore both mouse and keyboard."""
    enabled = True

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(takefocus=int(enabled), cursor='hand2' if enabled else '')
        self.apply_theme(self._palette)

    def apply_theme(self, ui):
        self._palette = dict(ui)
        colors = dict(ui)
        if not self.enabled:
            colors.update(accent=ui['muted'], accent_dim=ui['line'], select_bg=ui['surface'])
        super().apply_theme(colors)

    def _on_click(self, event):
        if self.enabled:
            return super()._on_click(event)
        return 'break'

    def _nudge(self, step):
        return super()._nudge(step) if self.enabled else 'break'


class ParameterRow(ttk.Frame):
    """Keep native IDs/values; only user commits notify the controller."""
    def __init__(self, parent, descriptor, variable, *, ui, on_commit, on_change,
                 choices=None, chips=False, on_reset=None, label=None, hint='', enabled_variable=None,
                 allow_percent=False, range_hint=False, checkbox=False):
        super().__init__(parent, style='Panel.TFrame')
        self.descriptor = descriptor
        self._ui = dict(ui)
        self.allow_percent = allow_percent
        self.maximum = descriptor['max']
        self._range_hint = range_hint
        self._tooltips = []
        self.variable = variable
        self._commit = on_commit
        self._change = on_change
        self._widgets = []
        self.slider = self.field = self.toggle = self.choice = None
        self._syncing = False
        self._drag_changed = False
        self._trace = None
        self._input_trace = None
        self._enable_trace = None
        self.enabled_variable = enabled_variable
        self.enable_toggle = None
        self.enabled = True
        self.disabled_reason = ''
        self.nondefault = self.unapplied = False
        self._feedback_state = None
        self.choices = list(descriptor.get('choices') or []) if choices is None else choices
        full_label = parameter_label(descriptor)
        label = label or full_label.split('\n', 1)[0]
        self.title = label
        header = ttk.Frame(self, style='Panel.TFrame')
        header.pack(fill='x')
        self.reset_button = None
        if on_reset is not None:
            self.reset_button = ChromeButton(header, text='↺', command=on_reset, variant='tool', ui=ui)
            self.reset_button.pack(side='right', padx=(4, 0))
            Tooltip(self.reset_button, tr('realtime.editor.reset_parameter'))
            self._widgets.append(self.reset_button)
        self.label = ttk.Label(header, text=label, style='TLabel', justify='left')
        self.label.bind('<Configure>', lambda e: self.label.configure(wraplength=max(40, e.width)))
        self._base_help_text = hint or full_label
        self.help_text = self._base_help_text
        self._tooltips.append(Tooltip(self.label, self.help_text))
        left_toggle = checkbox or descriptor['name'].endswith('uiCorrection')
        if self.choices:
            self.display = tk.StringVar(self)
            labels = [effect_text(text) for _, text in self.choices]
            # Duplicate labels must still map unambiguously to the original values.
            self.choice_labels = [f'{label} ({value})' if labels.count(label) > 1 else label
                                  for (value, _), label in zip(self.choices, labels)]
            if chips and len(self.choices) <= 3:
                self.choice = InspectorChips(self, self.display, self.choice_labels,
                                             command=self._choose, ui=ui)
                self.choice.apply_theme(ui)
            else:
                self.choice = InspectorChoice(self, textvariable=self.display,
                                              values=self.choice_labels, state='readonly', width=12, ui=ui)
                self.choice.bind('<<ComboboxSelected>>', lambda _: self._choose())
            self.choice.pack(fill='x', pady=(3, 0))
            self._widgets.append(self.choice)
        elif (descriptor['integer'] and descriptor['min'] == 0 and descriptor['max'] == 1
              and (checkbox or descriptor['name'].split('_')[-1].lower().startswith(('enable', 'use', 'disable'))
                   or descriptor['name'].endswith('uiCorrection'))):
            self.display = tk.BooleanVar(self)
            self.toggle = CheckToggle(header, label if left_toggle else '', self.display, command=self._toggle, ui=ui)
            self.toggle.pack(side='left' if left_toggle else 'right', padx=0 if left_toggle else (8, 0))
            self._widgets.append(self.toggle)
        else:
            self.number_text = tk.StringVar(self, value=self._format_numeric(variable.get()))
            self._input_trace = self.number_text.trace_add('write', self._input_changed)
            self.field = ChromeSpinbox(header, textvariable=self.number_text, width=6,
                                       from_=descriptor['min'], to=descriptor['max'],
                                       increment=clean_step(descriptor.get('step') or (1 if descriptor['integer'] else .01)),
                                       command=self._commit_input, ui=ui)
            self.field.pack(side='right', padx=(8, 0))
            self._widgets.append(self.field)
            for event in ('<Return>', '<KP_Enter>', '<FocusOut>'):
                self.field.bind(event, lambda _: self._commit_input())
            if descriptor['max'] > descriptor['min']:
                self.slider = AccentSlider(self, variable=variable, from_=descriptor['min'],
                                           to=descriptor['max'],
                                           resolution=clean_step(descriptor.get('step') or (1 if descriptor['integer'] else .01)),
                                           command=self._slide)
                self.slider.pack(fill='x')
        # Reserve the numeric/toggle width before giving the title the rest.
        if enabled_variable is None:
            if self.toggle is None or not left_toggle:
                self.label.pack(side='left', fill='x', expand=True)
        else:
            self.enable_display = tk.BooleanVar(self)
            self.enable_toggle = CheckToggle(header, label, self.enable_display,
                                             command=self._toggle_enabled, ui=ui)
            self.enable_toggle.pack(side='left')
            self._widgets.append(self.enable_toggle)
            self._enable_trace = enabled_variable.trace_add('write', self._enabled_changed)
            self._sync_enable()
        for widget in (self.enable_toggle, self.field, self.slider, self.toggle, self.choice):
            if widget is not None and (hint or range_hint):
                self._tooltips.append(Tooltip(widget, self.help_text))
        self._update_range_help()
        self.feedback = ttk.Label(self, style='Hint.TLabel', justify='left')
        self.feedback.bind('<Configure>', lambda e: self.feedback.configure(wraplength=max(40, e.width)))
        self._trace = variable.trace_add('write', self._changed)
        self._sync_display()
        self.apply_theme(ui)

    def _sync_enable(self):
        if self.enabled_variable is not None:
            try:
                self.enable_display.set(float(self.enabled_variable.get()) != 0)
            except (ValueError, tk.TclError):
                self.enable_display.set(False)
        self._set_control_states()

    def _enabled_changed(self, *_):
        self._sync_enable()
        self._change()

    def _toggle_enabled(self):
        if not self.enabled:
            self._sync_enable()
            return
        self.enabled_variable.set('1' if self.enable_display.get() else '0')
        # A switch-off is a commit even though the numeric controls are disabled.
        self.commit()

    def _input_enabled(self):
        return self.enabled and (self.enabled_variable is None or self.enable_display.get())

    def _set_control_states(self):
        editable = self._input_enabled()
        if self.slider is not None and not editable:
            self.slider._dragging = False
        for widget in (self.slider, self.field):
            if widget is not None:
                widget.configure(state='normal' if editable else 'disabled')
        if self.slider is not None:
            self.slider.configure(**ui_theme.slider_colors(self._ui, editable))
        for widget in (self.toggle, self.enable_toggle):
            if widget is not None:
                widget.configure(state='normal' if self.enabled else 'disabled')

    def value(self):
        try:
            value = parse_number(self.variable.get(), percent=self.allow_percent)
        except (ValueError, TypeError):
            raise ValueError(tr('parameter.invalid_number', name=self.title)) from None
        item = self.descriptor
        if not math.isfinite(value) or not item['min'] <= value <= self.maximum:
            raise ValueError(tr('realtime.editor.range_error', name=self.title,
                                minimum=item['min'], maximum=self.maximum)
                             + (' ' + tr('parameter.expand_hint') if self.maximum < item['max'] else ''))
        if item['integer'] and not value.is_integer():
            raise ValueError(tr('realtime.integer_required', name=self.title))
        if self.choices and value not in [v for v, _ in self.choices]:
            raise ValueError(tr('realtime.editor.choice_error', name=self.title))
        return int(value) if item['integer'] else value

    def _sync_display(self):
        self._syncing = True
        try:
            value = float(self.variable.get())
            if self.choice:
                found = next((label for (v, _), label in zip(self.choices, self.choice_labels) if v == value), None)
                self.display.set(found if found is not None else str(self.variable.get()))
            elif self.toggle:
                self.display.set(value != 0)
            elif self.field:
                text = self._format_numeric(self.variable.get())
                if self.number_text.get() != text:
                    self.number_text.set(text)
        except (ValueError, tk.TclError):
            if self.field:
                self.number_text.set(self.variable.get())
        finally:
            self._syncing = False

    def _input_changed(self, *_):
        if self._syncing:
            return
        # Keep unfinished keyboard edits in the draft without reformatting on
        # each keystroke. Collect/switch/save must see them even before blur.
        self._syncing = True
        try:
            self.variable.set(self.number_text.get())
        finally:
            self._syncing = False

    def _format_numeric(self, raw):
        return format_number(raw, self.descriptor.get('step') or (1 if self.descriptor['integer'] else .01))

    def _commit_input(self):
        if not self._input_enabled():
            return
        if self.variable.get() != self.number_text.get():
            self.variable.set(self.number_text.get())
        if self.allow_percent:
            try:
                value = str(self.value())
                if self.variable.get() != value:
                    self.variable.set(value)
            except (ValueError, tk.TclError):
                pass
        self.commit()

    def _changed(self, *_):
        if not self._syncing:
            self._sync_display()
        # Keep just the thumb and number hot while the pointer is down. Full
        # panel validation/layout (shared by both modes) runs once on release.
        if self.slider is not None and self.slider._dragging:
            self._drag_changed = True
            return
        self._drag_changed = False
        self._change()

    def _choose(self):
        if not self.enabled:
            self._sync_display()
            return
        if self.display.get() in self.choice_labels:
            index = self.choice_labels.index(self.display.get())
            self.variable.set(str(self.choices[index][0]))
            self.commit()

    def _toggle(self):
        if not self.enabled:
            self._sync_display()
            return
        self.variable.set('1' if self.display.get() else '0')
        self.commit()

    def _slide(self, *_):
        # AccentSlider emits on press, drag, release and keyboard. Commit only at
        # release (or a keyboard step), never send every pointer movement.
        if self._input_enabled() and not self.slider._dragging:
            if self._drag_changed:
                self._drag_changed = False
                self._change()  # Includes release at the same value as the last move.
            self.commit()

    def commit(self):
        if not self.enabled:
            return
        try:
            self.value()
        except (ValueError, tk.TclError):
            self._change()
            return
        self._commit()

    def set_feedback(self, *, reason='', nondefault=False, unapplied=False, error='', pending=False, restart=False):
        operation = feedback_state(reason=reason, error=error, pending=pending, unapplied=unapplied, restart=restart)
        state = (reason, nondefault, unapplied, error, pending)
        if state == self._feedback_state and operation == getattr(self, 'operation_state', None):
            return
        self.operation_state = operation
        self._feedback_state = state
        self.enabled = not reason
        self.disabled_reason = reason
        self.nondefault, self.unapplied = nondefault, unapplied
        self._set_control_states()
        if isinstance(self.choice, InspectorChips):
            self.choice.set_enabled(self.enabled)
        elif self.choice is not None:
            self.choice.configure(state='readonly' if self.enabled else 'disabled')
        if self.reset_button is not None:
            self.reset_button.configure(state='normal' if nondefault else 'disabled')
        labels = []
        if unapplied:
            labels.append(tr('parameter.state.restart' if operation == 'restart' else
                             'realtime.editor.syncing' if pending else 'realtime.editor.unapplied'))
        if reason:
            labels.append(reason)
        if error:
            labels.append(error)
        self.feedback.configure(text=' · '.join(labels))
        if labels:
            self.feedback.pack(fill='x', pady=(2, 0))
        else:
            self.feedback.pack_forget()
        self.label.configure(style='TLabel' if self.enabled else 'Hint.TLabel')

    def set_maximum(self, maximum):
        maximum = min(maximum, self.descriptor['max'])
        if maximum == self.maximum:
            return
        self.maximum = maximum
        if self.slider is not None:
            self.slider.configure(to=self.maximum)
        if self.field is not None:
            self.field.configure(to=self.maximum)
        self._update_range_help()

    def _update_range_help(self):
        if not self._range_hint:
            return
        self.help_text = self._base_help_text + '\n' + tr(
            'parameter.range_hint', minimum=self._format_numeric(self.descriptor['min']),
            maximum=self._format_numeric(self.maximum)) + '\n' + tr('parameter.range_scale_hint')
        for tooltip in self._tooltips:
            tooltip._hide()
            tooltip.text = self.help_text

    def apply_theme(self, ui):
        self._ui = dict(ui)
        for widget in self._widgets:
            widget.apply_theme(ui)
        if self.slider:
            self.slider.configure(**ui_theme.slider_colors(ui, self._input_enabled()))

    def destroy(self):
        for tooltip in self._tooltips:
            tooltip._hide()
        if self._enable_trace is not None:
            self.enabled_variable.trace_remove('write', self._enable_trace)
            self._enable_trace = None
        if self._input_trace is not None:
            self.number_text.trace_remove('write', self._input_trace)
            self._input_trace = None
        if self._trace is not None:
            self.variable.trace_remove('write', self._trace)
            self._trace = None
        super().destroy()
