"""Modal importer. Detection and image validation run off the Tk thread."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk

from dlss5tool import paths, ui_theme
from dlss5tool.i18n import tr
from dlss5tool.image_sequence import COLOR_PROFILES, ImageSequence, parse_rate, propose
from dlss5tool.ui_widgets import CheckToggle, ChromeButton, ChromeCombobox, ChromeEntry


def _dialog_chrome(parent):
    """Match the open studio theme, including tests that only paint the root."""
    ui = getattr(parent, '_dlss_ui', None)
    if not isinstance(ui, dict) or 'panel' not in ui:
        try:
            background = str(parent.cget('bg')).lower()
        except tk.TclError:
            background = ''
        ui = ui_theme.tokens('light')
        for tokens in ui_theme.THEMES.values():
            if str(tokens.get('bg', '')).lower() == background:
                ui = tokens
                break
    try:
        styled = bool(ttk.Style(parent).lookup('Chrome.TEntry', 'bordercolor'))
    except tk.TclError:
        styled = False
    if styled:
        ui_theme.configure_fonts(parent)
    else:
        ui_theme.apply_ttk(parent, ui)
    return ui, ui.get('name') == 'dark'


_RATE_FILE = 'image-sequence-rate.txt'
_HDR_PROFILES = tuple(profile for profile in COLOR_PROFILES if profile != 'srgb')
_LIST_HEIGHT = 240


def remembered_rate():
    try:
        return str(parse_rate(paths.state_path(_RATE_FILE).read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return '24'


def remember_rate(rate):
    target = paths.state_path(_RATE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(parse_rate(rate)), encoding='utf-8')


def _place_over_parent(window, parent):
    """Center on the owner window, or on the desktop when the owner is hidden."""
    window.update_idletasks()
    width = max(window.winfo_reqwidth(), window.winfo_width(), 1)
    height = max(window.winfo_reqheight(), window.winfo_height(), 1)
    try:
        visible = bool(parent.winfo_viewable())
    except tk.TclError:
        visible = False
    if visible:
        origin_x = parent.winfo_rootx()
        origin_y = parent.winfo_rooty()
        bounds_w = max(parent.winfo_width(), 1)
        bounds_h = max(parent.winfo_height(), 1)
    else:
        origin_x = window.winfo_vrootx()
        origin_y = window.winfo_vrooty()
        bounds_w = max(window.winfo_vrootwidth(), 1)
        bounds_h = max(window.winfo_vrootheight(), 1)
    x = origin_x + max(0, (bounds_w - width) // 2)
    y = origin_y + max(0, (bounds_h - height) // 2)
    left = window.winfo_vrootx()
    top = window.winfo_vrooty()
    right = left + max(window.winfo_vrootwidth(), width)
    bottom = top + max(window.winfo_vrootheight(), height)
    x = min(max(x, left), right - width)
    y = min(max(y, top), bottom - height)
    window.geometry(f'+{int(x)}+{int(y)}')


def ask_sequence(parent, selected):
    """Return saved sequence records for the checked groups, or an empty list."""
    if isinstance(selected, (str, Path)):
        chosen = [selected] if str(selected).strip() else []
    elif selected is None:
        chosen = []
    else:
        chosen = [item for item in selected if str(item).strip()]
    if not chosen:
        return []
    ui, dark = _dialog_chrome(parent)
    panel = ui['panel']
    window = tk.Toplevel(parent)
    window.withdraw()
    window.title(tr('sequence.import'))
    window.configure(bg=panel)
    window.transient(parent)
    window.resizable(False, False)
    ui_theme.apply_app_icon(window)
    body = ttk.Frame(window, padding=16)
    body.pack(fill='both', expand=True)
    status = tk.StringVar(value=tr('sequence.identifying'))
    status_label = ttk.Label(body, textvariable=status, wraplength=460, justify='left')
    status_label.pack(anchor='w')
    list_host = ttk.Frame(body)
    list_host.pack(fill='x', pady=(8, 0))
    canvas = tk.Canvas(list_host, height=1, highlightthickness=0, borderwidth=0, bg=panel)
    scrollbar = ttk.Scrollbar(list_host, command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side='left', fill='both', expand=True)
    list_inner = ttk.Frame(canvas)
    list_window = canvas.create_window((0, 0), window=list_inner, anchor='nw')
    settings = ttk.Frame(body)
    settings.pack(fill='x', pady=(12, 0))
    rate_row = ttk.Frame(settings)
    rate_row.pack(fill='x')
    ttk.Label(rate_row, text=tr('sequence.rate')).pack(side='left')
    rate = tk.StringVar(value=remembered_rate())
    rate_field = ChromeEntry(rate_row, ui=ui, textvariable=rate, width=16)
    rate_field.pack(side='left', padx=(8, 0))
    entry = rate_field.entry
    color_row = ttk.Frame(settings)
    ttk.Label(color_row, text=tr('sequence.color')).pack(side='left')
    hdr_labels = [tr('sequence.color_' + name) for name in _HDR_PROFILES]
    color_field = ChromeCombobox(
        color_row, ui=ui, state='readonly', width=36, values=hdr_labels,
    )
    color_field.pack(side='left', padx=(8, 0))
    color_box = color_field.combo
    color_box.set('')
    hint = ttk.Label(
        settings, text=tr('sequence.hdr_choose'), wraplength=460, justify='left',
        style='Hint.TLabel',
    )
    note = ttk.Label(
        settings, text=tr('sequence.hdr_note'), wraplength=460, justify='left',
        style='Hint.TLabel',
    )
    bar = ttk.Progressbar(body, length=460, maximum=100)
    ttk.Separator(body, orient='horizontal').pack(fill='x', pady=(14, 0))
    actions = ttk.Frame(body)
    actions.pack(fill='x', pady=(12, 0))
    cancel = threading.Event()
    result = []
    events = queue.Queue()
    rows = []
    details = []
    worker = None
    closing = False
    running = False
    ready = False
    accepted_rate = None
    syncing = False

    def check_cancel():
        if cancel.is_set():
            raise InterruptedError()

    def wheel(event):
        if list_inner.winfo_reqheight() > canvas.winfo_height():
            canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def bind_wheel(widget):
        widget.bind('<MouseWheel>', wheel)
        for child in widget.winfo_children():
            bind_wheel(child)

    def sync_list(_event=None):
        nonlocal syncing
        if syncing or not canvas.winfo_exists():
            return
        syncing = True
        try:
            canvas.update_idletasks()
            bbox = canvas.bbox('all')
            canvas.configure(scrollregion=bbox if bbox else (0, 0, 0, 0))
            height = min(_LIST_HEIGHT, max(list_inner.winfo_reqheight(), 1))
            if int(float(canvas.cget('height'))) != height:
                canvas.configure(height=height)
            overflow = list_inner.winfo_reqheight() > _LIST_HEIGHT
            if overflow and not scrollbar.winfo_ismapped():
                scrollbar.pack(side='right', fill='y')
            elif not overflow and scrollbar.winfo_ismapped():
                scrollbar.pack_forget()
            width = max(list_host.winfo_width(), 460)
            if scrollbar.winfo_ismapped():
                width -= scrollbar.winfo_width()
            canvas.itemconfigure(list_window, width=max(width, 1))
        finally:
            syncing = False

    def set_status(text):
        status.set(text)
        if text:
            if not status_label.winfo_manager():
                status_label.pack(anchor='w', before=list_host)
        elif status_label.winfo_manager():
            status_label.pack_forget()

    def refresh_details(*_args):
        for group, label in details:
            label.configure(text=group.detail(rate.get()))

    def refresh_color(_event=None):
        hdr = any(variable.get() and group.importable and group.kind == 'hdr' for group, variable in rows)
        if not hdr:
            for widget in (color_row, hint, note):
                if widget.winfo_ismapped():
                    widget.pack_forget()
            return
        if not color_row.winfo_ismapped():
            color_row.pack(fill='x', pady=(8, 0))
        if not hint.winfo_ismapped():
            hint.pack(anchor='w', pady=(4, 0))
        if color_box.get().strip() in hdr_labels:
            if not note.winfo_ismapped():
                note.pack(anchor='w', pady=(4, 0))
        elif note.winfo_ismapped():
            note.pack_forget()

    def add_row(group, use_check):
        row = ttk.Frame(list_inner)
        row.pack(fill='x', pady=(0, 8))
        variable = tk.BooleanVar(value=group.importable and not group.recommended)
        if use_check:
            toggle = CheckToggle(row, '', variable, command=refresh_color, ui=ui)
            toggle.pack(side='left', anchor='n', padx=(0, 8))
        text = ttk.Frame(row)
        text.pack(side='left', fill='x', expand=True)
        title = ttk.Label(text, text=group.display_name(), wraplength=400, justify='left')
        title.pack(anchor='w')
        detail = ttk.Label(
            text, text=group.detail(rate.get()), wraplength=400, justify='left',
            style='Hint.TLabel',
        )
        detail.pack(anchor='w')
        details.append((group, detail))
        if use_check:
            def toggle_row(_event=None, item=variable):
                item.set(not item.get())
                refresh_color()
            title.bind('<Button-1>', toggle_row)
            detail.bind('<Button-1>', toggle_row)
        rows.append((group, variable))

    def show_groups(groups):
        nonlocal ready
        for child in list_inner.winfo_children():
            child.destroy()
        rows.clear()
        details.clear()
        plain = len(groups) == 1 and not groups[0].recommended
        if plain:
            add_row(groups[0], False)
        else:
            sections = (
                (tr('sequence.section_import'), [group for group in groups if not group.recommended]),
                (tr('sequence.section_other'), [group for group in groups if group.recommended]),
            )
            for title, items in sections:
                if not items:
                    continue
                ttk.Label(list_inner, text=title, style='Kicker.TLabel').pack(anchor='w', pady=(2, 4))
                for group in items:
                    add_row(group, group.importable)
        refresh_color()
        refresh_details()
        bind_wheel(canvas)
        ready = True
        set_status('')
        submit.config(state='normal')
        window.update_idletasks()
        sync_list()
        _place_over_parent(window, parent)

    def close():
        nonlocal closing
        if closing:
            return
        closing = True
        cancel.set()
        if worker is None or not worker.is_alive():
            window.destroy()
            return
        set_status(tr('sequence.cancelling'))
        cancel_button.config(state='disabled')
        submit.config(state='disabled')

    def poll():
        nonlocal running
        if not window.winfo_exists():
            return
        if closing:
            if worker is None or not worker.is_alive():
                window.destroy()
                return
            window.after(50, poll)
            return
        while True:
            try:
                kind, payload = events.get_nowait()
            except queue.Empty:
                break
            if kind == 'groups':
                show_groups(payload)
                return
            if kind == 'progress':
                done, total = payload
                set_status(tr('sequence.scanning', done=done, total=total))
                bar['value'] = done / total * 100 if total else 0
            elif kind == 'plan_error':
                set_status(str(payload))
                return
            elif kind == 'error':
                running = False
                set_status(str(payload))
                submit.config(state='normal')
                rate_field.config(state='normal')
                color_field.config(state='readonly')
                return
            elif kind == 'done':
                try:
                    saved = [item.save(paths.state_path('image-sequences')) for item in payload]
                    try:
                        remember_rate(accepted_rate)
                    except OSError:
                        pass
                    result.extend(saved)
                except Exception as error:
                    running = False
                    set_status(str(error))
                    submit.config(state='normal')
                    rate_field.config(state='normal')
                    color_field.config(state='readonly')
                    return
                window.destroy()
                return
        window.after(50, poll)

    def start(_event=None):
        nonlocal worker, running, accepted_rate
        if running or closing or not ready:
            return
        try:
            fps = parse_rate(rate.get())
        except ValueError as error:
            set_status(str(error))
            entry.focus_set()
            return
        chosen_groups = [group for group, variable in rows if variable.get() and group.importable]
        if not chosen_groups:
            set_status(tr('sequence.none_checked'))
            return
        hdr_profile = None
        if any(group.kind == 'hdr' for group in chosen_groups):
            try:
                hdr_profile = _HDR_PROFILES[hdr_labels.index(color_box.get().strip())]
            except ValueError:
                set_status(tr('sequence.hdr_choose'))
                refresh_color()
                color_box.focus_set()
                return
        submit.config(state='disabled')
        rate_field.config(state='disabled')
        color_field.config(state='disabled')
        running = True
        accepted_rate = fps
        if not bar.winfo_ismapped():
            bar.pack(fill='x', pady=(8, 0), before=actions)
        total = sum(len(group.files) for group in chosen_groups)
        set_status(tr('sequence.scanning', done=0, total=total))

        def scan():
            try:
                sequences = []
                offset = 0
                for group in chosen_groups:
                    profile = hdr_profile if group.kind == 'hdr' else 'srgb'
                    def progress(done, _group_total, start=offset):
                        if done == _group_total or done % 25 == 0:
                            events.put(('progress', (start + done, total)))
                    sequences.append(ImageSequence.scan_files(
                        group.files, fps, check_cancel, progress, color_profile=profile))
                    offset += len(group.files)
                events.put(('done', sequences))
            except InterruptedError:
                events.put(('stop', None))
            except Exception as error:
                events.put(('error', error))

        worker = threading.Thread(target=scan, name='image-sequence-scan', daemon=True)
        worker.start()
        window.after(50, poll)

    def identify():
        try:
            events.put(('groups', propose(chosen, check_cancel)))
        except InterruptedError:
            events.put(('stop', None))
        except Exception as error:
            events.put(('plan_error', error))

    rate.trace_add('write', refresh_details)
    color_box.bind('<<ComboboxSelected>>', refresh_color)
    list_inner.bind('<Configure>', sync_list)
    canvas.bind('<Configure>', sync_list)
    bind_wheel(canvas)
    submit = ChromeButton(
        actions, text=tr('sequence.confirm'), command=start, ui=ui, variant='accent',
    )
    submit.pack(side='right')
    submit.config(state='disabled')
    cancel_button = ChromeButton(
        actions, text=tr('sequence.cancel'), command=close, ui=ui, variant='ghost',
    )
    cancel_button.pack(side='right', padx=(0, 8))
    window.protocol('WM_DELETE_WINDOW', close)
    window.bind('<Escape>', lambda _event: close())
    window.bind('<Return>', start)
    window.update_idletasks()
    ui_theme.apply_native_titlebar(window, ui, dark)
    _place_over_parent(window, parent)
    window.deiconify()
    window.grab_set()
    entry.focus_set()
    worker = threading.Thread(target=identify, name='image-sequence-plan', daemon=True)
    worker.start()
    window.after(50, poll)
    parent.wait_window(window)
    return list(result)
