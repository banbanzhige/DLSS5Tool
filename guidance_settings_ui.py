"""Quality-first depth/flow inspector, using the existing desktop controls."""
import tkinter as tk
from tkinter import ttk

from guidance_parameters import NUMERIC, parameters
from i18n import tr
from ui_widgets import CollapsibleSection, Tooltip


def build_guidance_settings(app, parent):
    d = app._host_settings
    saved = parameters(app._saved_settings)
    d['analysis_vars'] = {key: tk.StringVar(value=str(value)) for key, value in saved.items()
                          if key in NUMERIC}
    d['v_depth_palette'] = tk.StringVar(value=tr('guidance.option.' + saved['guidance_depth_palette']))
    d['v_depth_invert'] = tk.StringVar(value=tr('guidance.option.' + ('inverted' if saved['guidance_depth_invert'] else 'normal')))
    d['analysis_valid'] = dict(saved)
    body = ttk.Frame(parent, style='Panel.TFrame')
    body.pack(fill='x', padx=16, pady=(12, 8))
    app._guidance_settings_frame = body
    d['guidance_controls'] = {}

    def hint(frame, key, row=None):
        label = ttk.Label(frame, text=tr(key), style='Hint.TLabel', wraplength=280, justify='left')
        if row is None:
            label.pack(fill='x', pady=(4, 6))
        else:
            label.grid(row=row, column=0, columnspan=2, sticky='ew', pady=(4, 6))
        label.bind('<Configure>', lambda e: label.configure(wraplength=max(100, e.width)), add='+')
        return label

    def group(title, collapse=False, settings_page=False):
        if collapse:
            section = CollapsibleSection(app._export_inner if settings_page else body,
                                         tr(title), collapsed=True, ui=app._ui)
            section.pack(fill='x', padx=16 if settings_page else 0, pady=(10, 0),
                         **({'before': app._host_section} if settings_page else {}))
            app._theme_widgets.append(section)
            frame = section.body
        else:
            ttk.Label(body, text=tr(title), style='Kicker.TLabel').pack(fill='x', pady=(12, 6))
            frame = ttk.Frame(body, style='Panel.TFrame')
            frame.pack(fill='x')
            section = None
        frame.columnconfigure(1, weight=1)
        return frame, section

    def combo(frame, row, key, label, variable, choices):
        field = ttk.Frame(frame, style='Panel.TFrame')
        field.grid(row=row, column=0, columnspan=2, sticky='ew', pady=3)
        ttk.Label(field, text=tr(label)).pack(anchor='w', pady=(0, 3))
        widget = app._chrome_combo(field, d[variable], choices)
        widget.pack(fill='x')
        widget.bind('<<ComboboxSelected>>', lambda e: app._on_mod_settings_change())
        d['guidance_controls'][key] = widget
        return widget

    def number(frame, row, key, label, step):
        default, low, high, kind = NUMERIC[key]
        variable = d['analysis_vars'][key]
        caption = ttk.Label(frame, text=tr(label), wraplength=130, justify='left')
        caption.grid(row=row, column=0, sticky='w', padx=(0, 8), pady=3)
        frame.columnconfigure(1, minsize=100)
        frame.bind('<Configure>', lambda e: caption.configure(wraplength=max(75, e.width - 125)), add='+')
        error = ttk.Label(frame, text='', style='Hint.TLabel', wraplength=250)
        def commit(event=None):
            try:
                value = parameters({key: variable.get()}, strict=True)[key]
            except ValueError:
                variable.set(str(d['analysis_valid'][key]))
                error.configure(text=tr('guidance.input_error', low=low, high=high))
                error.grid(row=row + 1, column=0, columnspan=2, sticky='ew')
                return
            error.grid_remove()
            variable.set(str(value))
            d['analysis_valid'][key] = value
            app._on_mod_settings_change()
        widget = app._chrome_spin(frame, from_=low, to=high, increment=step,
                                 textvariable=variable, width=7, command=commit)
        widget.grid(row=row, column=1, sticky='ew', pady=3)
        widget.bind('<Return>', commit)
        widget.bind('<FocusOut>', commit)
        if key.endswith('_edge'):
            Tooltip(widget, tr('guidance.edge_hint'))
        d['guidance_controls'][key] = widget
        return widget

    mode = ttk.Frame(body, style='Panel.TFrame')
    mode.pack(fill='x')
    mode.columnconfigure(1, weight=1)
    d['w_guidance'] = combo(mode, 0, 'mode', 'guidance.mode', 'v_guidance',
                            [tr('guidance.mode.' + str(i)) for i in range(4)])
    app._settings['w_guidance'] = d['w_guidance']
    status = ttk.Frame(body, style='Panel.TFrame')
    status.pack(fill='x', pady=(4, 0))
    status.columnconfigure(0, weight=1)
    d['w_guidance_status'] = ttk.Label(status, text='', style='Hint.TLabel', wraplength=210)
    d['w_guidance_status'].grid(row=0, column=0, sticky='ew')
    d['w_guidance_status'].bind('<Configure>', lambda e: d['w_guidance_status'].configure(wraplength=max(90, e.width)), add='+')
    app._chrome_button(status, text=tr('mods.details'), command=app._show_module_details,
                       variant='ghost', width=64).grid(row=0, column=1)
    hint(body, 'guidance.page_hint')

    flow, _ = group('guidance.flow_section')
    ttk.Label(flow, text='RAFT-Large', style='Hint.TLabel').grid(row=0, column=0, columnspan=2, sticky='w')
    number(flow, 2, 'guidance_flow_edge', 'guidance.analysis_edge', 64)
    iterations = number(flow, 4, 'guidance_flow_updates', 'guidance.iterations', 1)
    Tooltip(iterations, tr('guidance.iterations_hint'))
    flow_advanced, app._guidance_flow_advanced = group('guidance.flow_advanced', True)
    combo(flow_advanced, 0, 'flow', 'guidance.direction', 'v_flow_direction',
          [tr('guidance.option.' + value) for value in ('backward', 'forward_negated')])
    hint(flow_advanced, 'guidance.direction_hint', 1)

    depth, _ = group('guidance.depth_section')
    combo(depth, 0, 'depth', 'guidance.encoder', 'v_depth_encoder',
          [tr('guidance.option.' + value) for value in ('auto', 'vits', 'vitb', 'vitl')])
    number(depth, 2, 'guidance_depth_edge', 'guidance.analysis_edge', 64)
    depth_advanced, app._guidance_depth_advanced = group('guidance.depth_advanced', True)
    number(depth_advanced, 0, 'guidance_depth_smoothing', 'guidance.range_stability', 0.05)
    hint(depth_advanced, 'guidance.depth_hint', 2)
    number(depth_advanced, 3, 'guidance_depth_low', 'guidance.percentile_low', 0.5)
    number(depth_advanced, 5, 'guidance_depth_high', 'guidance.percentile_high', 0.5)
    hint(depth_advanced, 'guidance.percentile_hint', 7)

    display, app._guidance_display_section = group('guidance.display_section', True)
    number(display, 0, 'guidance_flow_range', 'guidance.display_range', 4)
    combo(display, 2, 'palette', 'guidance.display_depth', 'v_depth_palette',
          [tr('guidance.option.' + value) for value in ('gray', 'turbo')])
    combo(display, 3, 'invert', 'guidance.display_polarity', 'v_depth_invert',
          [tr('guidance.option.' + value) for value in ('normal', 'inverted')])
    hint(display, 'guidance.display_hint', 4)
    hint(display, 'guidance.direction_legend', 5)

    performance, app._guidance_advanced = group('guidance.advanced', True, settings_page=True)
    combo(performance, 0, 'device', 'guidance.device', 'v_guidance_device',
          [tr('guidance.option.' + value) for value in ('auto', 'cuda', 'cpu')])
    profile = combo(performance, 1, 'profile', 'guidance.depth_profile', 'v_depth_profile',
                    [tr('guidance.option.' + value) for value in ('fp32', 'sdpa_fp16')])
    Tooltip(profile, tr('guidance.depth_profile_hint'))
    hint(performance, 'guidance.depth_profile_short', 2)
    combo(performance, 3, 'execution', 'guidance.execution', 'v_guidance_execution',
          [tr('guidance.option.' + value) for value in ('serial', 'raft_streams')])
    hint(performance, 'guidance.execution_short', 4)
