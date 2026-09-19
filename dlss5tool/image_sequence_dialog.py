"""Small modal importer; image validation runs off the Tk thread."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from dlss5tool import paths
from dlss5tool.i18n import tr
from dlss5tool.image_sequence import COLOR_PROFILES, ImageSequence, parse_rate


def ask_sequence(parent, selected):
    window = tk.Toplevel(parent)
    window.title(tr('sequence.import'))
    window.transient(parent)
    window.resizable(False, False)
    body = ttk.Frame(window, padding=20)
    body.pack(fill='both', expand=True)
    ttk.Label(body, text=tr('sequence.help'), wraplength=440, justify='left').pack(anchor='w')
    ttk.Label(body, text=str(selected), wraplength=440).pack(anchor='w', pady=(8, 12))
    ttk.Label(body, text=tr('sequence.rate')).pack(anchor='w')
    rate = tk.StringVar(value='24')
    entry = ttk.Entry(body, textvariable=rate, width=24)
    entry.pack(anchor='w', pady=(4, 8))
    ttk.Label(body, text=tr('sequence.rate_help'), wraplength=440).pack(anchor='w')
    ttk.Label(body, text=tr('sequence.color')).pack(anchor='w', pady=(12, 0))
    color = ttk.Combobox(body, state='readonly', width=40,
        values=[tr('sequence.color_' + profile) for profile in COLOR_PROFILES])
    color.current(0)
    color.pack(anchor='w', pady=(4, 8))
    ttk.Label(body, text=tr('sequence.color_help'), wraplength=440, justify='left').pack(anchor='w')
    status = tk.StringVar(value='')
    ttk.Label(body, textvariable=status, wraplength=440, justify='left').pack(anchor='w', pady=(12, 4))
    bar = ttk.Progressbar(body, length=440, maximum=100)
    bar.pack(fill='x')
    actions = ttk.Frame(body)
    actions.pack(fill='x', pady=(12, 0))
    cancel = threading.Event()
    result = []
    events = queue.Queue()
    worker = None
    closing = False
    running = False

    def check_cancel():
        if cancel.is_set():
            raise InterruptedError()

    def close():
        nonlocal closing
        closing = True
        cancel.set()
        if worker is None or not worker.is_alive():
            window.destroy()
        else:
            status.set(tr('sequence.cancelling'))
            cancel_button.config(state='disabled')

    def poll():
        nonlocal running
        if closing:
            if worker is None or not worker.is_alive():
                window.destroy()
                return
        else:
            while True:
                try:
                    kind, payload = events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'progress':
                    done, total = payload
                    status.set(tr('sequence.scanning', done=done, total=total))
                    bar['value'] = done / total * 100
                elif kind == 'error':
                    running = False
                    status.set(str(payload))
                    submit.config(state='normal')
                    entry.config(state='normal')
                    color.config(state='readonly')
                    color.focus_set()
                    return
                elif kind == 'done':
                    try:
                        result.append(payload.save(paths.state_path('image-sequences')))
                    except Exception as error:
                        running = False
                        status.set(str(error))
                        submit.config(state='normal')
                        entry.config(state='normal')
                        color.config(state='readonly')
                        return
                    window.destroy()
                    return
        window.after(50, poll)

    def start(event=None):
        nonlocal worker, running
        if running or closing:
            return
        try:
            fps = parse_rate(rate.get())
        except ValueError as error:
            status.set(str(error))
            entry.focus_set()
            return
        submit.config(state='disabled')
        profile = COLOR_PROFILES[color.current()]
        color.config(state='disabled')
        running = True
        entry.config(state='disabled')
        status.set(tr('sequence.scanning', done=0, total='…'))
        def scan():
            try:
                sequence = ImageSequence.scan(selected, fps, check_cancel,
                    lambda done, total: events.put(('progress', (done, total)))
                    if done == total or done % 25 == 0 else None, color_profile=profile)
                events.put(('done', sequence))
            except Exception as error:
                events.put(('error', error))
        worker = threading.Thread(target=scan, name='image-sequence-scan', daemon=True)
        worker.start()
        window.after(50, poll)

    submit = ttk.Button(actions, text=tr('sequence.confirm'), command=start)
    submit.pack(side='right')
    cancel_button = ttk.Button(actions, text=tr('sequence.cancel'), command=close)
    cancel_button.pack(side='right', padx=(0, 8))
    window.protocol('WM_DELETE_WINDOW', close)
    window.bind('<Escape>', lambda event: close())
    window.bind('<Return>', start)
    window.grab_set()
    entry.focus_set()
    parent.wait_window(window)
    return result[0] if result else None
