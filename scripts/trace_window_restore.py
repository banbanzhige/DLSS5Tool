"""Read-only restore diagnosis in an isolated GUI process.

Baseline modes load Git's GUI and widget sources in memory. No source checkout,
installed application, real settings, or real queue is modified. An independent
process samples pixels so screen capture cannot hold the GUI's Python GIL.
"""
import argparse
from collections import Counter
import functools
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def observe(connection):
    import ctypes as c
    import numpy as np
    from PIL import ImageGrab
    user = c.WinDLL('user32')
    for name in ('IsIconic', 'IsWindowVisible'):
        fn = getattr(user, name)
        fn.argtypes, fn.restype = [c.c_void_p], c.c_int
    user.GetForegroundWindow.argtypes, user.GetForegroundWindow.restype = [], c.c_void_p
    connection.send('ready')
    while True:
        command = connection.recv()
        if command is None:
            break
        samples, errors, worst = [], [], None
        while time.perf_counter() < command['until']:
            hwnd = command['hwnd']
            if (user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd)
                    and user.GetForegroundWindow() == hwnd):
                try:
                    image = ImageGrab.grab(bbox=tuple(command['bbox']))
                    black = float(np.all(np.asarray(image)[:, :, :3] < 12, axis=2).mean())
                    samples.append({'ms': round((time.perf_counter()-command['start'])*1000, 3),
                                    'black': black})
                    if worst is None or black > worst[0]:
                        worst = black, image
                except OSError as error:
                    errors.append(str(error))
                    break
            time.sleep(.010)
        if worst is not None and worst[0] > .02:
            worst[1].save(command['image'])
        connection.send({'samples': samples, 'errors': errors,
                         'worst_black': worst[0] if worst else None})
    connection.close()


def load_head(name, revision):
    relative = name.replace('.', '/') + '.py'
    source = subprocess.check_output(['git', 'show', f'{revision}:{relative}'], cwd=ROOT)
    module = types.ModuleType(name)
    module.__file__ = f'git-{revision}/{relative}'
    module.__package__ = name.rpartition('.')[0]
    sys.modules[name] = module
    parent, _, child = name.rpartition('.')
    setattr(sys.modules[parent], child, module)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


class Trace:
    def __init__(self):
        self.cycle = None
        self.start = 0
        self.events = []
        self.counts = Counter()
        self.callback_depth = 0

    def mark(self, kind, **fields):
        if self.cycle is not None:
            self.events.append({'cycle': self.cycle, 'ms': round((time.perf_counter()-self.start)*1000, 3),
                                'kind': kind, **fields})

    def wrap(self, owner, name):
        original = getattr(owner, name, None)
        if original is None:
            return
        label = f'{owner.__name__}.{name}'

        @functools.wraps(original)
        def traced(*args, **kwargs):
            if self.cycle is None:
                return original(*args, **kwargs)
            self.counts[(self.cycle, label)] += 1
            self.mark('method_begin', method=label)
            began = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                self.mark('method_end', method=label, duration_ms=round((time.perf_counter()-began)*1000, 3))
        setattr(owner, name, traced)


def widgets(root):
    pending, result = [root], {}
    while pending:
        widget = pending.pop()
        result[str(widget)] = {'identity': id(widget), 'class': type(widget).__name__,
                               'mapped': widget.winfo_ismapped(), 'geometry': widget.winfo_geometry()}
        pending.extend(widget.winfo_children())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('head', 'head-no-caption', 'head-no-icon-map',
                                           'head-no-callbacks', 'head-quiet', 'head-extra-repaint', 'head-queue',
                                           'head-no-transitions', 'head-backing',
                                           'current', 'simple', 'simple-many', 'simple-nested',
                                           'simple-frames'), default='head')
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--screen', action='store_true')
    parser.add_argument('--theme', choices=('light', 'dark', 'alternate'), default='light')
    parser.add_argument('--maximized', action='store_true')
    parser.add_argument('--rapid', action='store_true')
    parser.add_argument('--native-minimize', action='store_true')
    parser.add_argument('--baseline-ref', default='e5e06dc',
                        help='Git revision for head modes; defaults to the pre-fix v3.1.3 baseline')
    args = parser.parse_args()
    from scripts.window_restore_probe import probe_output
    output = probe_output(parser, args.output)
    os.environ['DLSS5TOOL_SETTINGS_PATH'] = str(output / 'settings.json')
    os.environ['DLSS5TOOL_QUEUE_PATH'] = str(output / 'queue.json')
    os.environ['DLSS5TOOL_DISABLE_UPDATE_CHECK'] = '1'
    receiver = observer = None
    if args.screen:
        receiver, sender = mp.Pipe()
        observer = mp.get_context('spawn').Process(target=observe, args=(sender,))
        observer.start()
        sender.close()
        if receiver.recv() != 'ready':
            raise RuntimeError('Observer did not start')

    import tkinter as tk
    import dlss5tool
    from dlss5tool import app_settings, ui_theme
    ui_theme.enable_dpi_awareness()
    app_settings.save({'ui_theme': 'dark' if args.theme == 'dark' else 'light', 'inspector_width': 380})
    if args.mode.startswith('head'):
        ui_widgets = load_head('dlss5tool.ui_widgets', args.baseline_ref)
        gui = load_head('dlss5tool.gui', args.baseline_ref)
    else:
        from dlss5tool import ui_widgets, gui
    trace = Trace()
    for name in ('_apply_ui_theme', '_build_settings', '_draw_empty', '_apply_canvas_resize',
                 '_resize_preview_content', '_sync_preview_scrollregion', '_sync_export_scrollregion',
                 '_on_workspace_selected', '_on_canvas_configure', '_refresh_status_chips'):
        trace.wrap(gui.App, name)
    for name in ('ChromeButton', 'AccentSlider', 'ChipGroup', 'CheckToggle', 'StudioNotebook',
                 'TimelineBar', 'CollapsibleSection', 'ChromeField'):
        for method in ('_redraw', '_redraw_header', '_layout'):
            trace.wrap(getattr(ui_widgets, name), method)
    # A steady, empty window does not need GPU discovery for restore diagnosis.
    gui.App._start_render_gpu_scan = lambda self: None
    if args.mode in ('head-no-caption', 'head-quiet'):
        gui.App._install_caption_mode_button = lambda self: None
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *error: errors.append(str(error))
    original_call = tk.CallWrapper.__call__

    def callback(call, *values):
        target = call.func
        if getattr(target, '__qualname__', '') == 'Misc.after.<locals>.callit':
            cells = dict(zip(target.__code__.co_freevars, target.__closure__ or ()))
            if 'func' in cells:
                target = cells['func'].cell_contents
        name = getattr(target, '__qualname__', str(target))
        module = getattr(target, '__module__', '')
        if trace.cycle is None:
            return original_call(call, *values)
        trace.callback_depth += 1
        began = time.perf_counter()
        trace.mark('callback_begin', callback=name, module=module, depth=trace.callback_depth)
        try:
            if args.mode in ('head-no-callbacks', 'head-quiet') and module.startswith('dlss5tool.'):
                trace.mark('callback_skipped', callback=name)
                return None
            return original_call(call, *values)
        finally:
            trace.mark('callback_end', callback=name, depth=trace.callback_depth,
                       duration_ms=round((time.perf_counter()-began)*1000, 3))
            trace.callback_depth -= 1
    tk.CallWrapper.__call__ = callback

    if args.mode.startswith('simple'):
        app = None
        if args.mode == 'simple':
            canvas = tk.Canvas(root, background='#f0f2f5', highlightthickness=0)
            canvas.pack(fill='both', expand=True)
            canvas.create_rectangle(100, 100, 700, 500, fill='#fafbfc', outline='#dfe4ea')
            canvas.create_text(400, 300, text='Retained static interface', fill='#202b3a', font=('Segoe UI', 20))
        else:
            if args.mode == 'simple-nested':
                outer = tk.Canvas(root, background='#f0f2f5', highlightthickness=0)
                outer.pack(fill='both', expand=True)
                host = tk.Frame(outer, background='#f0f2f5')
                outer.create_window(0, 0, window=host, anchor='nw', width=1280, height=840)
            else:
                host = tk.Frame(root, background='#f0f2f5')
                host.pack(fill='both', expand=True)
            for row in range(9):
                host.rowconfigure(row, weight=1)
                for column in range(15):
                    host.columnconfigure(column, weight=1)
                    if args.mode == 'simple-frames':
                        from tkinter import ttk
                        cell_parent = ttk.Frame(host)
                        cell_parent.grid(row=row, column=column, sticky='nsew', padx=1, pady=1)
                        cell = tk.Canvas(cell_parent, background='#fafbfc', highlightthickness=0, width=50, height=30)
                        cell.pack(fill='both', expand=True)
                    else:
                        cell = tk.Canvas(host, background='#fafbfc', highlightthickness=0, width=50, height=30)
                        cell.grid(row=row, column=column, sticky='nsew', padx=1, pady=1)
                    cell.create_text(20, 20, text=str(row*15+column), fill='#202b3a')
    else:
        app = gui.App(root)
        if args.mode == 'head-queue':
            app.workspace_tabs.select(app.queue_tab)
    backing = getattr(app, '_window_backing_store', None) if app is not None else None
    if args.mode == 'head-backing':
        from dlss5tool.window_backing_store import WindowBackingStore
        backing = WindowBackingStore(root)
    root.title(f'Restore diagnosis [{args.mode}]')
    root.geometry('1280x840+20+20')
    if args.mode == 'head-no-transitions':
        import ctypes as c
        api = c.windll.dwmapi.DwmSetWindowAttribute
        api.argtypes = [c.c_void_p, c.c_uint, c.c_void_p, c.c_uint]
        api.restype = c.c_long
        value = c.c_int(1)
        root._trace_transition_hr = api(ui_theme._window_hwnd(root), 3, c.byref(value), c.sizeof(value))
    root.deiconify()
    if args.maximized:
        root.state('zoomed')

    def event(kind, value):
        trace.mark('tk_event', event=kind, widget=str(value.widget),
                   width=getattr(value, 'width', None), height=getattr(value, 'height', None),
                   x=getattr(value, 'x', None), y=getattr(value, 'y', None),
                   state=str(getattr(value, 'state', None)), count=getattr(value, 'count', None))
    for sequence in ('Map', 'Unmap', 'Configure', 'Expose', 'Visibility', 'Destroy'):
        root.bind(f'<{sequence}>', lambda value, kind=sequence: event(kind, value), add='+')

    if args.mode == 'head-extra-repaint':
        # Historical diagnostic only; the application no longer has this
        # forced redraw path or its temporary native snapshot overlay.
        def historical_repaint():
            import ctypes as c
            api = c.windll.user32.RedrawWindow
            api.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.c_uint]
            api.restype = c.c_int
            api(ui_theme._window_hwnd(root), None, None, 0x0181)
            pending = [root]
            while pending:
                widget = pending.pop()
                if widget.winfo_ismapped():
                    widget.event_generate('<Expose>', width=widget.winfo_width(),
                                          height=widget.winfo_height(), when='tail')
                    pending.extend(widget.winfo_children())
        def extra_map(value):
            if value.widget is root:
                root.after_idle(historical_repaint)
        root.bind('<Map>', extra_map, add='+')

    if args.mode == 'head-no-icon-map':
        ui_theme._apply_win32_icons = lambda *args: True
    case, reports = [0], []

    def start():
        root.focus_force()
        before = widgets(root)
        root._trace_before = before
        trace.cycle = case[0]
        trace.start = time.perf_counter()
        trace.mark('minimize_begin')
        if args.native_minimize:
            import ctypes as c
            post = c.windll.user32.PostMessageW
            post.argtypes, post.restype = [c.c_void_p, c.c_uint, c.c_size_t, c.c_ssize_t], c.c_int
            post(ui_theme._window_hwnd(root), 0x112, 0xF020, 0)
        else:
            root.iconify()
        trace.mark('minimize_return')
        root.after(30 if args.rapid else 300, restore)

    def restore():
        import ctypes as c
        before = root._trace_before
        t0 = time.perf_counter()
        trace.mark('restore_begin')
        x, y = root.winfo_rootx(), root.winfo_rooty()
        # Minimized coordinates are not useful. Use the known normal client
        # position recorded before iconify, outside the minimized state.
        x, y, width, height = root._trace_bbox
        if receiver is not None:
            receiver.send({'hwnd': ui_theme._window_hwnd(root),
                           'bbox': (x, y, x+width, y+height), 'start': t0,
                           'until': t0+.9, 'image': str(output / f'{case[0]}-worst.png')})
        root.deiconify()
        trace.mark('restore_return', duration_ms=round((time.perf_counter()-t0)*1000, 3))
        root.after_idle(lambda: trace.mark('first_idle'))
        root.after(1100, complete)

    def complete():
        if receiver is not None and not receiver.poll():
            root.after(20, complete)
            return
        trace.mark('cycle_complete')
        after = widgets(root)
        before = root._trace_before
        report = {'cycle': case[0], 'before_count': len(before), 'after_count': len(after),
                  'recreated_widgets': [name for name in before.keys() & after.keys()
                                        if before[name]['identity'] != after[name]['identity']],
                  'changed_geometry': [name for name in before.keys() & after.keys()
                                       if before[name]['geometry'] != after[name]['geometry']],
                  'screen': receiver.recv() if receiver is not None else None}
        reports.append(report)
        trace.cycle = None
        case[0] += 1
        if case[0] < args.cycles:
            root.after(300, prepare)
        else:
            finish()

    def prepare():
        if app is not None and args.theme == 'alternate':
            desired = 'light' if case[0] % 2 == 0 else 'dark'
            if app._ui_theme_name != desired:
                app._apply_ui_theme(desired, persist=False)
                root.after(300, begin_prepared)
                return
        begin_prepared()

    def begin_prepared():
        root._trace_bbox = (root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height())
        start()

    def finish():
        payload = {'mode': args.mode, 'tk': root.tk.call('package', 'require', 'Tk'),
                   'baseline_ref': args.baseline_ref if args.mode.startswith('head') else None,
                   'transition_hr': getattr(root, '_trace_transition_hr', None),
                   'backing': ({'captures': backing.captures, 'replays': backing.replays,
                                'rejected': backing.rejected, 'error': backing.error,
                                'method': backing.capture_method,
                                'last_capture_ms': backing.last_capture_ms,
                                'cached_bytes': backing.frame.width*backing.frame.height*4 if backing.frame else 0}
                               if backing is not None else None),
                   'errors': errors, 'cycles': reports, 'events': trace.events,
                   'method_counts': [{'cycle': cycle, 'method': method, 'count': count}
                                     for (cycle, method), count in trace.counts.items()]}
        (output / 'trace.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
        print(json.dumps({'mode': args.mode, 'errors': errors[:3], 'error_count': len(errors), 'events': len(trace.events),
                          'backing': payload['backing'],
                          'cycles': [{key: value for key, value in item.items() if key != 'screen'}
                                     for item in reports], 'methods': payload['method_counts']}))
        if receiver is not None:
            receiver.send(None)
            receiver.close()
            observer.join(timeout=3)
        tk.CallWrapper.__call__ = original_call
        if backing is not None:
            if backing.frame is not None:
                from PIL import Image
                Image.fromarray(backing.frame.pixels()[:, :, :3][:, :, ::-1]).save(output / 'cached-frame.png')
            backing.close()
        if app is not None:
            app._on_close()
        else:
            root.destroy()

    root.after(1200, prepare)
    root.mainloop()
    if errors:
        raise SystemExit('Diagnostic callback error')


if __name__ == '__main__':
    mp.freeze_support()
    main()
