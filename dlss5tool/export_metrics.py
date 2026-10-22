"""Bounded export telemetry: completed-frame rates and wall timings, not GPU time."""
from collections import deque
from contextlib import contextmanager
import math
import threading
import time

from dlss5tool.memory_budget import system_memory


class FrameRate:
    """Exclude startup, duplicate progress callbacks and non-frame percentages."""
    def __init__(self, *, window=30.0, minimum_span=2.0):
        self.window = float(window)
        self.minimum_span = float(minimum_span)
        self.samples = deque(maxlen=256)
        self.total = None

    def update(self, done, total, *, now=None):
        now = time.perf_counter() if now is None else float(now)
        done, total = max(0, int(done)), max(0, int(total))
        done = min(done, total)
        if self.total != total or (self.samples and done < self.samples[-1][1]):
            self.samples.clear()
        self.total = total
        if done > 0 and (not self.samples or done > self.samples[-1][1]):
            self.samples.append((now, done))
        while len(self.samples) > 1 and self.samples[1][0] <= now-self.window:
            self.samples.popleft()
        fps = None
        if len(self.samples) >= 2:
            start, first = self.samples[0]
            elapsed = now-start
            if elapsed >= self.minimum_span and done-first >= 2:
                fps = (done-first)/elapsed
        remaining = ((total-done)/fps if fps and done < total else
                     0.0 if done == total and total else None)
        return {'done': done, 'total': total, 'fps': fps, 'remaining_seconds': remaining}


class ExportTelemetry:
    """Constant-space totals plus bounded global RAM/commit observations.

    Global free-memory changes include other applications; they are context,
    never attributed to an SDK leak or extrapolated into a failure deadline.
    Timings may overlap across pipeline layers and must not be added together.
    """
    def __init__(self, *, memory_reader=system_memory, clock=time.perf_counter):
        self.clock, self.memory_reader = clock, memory_reader
        self.started = clock()
        self.lock = threading.Lock()
        self.stages = {}
        self.memory_samples = deque(maxlen=64)
        self.last_memory_time = float('-inf')

    def observe_memory(self):
        now = self.clock()
        with self.lock:
            if now-self.last_memory_time < 2:
                return
            self.last_memory_time = now
        try:
            memory = self.memory_reader()
        except Exception:
            return  # Diagnostics must not fail an otherwise valid export.
        if memory:
            sample = {'seconds': now-self.started,
                      'free_physical_bytes': int(memory['free_physical_bytes']),
                      'free_commit_bytes': int(memory['free_commit_bytes'])}
            with self.lock:
                self.memory_samples.append(sample)

    def record(self, name, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self.lock:
            stage = self.stages.setdefault(name, {'calls': 0, 'seconds': 0.0,
                'max_seconds': 0.0, 'warm_calls': 0, 'warm_seconds': 0.0})
            stage['calls'] += 1
            stage['seconds'] += seconds
            stage['max_seconds'] = max(stage['max_seconds'], seconds)
            if stage['calls'] > 4:
                stage['warm_calls'] += 1
                stage['warm_seconds'] += seconds
        self.observe_memory()

    @contextmanager
    def measure(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.record(name, self.clock()-started)

    def snapshot(self):
        with self.lock:
            return {'scope': 'wall-time; overlapping stages are not additive',
                    'elapsed_seconds': max(0.0, self.clock()-self.started),
                    'stages': {name: dict(value) for name, value in self.stages.items()},
                    'system_memory_samples': list(self.memory_samples)}
