"""Managed portable update UI; slow work stays off the Tk thread."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from dlss5tool import managed_update, updater
from dlss5tool.i18n import tr


class ManagedUpdateUI:
    def _cancel_managed_apply_on_close(self):
        self._managed_apply_on_close = False

    def _is_closing_for_managed_update(self):
        return bool(
            getattr(self, '_app_closing', False)
            or getattr(self, '_managed_schedule_started', False)
        )

    def _set_managed_status(self, text):
        if hasattr(self, 'set_status'):
            self.set_status(text)

    def _is_managed_install_busy(self):
        return bool(
            self._exporting or self._queue_running or self._diagnosing or self._switching_backend
            or getattr(self, '_module_reload_thread', None) is not None
            or getattr(self, '_managed_verifying', False)
            or self._is_closing_for_managed_update()
        )

    def _try_managed_update(self, release):
        layout = managed_update.current_layout()
        if layout is None:
            return False
        if managed_update.has_prepared_update(layout, release):
            self._resume_managed_update(release, layout)
            return True
        self._prompt_managed_download(release, layout)
        return True

    def _resume_managed_update(self, release, layout):
        """Reuse a persisted ready package after restart without re-downloading."""
        self._managed_verifying = True
        self._set_managed_status(tr('update.managed_verifying'))
        events = queue.Queue(maxsize=1)

        def verify():
            try:
                session = managed_update.Session(release, layout=layout)
                session.ready()
                events.put((session, None))
            except Exception as error:
                events.put((None, error))

        def finish(session, error):
            self._managed_verifying = False
            if self._is_closing_for_managed_update():
                return
            if error is None:
                self._managed_update_session = session
                ready_text = tr('update.managed_ready', version=release.tag)
                self._set_managed_status(ready_text)
                self.logln(ready_text)
                self._offer_managed_install()
                return
            self._set_managed_status('')
            self._prompt_managed_download(release, layout)

        def poll():
            try:
                session, error = events.get_nowait()
            except queue.Empty:
                self.root.after(50, poll)
                return
            finish(session, error)

        try:
            threading.Thread(target=verify, name='dlss5-managed-resume-check', daemon=True).start()
        except Exception as error:
            finish(None, error)
            return
        self.root.after(50, poll)

    def _prompt_managed_download(self, release, layout):
        if not messagebox.askyesno(tr('dialog.new_version'),
                                  tr('update.managed_offer', version=release.tag,
                                     edition=tr('update.edition_' + layout.edition))):
            return
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
            try:
                if window.winfo_exists():
                    label.config(text=tr('update.managed_cancel_wait'))
            except tk.TclError:
                pass

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

        def finish(kind, value):
            try:
                if window.winfo_exists():
                    window.destroy()
            except tk.TclError:
                pass
            self._update_downloading = False
            self._update_progress_percent = None
            self._update_thread = None
            self._update_action_labels()
            if kind == 'error':
                if isinstance(value, updater.DownloadCancelled):
                    self.logln(tr('update.cancelled'))
                else:
                    self._delta_fallback(release, str(value))
                return
            self._managed_update_session = value
            self.logln(tr('update.managed_ready', version=release.tag))
            self._offer_managed_install()

        def poll():
            complete = None
            while True:
                try:
                    kind, value = events.get_nowait()
                except queue.Empty:
                    break
                if kind == 'progress':
                    self._update_progress_percent = max(0, min(100, int(value)))
                    try:
                        if window.winfo_exists():
                            bar.config(value=self._update_progress_percent)
                            if not self._update_cancel_event.is_set():
                                label.config(text=tr('status.download_percent',
                                                     percent=self._update_progress_percent))
                    except tk.TclError:
                        pass
                    self._update_action_labels()
                else:
                    complete = kind, value
            if complete is None:
                try:
                    self.root.after(100, poll)
                except tk.TclError:
                    pass
                return
            finish(*complete)

        self._update_thread = threading.Thread(target=worker, name='dlss5-managed-update', daemon=True)
        try:
            self._update_thread.start()
        except Exception as error:
            finish('error', error)
            return
        self.root.after(100, poll)

    def _offer_managed_install(self):
        if self._is_managed_install_busy():
            messagebox.showinfo(tr('dialog.busy'), tr('update.install_busy'))
            return
        if getattr(self, '_managed_update_session', None) is None:
            return
        if not messagebox.askyesno(tr('dialog.download_complete'), tr('update.managed_install')):
            return
        self._begin_managed_install()

    def _begin_managed_install(self):
        """Verify readiness before tearing down UI/media resources."""
        session = getattr(self, '_managed_update_session', None)
        if session is None or self._is_managed_install_busy():
            return
        self._managed_verifying = True
        self._set_managed_status(tr('update.managed_verifying'))
        events = queue.Queue(maxsize=1)

        def verify():
            try:
                session.ready()
                events.put(None)
            except Exception as error:
                events.put(error)

        def finish(error, *, invalidate_session=True):
            self._managed_verifying = False
            if self._is_closing_for_managed_update():
                return
            if error is not None:
                if invalidate_session and getattr(self, '_managed_update_session', None) is session:
                    self._managed_update_session = None
                self._set_managed_status(str(error))
                messagebox.showerror(tr('dialog.update_failed'), str(error))
                return
            release = getattr(session, 'release', None)
            tag = getattr(release, 'tag', None)
            ready_text = tr('update.managed_ready', version=tag) if isinstance(tag, str) and tag else ''
            if self._is_managed_install_busy():
                self._set_managed_status(ready_text)
                messagebox.showinfo(tr('dialog.busy'), tr('update.install_busy'))
                return
            self._set_managed_status(ready_text)
            # Scheduling occurs at the end of _on_close, after GPU/worker retirement.
            # Any early close abort clears this flag so a later normal close cannot
            # unexpectedly apply an update.
            self._managed_apply_on_close = True
            self._on_close()

        def poll():
            try:
                error = events.get_nowait()
            except queue.Empty:
                self.root.after(50, poll)
                return
            finish(error)

        try:
            threading.Thread(target=verify, name='dlss5-managed-ready-check', daemon=True).start()
        except Exception as error:
            finish(error, invalidate_session=False)
            return
        self.root.after(50, poll)

    def _schedule_managed_apply_after_cleanup(self):
        if not getattr(self, '_managed_apply_on_close', False):
            return True
        if getattr(self, '_managed_schedule_started', False):
            return False
        session = getattr(self, '_managed_update_session', None)
        if session is None:
            self._managed_apply_on_close = False
            return True
        self._app_closing = True
        self._managed_schedule_started = True
        self._set_managed_status(tr('update.managed_scheduling'))
        events = queue.Queue(maxsize=1)

        def schedule():
            try:
                session.apply_on_exit(restart=True)
                events.put(None)
            except Exception as error:
                events.put(error)

        def finish(error):
            self._managed_schedule_started = False
            self._managed_apply_on_close = False
            if error is not None:
                messagebox.showerror(tr('dialog.update_failed'), str(error))
            # Close teardown has already retired media/audio/GPU resources; finish
            # destroying the window cleanly rather than stranding a torn-down UI.
            finalize = getattr(self, '_finalize_window_close', None) or self._on_close
            finalize()

        def poll():
            try:
                error = events.get_nowait()
            except queue.Empty:
                self.root.after(50, poll)
                return
            finish(error)

        try:
            threading.Thread(target=schedule, name='dlss5-managed-install-check', daemon=True).start()
        except Exception as error:
            finish(error)
            return False
        self.root.after(50, poll)
        return False
