"""Managed portable update UI; slow work stays off the Tk thread."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from dlss5tool import managed_update, updater
from dlss5tool.i18n import tr


class ManagedUpdateUI:
    def _try_managed_update(self, release):
        layout = managed_update.current_layout()
        if layout is None:
            return False
        if not messagebox.askyesno(tr('dialog.new_version'),
                                  tr('update.managed_offer', version=release.tag,
                                     edition=tr('update.edition_' + layout.edition))):
            return True
        self._update_downloading = True
        self._update_progress_percent = 0
        self._update_cancel_event.clear()
        self._update_action_labels()
        events = queue.Queue()
        window = tk.Toplevel(self.root)
        window.title(tr('dialog.downloading_update'))
        window.transient(self.root)
        body = ttk.Frame(window, padding=20)
        body.pack(fill='both', expand=True)
        label = ttk.Label(body, text=tr('update.preparing'), wraplength=420)
        label.pack(anchor='w')
        bar = ttk.Progressbar(body, maximum=100)
        bar.pack(fill='x', pady=12)

        def cancel():
            self._update_cancel_event.set()
            label.config(text=tr('update.managed_cancel_wait'))

        ttk.Button(body, text=tr('common.cancel'), command=cancel).pack(side='left')
        ttk.Button(body, text=tr('update.managed_background'), command=window.withdraw).pack(side='right')
        window.protocol('WM_DELETE_WINDOW', window.withdraw)

        def worker():
            try:
                session = managed_update.Session(release, layout=layout)
                session.prepare(progress=lambda value: events.put(('progress', value)),
                                cancelled=self._update_cancel_event.is_set)
                events.put(('complete', session))
            except Exception as error:
                events.put(('error', error))

        def poll():
            complete = None
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'progress':
                    self._update_progress_percent = max(0, min(100, int(value)))
                    bar.config(value=self._update_progress_percent)
                    label.config(text=tr('status.download_percent', percent=self._update_progress_percent))
                    self._update_action_labels()
                else:
                    complete = kind, value
            if complete is None:
                self.root.after(100, poll)
                return
            window.destroy()
            self._update_downloading = False
            self._update_progress_percent = None
            self._update_thread = None
            self._update_action_labels()
            kind, value = complete
            if kind == 'error':
                if isinstance(value, updater.DownloadCancelled):
                    self.logln(tr('update.cancelled'))
                else:
                    self._delta_fallback(release, str(value))
                return
            self._managed_update_session = value
            self.logln(tr('update.managed_ready', version=release.tag))
            self._offer_managed_install()

        self._update_thread = threading.Thread(target=worker, name='dlss5-managed-update', daemon=True)
        self._update_thread.start()
        self.root.after(100, poll)
        return True

    def _offer_managed_install(self):
        if (self._exporting or self._queue_running or self._diagnosing or self._switching_backend
                or getattr(self, '_module_reload_thread', None) is not None):
            messagebox.showinfo(tr('dialog.busy'), tr('update.install_busy'))
            return
        if not messagebox.askyesno(tr('dialog.download_complete'), tr('update.managed_install')):
            return
        # Scheduling occurs at the end of _on_close, after GPU/worker retirement.
        # If closing cannot complete, no native installation has been scheduled.
        self._managed_apply_on_close = True
        self._on_close()

    def _schedule_managed_apply_after_cleanup(self):
        if not getattr(self, '_managed_apply_on_close', False):
            return True
        if getattr(self, '_managed_schedule_started', False):
            return False
        self._managed_schedule_started = True
        events = queue.Queue(maxsize=1)

        def schedule():
            try:
                self._managed_update_session.apply_on_exit(restart=True)
                events.put(None)
            except Exception as error:
                events.put(error)

        def poll():
            try:
                error = events.get_nowait()
            except queue.Empty:
                self.root.after(50, poll)
                return
            self._managed_schedule_started = False
            self._managed_apply_on_close = False
            if error is not None:
                messagebox.showerror(tr('dialog.update_failed'), str(error))
                return
            self._on_close()

        threading.Thread(target=schedule, name='dlss5-managed-install-check', daemon=True).start()
        self.root.after(50, poll)
        return False
