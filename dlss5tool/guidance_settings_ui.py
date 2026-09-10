"""Quality-first depth/flow inspector, using the existing desktop controls."""
import tkinter as tk
from tkinter import ttk

from dlss5tool.guidance_parameters import NUMERIC, parameters
from dlss5tool.guidance_public import depth_enabled, public_modes
from dlss5tool.i18n import tr
from dlss5tool.ui_widgets import CollapsibleSection, Tooltip


def build_guidance_settings(app, parent):
    d = app._host_settings
    saved = parameters(app._saved_settings)
    d['analysis_vars'] = {key: tk.StringVar(value=str(value)) for key, value in saved.items()
                          if key in NUMERIC}
    d['v_depth_palette'] = tk.StringVar(value=tr('guidance.option.' + saved['guidance_depth_palette']))
    d['v_depth_invert'] = tk.StringVar(value=tr('guidance.option.' + ('inverted' if saved['guidance_depth_invert'] else 'normal')))
    d['analysis_valid'] = dict(saved)
    d['v_flow_backend'] = tk.StringVar(value=tr('guidance.option.' + app._saved_settings.get('guidance_flow_backend', 'raft')))
    saved_grid = app._saved_settings.get('guidance_flow_grid', 4)
    saved_grid = saved_grid if saved_grid in (1, 2, 4) else 4
    d['v_flow_grid'] = tk.StringVar(value=tr('guidance.option.grid_' + str(saved_grid)))
    d['flow_rows_raft'] = []
    d['flow_rows_nvofa'] = []
    body = ttk.Frame(parent, style='Panel.TFrame')
    body.pack(fill='x', padx=16, pady=(12, 8))
    app._guidance_settings_frame = body
    d['guidance_controls'] = {}
    d['guidance_tooltips'] = {}

    def help_for(key, *messages):
        widget = d['guidance_controls'][key]
        text = '\n\n'.join(tr(message) for message in messages)
        d['guidance_tooltips'][key] = Tooltip(widget, text)

    def group(title, collapse=False, settings_page=False):
        if collapse:
            section = CollapsibleSection(app._export_inner if settings_page else body,
                                         tr(title), collapsed=True, ui=app._ui)
            section.pack(fill='x', padx=16 if settings_page else 0, pady=(10, 0),
                         **({'before': app._host_section} if settings_page else {}))
            app._theme_widgets.append(section)
            frame = section.body
        else:
            caption = ttk.Label(body, text=tr(title), style='Kicker.TLabel')
            caption.pack(fill='x', pady=(12, 6))
            frame = ttk.Frame(body, style='Panel.TFrame')
            frame.pack(fill='x')
            section = None
        frame.columnconfigure(1, weight=1)
        if title.startswith('guidance.depth_') and not depth_enabled():
            if section is not None:
                section.pack_forget()
            else:
                caption.pack_forget()
                frame.pack_forget()
        return frame, section

    def combo(frame, row, key, label, variable, choices):
        field = ttk.Frame(frame, style='Panel.TFrame')
        field.grid(row=row, column=0, columnspan=2, sticky='ew', pady=3)
        heading = ttk.Frame(field, style='Panel.TFrame')
        heading.pack(fill='x', pady=(0, 3))
        ttk.Label(heading, text=tr(label)).pack(side='left')
        widget = app._chrome_combo(field, d[variable], choices)
        widget.pack(fill='x')
        widget.bind('<<ComboboxSelected>>', lambda e: app._on_mod_settings_change())
        d['guidance_controls'][key] = widget
        return widget

    def number(frame, row, key, label, step):
        default, low, high, kind = NUMERIC[key]
        variable = d['analysis_vars'][key]
        container = ttk.Frame(frame, style='Panel.TFrame')
        container.grid(row=row, column=0, columnspan=2, sticky='ew', pady=3)
        container.columnconfigure(1, weight=1, minsize=100)
        caption = ttk.Label(container, text=tr(label), wraplength=130, justify='left')
        caption.grid(row=0, column=0, sticky='w', padx=(0, 8))
        container.bind('<Configure>', lambda e: caption.configure(wraplength=max(75, e.width - 125)), add='+')
        error = ttk.Label(container, text='', style='Hint.TLabel', wraplength=250)
        def commit(event=None):
            try:
                value = parameters({key: variable.get()}, strict=True)[key]
            except ValueError:
                variable.set(str(d['analysis_valid'][key]))
                error.configure(text=tr('guidance.input_error', low=low, high=high))
                error.grid(row=1, column=0, columnspan=2, sticky='ew')
                return
            error.grid_remove()
            variable.set(str(value))
            d['analysis_valid'][key] = value
            app._on_mod_settings_change()
        widget = app._chrome_spin(container, from_=low, to=high, increment=step,
                                 textvariable=variable, width=7, command=commit)
        widget.grid(row=0, column=1, sticky='ew')
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
                            [tr('guidance.mode.' + str(i)) for i in public_modes()])
    app._settings['w_guidance'] = d['w_guidance']
    status = ttk.Frame(body, style='Panel.TFrame')
    status.pack(fill='x', pady=(4, 0))
    status.columnconfigure(0, weight=1)
    d['w_guidance_status'] = ttk.Label(status, text='', style='Hint.TLabel', width=1, anchor='w')
    d['w_guidance_status'].grid(row=0, column=0, sticky='ew')
    d['guidance_status_tooltip'] = Tooltip(d['w_guidance_status'], '')
    app._chrome_button(status, text=tr('mods.details'), command=app._show_module_details,
                       variant='ghost', width=64).grid(row=0, column=1)
    help_for('mode', 'guidance.page_hint' if depth_enabled() else 'guidance.flow_page_hint')

    flow, _ = group('guidance.flow_section')
    combo(flow, 0, 'flow_backend', 'guidance.flow_backend', 'v_flow_backend',
          [tr('guidance.option.' + value) for value in ('raft', 'nvofa')])
    help_for('flow_backend', 'guidance.nvofa_hint')
    number(flow, 2, 'guidance_flow_edge', 'guidance.analysis_edge', 64)
    iterations = number(flow, 4, 'guidance_flow_updates', 'guidance.iterations', 1)
    help_for('guidance_flow_updates', 'guidance.iterations_hint')
    d['flow_rows_raft'].append(iterations.master)
    combo(flow, 6, 'flow_grid', 'guidance.flow_grid', 'v_flow_grid',
          [tr('guidance.option.grid_' + size) for size in ('4', '2', '1')])
    d['flow_rows_nvofa'].append(d['guidance_controls']['flow_grid'].master)
    help_for('flow_grid', 'guidance.grid_hint')
    flow_advanced, app._guidance_flow_advanced = group('guidance.flow_advanced', True)
    combo(flow_advanced, 0, 'flow', 'guidance.direction', 'v_flow_direction',
          [tr('guidance.option.' + value) for value in ('backward', 'forward_negated')])
    help_for('flow', 'guidance.direction_hint')

    depth, _ = group('guidance.depth_section')
    combo(depth, 0, 'depth', 'guidance.encoder', 'v_depth_encoder',
          [tr('guidance.option.' + value) for value in ('auto', 'vits', 'vitb', 'vitl')])
    number(depth, 2, 'guidance_depth_edge', 'guidance.analysis_edge', 64)
    depth_advanced, app._guidance_depth_advanced = group('guidance.depth_advanced', True)
    number(depth_advanced, 0, 'guidance_depth_smoothing', 'guidance.range_stability', 0.05)
    help_for('guidance_depth_smoothing', 'guidance.depth_hint')
    number(depth_advanced, 3, 'guidance_depth_low', 'guidance.percentile_low', 0.5)
    number(depth_advanced, 5, 'guidance_depth_high', 'guidance.percentile_high', 0.5)
    help_for('guidance_depth_low', 'guidance.percentile_hint')

    display, app._guidance_display_section = group('guidance.display_section', True)
    number(display, 0, 'guidance_flow_range', 'guidance.display_range', 4)
    combo(display, 2, 'palette', 'guidance.display_depth', 'v_depth_palette',
          [tr('guidance.option.' + value) for value in ('gray', 'turbo')])
    combo(display, 3, 'invert', 'guidance.display_polarity', 'v_depth_invert',
          [tr('guidance.option.' + value) for value in ('normal', 'inverted')])
    help_for('guidance_flow_range', 'guidance.display_hint', 'guidance.direction_legend')

    performance, app._guidance_advanced = group('guidance.advanced', True, settings_page=True)
    combo(performance, 0, 'device', 'guidance.device', 'v_guidance_device',
          [tr('guidance.option.' + value) for value in ('auto', 'cuda', 'cpu')])
    profile = combo(performance, 1, 'profile', 'guidance.depth_profile', 'v_depth_profile',
                    [tr('guidance.option.' + value) for value in ('fp32', 'sdpa_fp16')])
    help_for('profile', 'guidance.depth_profile_hint')
    combo(performance, 3, 'execution', 'guidance.execution', 'v_guidance_execution',
          [tr('guidance.option.' + value) for value in ('serial', 'raft_streams')])
    help_for('execution', 'guidance.execution_short')
    if not depth_enabled():
        for key in ('palette', 'invert', 'profile', 'execution'):
            d['guidance_controls'][key].master.grid_remove()
        for row in (2, 4):
            for child in performance.grid_slaves(row=row):
                child.grid_remove()
    sync_flow_backend_controls(app)


def sync_flow_backend_controls(app):
    d = getattr(app, '_host_settings', None)
    if not d or 'flow_rows_raft' not in d:
        return
    nvofa = d['v_flow_backend'].get() == tr('guidance.option.nvofa')
    for widget in d['flow_rows_raft']:
        widget.grid_remove() if nvofa else widget.grid()
    for widget in d['flow_rows_nvofa']:
        widget.grid() if nvofa else widget.grid_remove()
