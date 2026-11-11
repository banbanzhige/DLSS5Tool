"""Shared, conservative admission policy for the two workbenches.

Observe the existing task lifecycles instead of maintaining a second busy flag.
Only disposable local preview resources may be retired; never cancel exports or
realtime sessions and never change render settings. Tk owns admission; workers
only publish completion/error data.
"""
from dataclasses import asdict, dataclass
import threading

from dlss5tool.i18n import tr


@dataclass(frozen=True)
class Occupancy:
    owner: str
    phase: str
    detail: str = ''


def conflict(operation, occupants):
    for item in occupants:
        if item.owner == 'preview':
            # Cached preview can be reused by a local export, but not by RT.
            if operation in ('realtime', 'diagnostics'):
                return item
        elif item.owner == 'queue' and operation == 'export':
            continue  # The queue owns its serial export child.
        elif item.owner == 'realtime' and operation == 'realtime':
            continue  # The existing RT start/restart state machine owns this.
        else:
            return item
    return None


def describe(item):
    if item is None:
        return ''
    return tr('resources.conflict', owner=tr('resources.owner.' + item.owner),
              phase=tr('resources.phase.' + item.phase), detail=item.detail)


class GpuResourceGuard:
    @staticmethod
    def _gpu_thread_alive(thread):
        return thread is not None and thread.is_alive()

    def _gpu_occupancy(self):
        items = []
        release = getattr(self, '_gpu_release_thread', None)
        error = getattr(self, '_gpu_release_error', None) or getattr(self, '_shared_retire_error', None)
        if error:
            items.append(Occupancy('release', 'failed', str(error)))
        elif self._gpu_thread_alive(release) or self._gpu_thread_alive(getattr(self, '_shared_retiring', None)):
            items.append(Occupancy('release', 'releasing'))
        if getattr(self, '_queue_cleanup_pending', False):
            items.append(Occupancy('release', 'releasing'))
        for flag, owner in (('_exporting', 'export'), ('_queue_running', 'queue'),
                            ('_deep_diagnosing', 'diagnostics'), ('_switching_backend', 'backend'),
                            ('_module_reload_thread', 'backend'), ('_clear_preview_pending', 'release')):
            if getattr(self, flag, False):
                items.append(Occupancy(owner, 'running' if owner != 'release' else 'releasing'))
        workspace = getattr(self, '_realtime_workspace', None)
        if workspace and workspace.session_summary()[1]:
            phase = ('releasing' if getattr(workspace, '_recovering', False) else
                     'stopping' if getattr(workspace, '_stop_requested', False) or getattr(workspace, '_native_state', 0) == 3 else
                     'starting' if getattr(workspace, '_pending_start', None) else 'running')
            items.append(Occupancy('realtime', phase))
        if (getattr(self, '_live', None) is not None or getattr(self, '_super_resolution_live', None) is not None
                or getattr(self, '_render_cache_manager', None) is not None
                or any(self._gpu_thread_alive(getattr(self, name, None)) for name in
                       ('_play_dlss_thread', '_shared_setup_thread'))):
            items.append(Occupancy('preview', 'running'))
        return items

    def _gpu_conflict(self, operation):
        return conflict(operation, self._gpu_occupancy())

    def _gpu_admit(self, operation, *, notify=True):
        item = self._gpu_conflict(operation)
        if item is None:
            return True
        if notify:
            from tkinter import messagebox
            messagebox.showinfo(tr('dialog.busy'), describe(item), parent=self.root)
        return False

    def _local_gpu_allowed(self):
        return (getattr(self, '_workbench_mode', 'local') == 'local'
                and self._gpu_conflict('preview') is None)

    def _gpu_resource_snapshot(self):
        return {'schema': 1, 'scope': 'this-application',
                'occupants': [asdict(item) for item in self._gpu_occupancy()],
                'policy': 'no-preemption; no-quality-downgrade; release-before-start'}

    def _release_preview_for_realtime(self):
        """Called only by an explicit RT start, after other tasks were excluded."""
        self._gpu_release_error = None
        self.pause()
        self._freeze_preview_cache(resume_ms=None)
        self._cancel_after('_scrub_after')
        self._shared_retire()
        pending = [getattr(self, name, None) for name in ('_play_dlss_thread', '_shared_retiring')]

        def release():
            try:
                for thread in pending:
                    if thread is not None:
                        thread.join()
                if getattr(self, '_shared_retire_error', None):
                    raise RuntimeError(self._shared_retire_error)
                # Unlike best-effort application shutdown, admission must not
                # report idle when a close operation failed.
                for lock_name, field in (('_live_lock', '_live'),
                                         ('_super_resolution_lock', '_super_resolution_live')):
                    with getattr(self, lock_name):
                        resource = getattr(self, field, None)
                        if resource is not None:
                            resource.close()
                            setattr(self, field, None)
                self._super_resolution_key = None
                self._last_super_resolution_preview = None
                self._cache_clear()
            except Exception as error:
                self._gpu_release_error = str(error)

        self._gpu_release_thread = threading.Thread(target=release, name='gpu-preview-release', daemon=True)
        self._gpu_release_thread.start()
        self._gpu_release_after = self.root.after(50, self._poll_gpu_release)

    def _poll_gpu_release(self):
        self._gpu_release_after = None
        if self._gpu_thread_alive(getattr(self, '_gpu_release_thread', None)):
            self._gpu_release_after = self.root.after(50, self._poll_gpu_release)
            return
        self._preview_cache_frozen = False
        item = self._gpu_conflict('realtime')
        message = describe(item) if item else tr('resources.ready')
        workspace = getattr(self, '_realtime_workspace', None)
        if workspace and workspace.session_summary()[1]:
            return  # A fast explicit retry already owns the status message.
        if workspace and not workspace._closed:
            workspace.status.set(message)
        self.set_status(message)
        # No deferred auto-start: the selected window/settings may have changed.
