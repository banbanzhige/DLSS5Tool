"""Explicit recovery actions after a local export has stopped for RAM pressure."""
from dataclasses import dataclass, field
import os
import time
import tkinter as tk
from tkinter import messagebox, ttk

from dlss5tool.i18n import tr
from dlss5tool.memory_budget import copy_pressure_details


@dataclass(frozen=True)
class ExportRetry:
    kind: str
    source_path: str
    output_path: str
    settings: dict = field(default_factory=dict)
    export_settings: dict = field(default_factory=dict)
    color_info: dict = field(default_factory=dict)
    job_id: str = ''


def pressure_hint(details):
    reason = details.get('failure') or ('reserve' if details.get('blocked_by_reserve')
                                      else details.get('reason'))
    if reason not in ('physical', 'commit', 'reserve', 'busy', 'reclaim_timeout', 'idle'):
        reason = 'physical'
    return tr('memory.export.'+reason)


def show_memory_actions(parent, details, source_name):
    """No automatic retry: the user chooses after the failed call has unwound."""
    window = tk.Toplevel(parent)
    window.title(tr('memory.export.title'))
    window.transient(parent)
    window.resizable(False, False)
    result = ['close']
    def choose(action):
        result[0] = action
        window.destroy()
    panel = ttk.Frame(window, padding=18)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text=tr('memory.export.file',name=source_name),
              wraplength=470, justify='left').pack(fill='x',pady=(0,10))
    ttk.Label(panel, text=pressure_hint(details), wraplength=470, justify='left').pack(fill='x')
    ttk.Label(panel, text=tr('memory.export.restart'), wraplength=470,
              justify='left').pack(fill='x', pady=(10, 16))
    ttk.Button(panel, text=tr('memory.export.retry'), command=lambda: choose('retry')).pack(fill='x')
    row = ttk.Frame(panel)
    row.pack(fill='x', pady=(8, 0))
    ttk.Button(row, text=tr('memory.export.settings'), command=lambda: choose('settings')).pack(side='left')
    ttk.Button(row, text=tr('common.close'), command=lambda: choose('close')).pack(side='right')
    window.protocol('WM_DELETE_WINDOW', lambda: choose('close'))
    window.bind('<Escape>', lambda _event: choose('close'))
    window.grab_set()
    parent.wait_window(window)
    return result[0]


class ExportMemoryUI:
    def _memory_actions_available(self, request):
        if request.kind != 'queue':
            return self._can_clear_preview_cache()
        # A queue can run without any source loaded into the preview window.
        if any(getattr(self,name,False) for name in ('_exporting','_queue_running',
                '_diagnosing','_switching_backend','_module_reload_thread','_clear_preview_pending')):
            return False
        job = self._queue_job(request.job_id)
        return job is not None and job.source_path == request.source_path and job.state == 'failed'

    def _offer_export_memory_actions(self, details, request):
        details = copy_pressure_details(details)
        if details is None:
            return False
        source = self.video
        def offer():
            if (self.video != source or getattr(self, '_render_gpu_scan_closing', False)
                    or not self._memory_actions_available(request)):
                return
            action = show_memory_actions(self.root, details, os.path.basename(request.source_path))
            if action == 'settings':
                self._open_preview_cache_settings()
            elif action == 'retry':
                self._start_export_memory_retry(request, source)
        self.root.after(0, offer)
        return True

    def _open_preview_cache_settings(self):
        self.workspace_tabs.select(self._export_page)
        if self._preview_section.collapsed:
            self._preview_section.toggle()
        widget = self._preview_settings['w_cache_mb']
        def reveal():
            self.root.update_idletasks()
            inner = self._export_inner
            y = widget.winfo_rooty()-inner.winfo_rooty()
            self._export_canvas.yview_moveto(max(0.0, (y-80)/max(inner.winfo_height(), 1)))
            widget.focus_set()
        self.root.after_idle(reveal)
        self.set_status(tr('memory.export.settings_hint'))

    def _start_export_memory_retry(self, request, source):
        if (not self._memory_actions_available(request) or self.video != source
                or getattr(self, '_render_gpu_scan_closing', False)):
            return
        conflict = self._gpu_conflict('export')
        if conflict is not None and not (conflict.owner == 'release'
                                        and getattr(self, '_queue_cleanup_pending', False)):
            self._gpu_admit('export')
            return
        self._export_memory_retry = (request, source, time.monotonic())
        # Reuse the queue's owner-retirement guard. Replacements must wait
        # until every old owner exits, including after cleanup errors.
        self._queue_cleanup_pending = True
        self._queue_direct_cleanup_started = False
        self._clear_preview_pending = True
        self._guidance_preview_epoch = getattr(self,'_guidance_preview_epoch',0)+1
        self._guidance_result = self._guidance_ready = self._guidance_presented = None
        self._guidance_display_signature = None
        self._cancel_after('_live_debounce')
        self._cancel_after('_output_preview_after')
        self.pause()
        self._update_clear_cache_control()
        self.set_status(tr('memory.export.clearing'))
        self._export_memory_retry_after = self.root.after(0, self._poll_export_memory_retry)

    def _poll_export_memory_retry(self):
        from dlss5tool.queue_memory_recovery import WAIT_TIMEOUT
        self._export_memory_retry_after = None
        pending = getattr(self, '_export_memory_retry', None)
        if pending is None:
            return
        request, source, started = pending
        if self.video != source or getattr(self, '_render_gpu_scan_closing', False):
            self._cancel_export_memory_retry()
            return
        try:
            if not self._retire_queue_memory_owners():
                if time.monotonic()-started >= WAIT_TIMEOUT:
                    raise RuntimeError(tr('memory.export.cleanup_timeout'))
                self._export_memory_retry_after = self.root.after(250, self._poll_export_memory_retry)
                return
            self._finish_clear_preview_cache()
        except Exception as error:
            self._export_memory_retry = None
            self._clear_preview_pending = self._preview_cache_frozen = False
            # Keep the retirement guard on failure, never declare old owners
            # free just to make a retry possible.
            self._queue_cleanup_pending = True
            self._update_clear_cache_control()
            self.logln(str(error))
            messagebox.showerror(tr('memory.export.title'), str(error), parent=self.root)
            return
        self._export_memory_retry = None
        self._preview_cache_frozen = False
        self._export_memory_retry_after = self.root.after(0, lambda: self._run_export_memory_retry(request, source))

    def _run_export_memory_retry(self, request, source):
        self._export_memory_retry_after = None
        if (self.video != source or not self._memory_actions_available(request)
                or getattr(self, '_render_gpu_scan_closing', False)):
            return
        if not self._gpu_admit('export'):
            return
        if request.kind == 'queue':
            job = self._queue_job(request.job_id)
            if job is None or job.source_path != request.source_path or job.state != 'failed':
                return
            job.reset_for_retry()
            self._save_queue_state()
            self._refresh_queue_tree()
            self.start_export_queue()
            return
        # A failed writer may have left a partial output. Preserve it and any
        # later user-created file; retries never overwrite either one.
        output = request.output_path
        if os.path.exists(output):
            output = self._unique_target_path(output)
        if request.kind == 'image':
            self._export_image_source(request.source_path, dict(request.settings), output, notify=True)
        else:
            self._export_video_source(request.source_path, dict(request.settings),
                dict(request.export_settings), dict(request.color_info), output, notify=True)

    def _cancel_export_memory_retry(self):
        if getattr(self, '_export_memory_retry', None) is not None:
            self._clear_preview_pending = False
        self._export_memory_retry = None
        self._cancel_after('_export_memory_retry_after')
