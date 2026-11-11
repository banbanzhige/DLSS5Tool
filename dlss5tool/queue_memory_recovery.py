"""Bounded whole-item queue recovery using the existing RAM/commit policy."""
from dataclasses import dataclass
import time

from dlss5tool import memory_budget as mb
from dlss5tool.i18n import tr

MAX_RETRIES = 2
WAIT_TIMEOUT = 120.0
POLL_MS = 250


@dataclass
class Recovery:
    job_id: str
    details: dict
    attempt: int
    started: float
    retired: bool = False
    ready_since: float | None = None
    cleanup_error: str = ''

    def poll(self, memory, now):
        if now-self.started >= WAIT_TIMEOUT:
            return 'timeout'
        required = max(0, self.details.get('required_bytes') or 0,
                       self.details.get('recommended_bytes') or 0)
        headroom = mb.memory_details(required, memory=memory)['available_bytes'] if memory else None
        if not self.retired or headroom is None or headroom < required+2*mb.RECOVERY_MARGIN:
            self.ready_since = None
            return 'wait'
        if self.ready_since is None:
            self.ready_since = now
        return 'ready' if now-self.ready_since >= mb.memory_policy()['recovery_delay_seconds'] else 'wait'


class QueueMemoryRecovery:
    def _begin_queue_memory_recovery(self, job, details):
        details = mb.copy_pressure_details(details)
        if details is None:
            return False
        attempts = getattr(self, '_queue_memory_attempts', None)
        if attempts is None:
            attempts = self._queue_memory_attempts = {}
        attempt = attempts.get(job.job_id, 0)+1
        self._queue_cleanup_pending = True
        self._queue_direct_cleanup_started = False
        if attempt > MAX_RETRIES:
            try:
                self._retire_queue_memory_owners()
            except Exception as error:
                self.logln('[队列] '+str(error))
            self._pause_queue_memory_failure(job, tr('queue.memory_exhausted'))
            return True
        attempts[job.job_id] = attempt
        self._queue_memory_recovery = Recovery(job.job_id, details, attempt, time.monotonic())
        self._queue_active_job_id = job.job_id
        job.state = 'running'
        job.finished_at = 0.0
        job.progress_label = tr('queue.memory_wait', attempt=attempt, limit=MAX_RETRIES)
        self.logln('[队列] '+job.progress_label)
        self.set_status(job.progress_label)
        self._save_queue_state()
        self._refresh_queue_tree()
        self._update_queue_action_states()
        self._queue_recovery_after = self.root.after(0, self._poll_queue_memory_recovery)
        return True

    def _retire_queue_memory_owners(self):
        """Retain failed owners and never start replacement work before exit."""
        self._freeze_preview_cache(resume_ms=None)
        if self._wait_play_dlss(timeout=.25) is False:
            return False
        for name in ('_local_composition_job', '_shared_interim_job'):
            job = getattr(self, name, None)
            if job is not None:
                job.close()
                if job.is_alive():
                    return False
                setattr(self, name, None)
        actions = []
        if not getattr(self, '_queue_direct_cleanup_started', False):
            actions = [('queue NR', self._close_live), ('queue VSR', self._close_super_resolution)]
            self._queue_direct_cleanup_started = True
        self._cleanup_video_export(actions)
        previous = getattr(self, '_shared_retiring', None)
        if previous is not None and previous.is_alive():
            return False
        if (getattr(self, '_render_cache_manager', None) is not None or
                getattr(self, '_shared_retire_pending', None)):
            self._shared_retire()
        retiring = getattr(self, '_shared_retiring', None)
        if retiring is not None and retiring.is_alive():
            return False
        if getattr(self, '_shared_retire_pending', None):
            raise RuntimeError(self._shared_retire_error or tr('queue.memory_cleanup'))
        self._queue_cleanup_pending = False
        return True

    def _poll_queue_memory_recovery(self):
        self._queue_recovery_after = None
        recovery = getattr(self, '_queue_memory_recovery', None)
        if recovery is None or not self._queue_running:
            return
        job = self._queue_job(recovery.job_id)
        if job is None or self._queue_active_job_id != recovery.job_id:
            self._queue_memory_recovery = None
            self._finish_queue_run(paused=True)
            return
        if self._queue_pause_requested:
            self._cancel_queue_memory_wait(cancelled=False)
            return
        if not recovery.retired:
            try:
                recovery.retired = self._retire_queue_memory_owners()
            except Exception as error:
                # Keeping traceback-bearing errors here would retain frames.
                message = str(error)
                if message != recovery.cleanup_error:
                    self.logln('[队列] '+message)
                recovery.cleanup_error = message
        pool = getattr(self, '_shared_cache_pool', None)
        try:
            if pool is not None:
                pool.request_reclaim(max(0, recovery.details.get('required_bytes') or 0))
            status = recovery.poll(mb.fresh_system_memory(), time.monotonic())
        except Exception as error:
            self._pause_queue_memory_failure(job, str(error))
            return
        if status == 'timeout':
            reason = recovery.cleanup_error or tr('queue.memory_timeout')
            self._pause_queue_memory_failure(job, reason)
            return
        if status == 'ready':
            self._queue_memory_recovery = None
            self._queue_active_job_id = None
            job.reset_for_retry()
            message = tr('queue.memory_retry', attempt=recovery.attempt, limit=MAX_RETRIES)
            self.logln('[队列] '+message)
            self.set_status(message)
            self._save_queue_state()
            self._refresh_queue_tree()
            self.root.after(0, self._run_next_queue_job)
            return
        self._queue_recovery_after = self.root.after(POLL_MS, self._poll_queue_memory_recovery)

    def _pause_queue_memory_failure(self, job, reason):
        self._cancel_after('_queue_recovery_after')
        self._queue_memory_recovery = None
        job.state = 'failed'
        job.error = tr('queue.memory_stopped', reason=reason)
        job.finished_at = time.time()
        self.logln('[队列] '+job.error)
        self.set_status(job.error)
        self._save_queue_state()
        self._refresh_queue_tree()
        self._finish_queue_run(paused=True)

    def _cancel_queue_memory_wait(self, *, cancelled):
        recovery = getattr(self, '_queue_memory_recovery', None)
        if recovery is None:
            return False
        if not recovery.retired:
            try:
                self._retire_queue_memory_owners()
            except Exception as error:
                self.logln('[队列] '+str(error))
        self._cancel_after('_queue_recovery_after')
        self._queue_memory_recovery = None
        job = self._queue_job(recovery.job_id)
        if job is not None:
            job.state = 'cancelled' if cancelled else 'pending'
            job.error = tr('message.job_cancelled') if cancelled else ''
            job.finished_at = time.time() if cancelled else 0.0
            self._save_queue_state()
            self._refresh_queue_tree()
        self._finish_queue_run(paused=True)
        return True
