"""Guidance-tab export UI, isolated from normal DLSS export settings."""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from dlss5tool import guidance_client
from dlss5tool.guidance_public import public_targets
from dlss5tool.guidance_export import export_guidance, GuidanceExportCancelled
from dlss5tool.i18n import tr
from dlss5tool.ui_widgets import Tooltip


class GuidanceExportUI:
    def _build_guidance_export(self):
        footer = ttk.Frame(self._guidance_page, style='Panel.TFrame')
        footer.grid(row=1, column=0, columnspan=2, sticky='ew', padx=16, pady=(8, 16))
        footer.columnconfigure(1, weight=1)
        self._guidance_export_footer = footer
        self._guidance_export_active = False
        self._guidance_export_target = tk.StringVar(value=tr('view.' + public_targets()[0]))
        self._guidance_export_scope = tk.StringVar(value=tr('guidance.export.video'))
        ttk.Label(footer, text=tr('guidance.export.content')).grid(row=0, column=0, sticky='w', padx=(0, 8))
        target = self._chrome_combo(footer, self._guidance_export_target, [tr('view.' + name) for name in public_targets()])
        target.grid(row=0, column=1, sticky='ew', pady=3)
        target.bind('<<ComboboxSelected>>', lambda e: self._update_guidance_export_controls())
        ttk.Label(footer, text=tr('guidance.export.scope')).grid(row=1, column=0, sticky='w', padx=(0, 8))
        scope = self._chrome_combo(footer, self._guidance_export_scope,
                                   [tr('guidance.export.video'), tr('guidance.export.frame')])
        scope.grid(row=1, column=1, sticky='ew', pady=3)
        scope.bind('<<ComboboxSelected>>', lambda e: self._update_guidance_export_controls())
        button = self._chrome_button(footer, text=tr('guidance.export.start'), command=self._start_guidance_export,
                                      variant='accent')
        button.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        cancel = self._chrome_button(footer, text=tr('action.cancel_export'), command=self._cancel_guidance_export,
                                     variant='danger')
        Tooltip(button, tr('guidance.export.hint'))
        self._guidance_export_widgets = dict(target=target, scope=scope, button=button, cancel=cancel)

    def _update_guidance_export_controls(self):
        if not hasattr(self, '_guidance_export_widgets'):
            return
        widgets = self._guidance_export_widgets
        busy = self._exporting or self._queue_running or self._switching_backend or self._diagnosing
        target = 'flow' if self._guidance_export_target.get() == tr('view.flow') else 'depth'
        mode = self._collect_host_settings()['guidance_mode']
        available = mode in ((1, 3) if target == 'flow' else (2, 3))
        if self._is_image:
            self._guidance_export_scope.set(tr('guidance.export.frame'))
        enabled = bool(self.video) and available and not (self._video_color_info or {}).get('is_hdr') and not busy
        widgets['target'].config(state='disabled' if busy else 'readonly')
        widgets['scope'].config(state='disabled' if busy or self._is_image else 'readonly')
        widgets['button'].config(state='normal' if enabled else 'disabled')
        widgets['button'].config(text=tr('guidance.export.button', view=tr('view.' + target)))
        if self._guidance_export_active:
            widgets['button'].grid_remove()
            widgets['cancel'].grid(row=2, column=0, columnspan=2, sticky='ew', pady=(8, 0))
            widgets['cancel'].config(state='disabled' if self._export_cancel_event.is_set() else 'normal')
        else:
            widgets['cancel'].grid_remove()
            widgets['button'].grid()

    def _cancel_guidance_export(self):
        if self._guidance_export_active:
            self._export_cancel_event.set()
            self._update_guidance_export_controls()
            self.set_status(tr('status.cancelling_export'))

    def _start_guidance_export(self):
        if not self.video or self._exporting or self._queue_running or self._switching_backend or self._diagnosing:
            return
        target = 'flow' if self._guidance_export_target.get() == tr('view.flow') else 'depth'
        settings = self._collect_settings()
        mode = settings['guidance_mode']
        if mode not in ((1, 3) if target == 'flow' else (2, 3)):
            messagebox.showwarning(tr('tab.guidance'), tr('guidance.preview_disabled', view=tr('view.' + target)))
            return
        if (self._video_color_info or {}).get('is_hdr'):
            messagebox.showwarning(tr('tab.guidance'), tr('guidance.preview_sdr'))
            return
        try:
            guidance_client.validate(settings)
        except Exception as error:
            messagebox.showerror(tr('tab.guidance'), str(error))
            return
        video = not self._is_image and self._guidance_export_scope.get() == tr('guidance.export.video')
        extension = '.mp4' if video else '.png'
        source = self.video
        default = os.path.splitext(os.path.basename(source))[0] + '_' + target
        if not video:
            default += '_' + str(self._frame)
        path = filedialog.asksaveasfilename(parent=self.root, title=tr('guidance.export.start'),
                                           initialfile=default + extension, defaultextension=extension,
                                           filetypes=[('MP4' if video else 'PNG', '*' + extension)])
        if not path:
            return
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(source)):
            messagebox.showerror(tr('tab.guidance'), tr('guidance.export.source_error'))
            return
        still = self._image_bgr.copy() if self._is_image else None
        frame = None if video else self._frame
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        previous = self._play_dlss_thread
        self._guidance_preview_epoch += 1
        self._guidance_result = self._guidance_ready = self._guidance_presented = None
        self._guidance_display_signature = None
        self._export_cancel_event.clear()
        self._exporting = self._guidance_export_active = True
        self._update_action_labels()
        self._update_host_control_states()
        self._update_queue_action_states()
        self.set_status(tr('guidance.export.preparing'))
        events = queue.SimpleQueue()

        def work():
            try:
                while previous is not None and previous.is_alive():
                    if self._export_cancel_event.wait(0.05):
                        raise GuidanceExportCancelled()
                # Release the preview component before opening the export session;
                # no second copy of the model is kept on the GPU.
                self._close_live()
                last_report = [0.0]
                def progress(done, total):
                    now = time.monotonic()
                    if done == total or now - last_report[0] >= 0.1:
                        events.put(('progress', done, total))
                        last_report[0] = now
                count = export_guidance(source, path, settings, target, frame=frame, still=still,
                                         cancel=self._export_cancel_event, progress=progress)
                events.put(('done', count))
            except GuidanceExportCancelled:
                events.put(('cancelled',))
            except Exception as error:
                events.put(('error', str(error)))

        def poll():
            terminal = None
            while True:
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    break
                if item[0] == 'progress':
                    self.pbar['maximum'] = max(item[2], 1)
                    self.pbar['value'] = item[1]
                    self.eta_label.config(text=tr('guidance.export.progress', done=item[1], total=item[2]))
                else:
                    terminal = item
            if terminal is None:
                self.root.after(100, poll)
                return
            self._exporting = self._guidance_export_active = False
            self._export_cancel_event.clear()
            self.pbar['value'] = 0
            self._update_action_labels()
            self._update_host_control_states()
            self._update_queue_action_states()
            self._schedule_preview_cache_resume()
            if terminal[0] == 'done':
                message = tr('status.exported', path=path)
                self.logln(message)
                self.set_status(message)
                messagebox.showinfo(tr('tab.export'), message, parent=self.root)
            elif terminal[0] == 'cancelled':
                self.set_status(tr('guidance.export.cancelled'))
            else:
                self.logln(terminal[1])
                self.set_status(tr('status.export_failed_log'))
                messagebox.showerror(tr('dialog.export_failed'), terminal[1], parent=self.root)
        self._guidance_export_thread = threading.Thread(target=work, name='guidance-export', daemon=True)
        self._guidance_export_thread.start()
        self.root.after(100, poll)
