"""System RAM/commit headroom for disposable caches, never model precision.

The saved cache preference remains an upper bound. The system/model reserve is
one eighth of RAM, clamped to 2..4 GiB after bounded local peak calibration.
Pending processing allocations also reduce disposable cache capacity. This is
an admission policy, not a process-wide allocation quota or OOM guarantee.
"""
import ctypes as C
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import os
import threading
import time
import weakref

_GIB = 1024**3
_snapshot_lock = threading.Lock()
_snapshot = None
_snapshot_time = float('-inf')
_pool_context = ContextVar('dlss5_memory_pool', default=None)
_processing_priority = ContextVar('dlss5_processing_priority',default=1)
_preferred_pool = None
_default_pool = None
_pool_lock = threading.RLock()
_events = deque(maxlen=64)
_events_lock = threading.Lock()

# Kept separate so calibration can evaluate physical and process commit limits.
# Numeric policy is recorded with the calibration evidence.
PHYSICAL_RESERVE_MIN = 2 * _GIB
PHYSICAL_RESERVE_MAX = 4 * _GIB
PHYSICAL_RESERVE_DIVISOR = 8
COMMIT_RESERVE = None
RECOVERY_MARGIN = 128 * 1024**2
RECLAIM_TIMEOUT = 1.5


def memory_policy():
    return dict(version='ram-coordination-20261026',physical_reserve_min=PHYSICAL_RESERVE_MIN,
        physical_reserve_max=PHYSICAL_RESERVE_MAX,physical_reserve_divisor=PHYSICAL_RESERVE_DIVISOR,
        commit_reserve='same_as_physical' if COMMIT_RESERVE is None else COMMIT_RESERVE,
        cache_physical_fraction=.25,recovery_margin_bytes=RECOVERY_MARGIN,
        recovery_headroom_bytes=2*RECOVERY_MARGIN,recovery_delay_seconds=.5,
        reclaim_timeout_seconds=RECLAIM_TIMEOUT,idle_retire_seconds=5.0)


class _MemoryStatus(C.Structure):
    _fields_ = [('length', C.c_uint32), ('load', C.c_uint32)] + [
        (name, C.c_uint64) for name in (
            'total_physical', 'free_physical', 'total_commit', 'free_commit',
            'total_virtual', 'free_virtual', 'extended')]


@lru_cache(maxsize=1)
def _status_function():
    if os.name != 'nt':
        return None
    kernel = C.WinDLL('kernel32', use_last_error=True)
    function = kernel.GlobalMemoryStatusEx
    function.argtypes = [C.POINTER(_MemoryStatus)]
    function.restype = C.c_int
    return function


def system_memory():
    """Cheap cached Windows snapshot; unavailable platforms retain prior policy."""
    global _snapshot, _snapshot_time
    with _snapshot_lock:
        now = time.monotonic()
        if now - _snapshot_time < .25:
            return _snapshot
        try:
            function = _status_function()
            status = _MemoryStatus()
            status.length = C.sizeof(status)
            if function is None or not function(C.byref(status)):
                result = None
            else:
                result = {'total_physical_bytes': status.total_physical,
                          'free_physical_bytes': status.free_physical,
                          'free_commit_bytes': status.free_commit}
        except (OSError, AttributeError):
            result = None
        _snapshot, _snapshot_time = result, now
        return result


def fresh_system_memory():
    """Invalidate the timed cache, including when a test supplies a reader."""
    global _snapshot_time
    with _snapshot_lock:
        _snapshot_time = float('-inf')
    return system_memory()


def reserve_bytes(memory, *, critical=False):
    total = max(0, int(memory.get('total_physical_bytes', 0)))
    if critical:
        return min(512 * 1024**2, max(256 * 1024**2, total // 128))
    return min(PHYSICAL_RESERVE_MAX, max(PHYSICAL_RESERVE_MIN,
                                        total // PHYSICAL_RESERVE_DIVISOR))


def memory_details(required=0, *, memory=None, stage='allocation', critical=False):
    memory = system_memory() if memory is None else memory
    result = dict(stage=stage, required_bytes=max(0, int(required)), memory=memory,
                  observed_at=time.time(),recommended_bytes=None,
                  physical_reserve_bytes=0, commit_reserve_bytes=0,
                  available_bytes=None, reason='unknown_snapshot')
    if not memory or int(memory['total_physical_bytes']) <= 0:
        return result
    result['memory']=dict(memory)
    reserve = reserve_bytes(memory, critical=critical)
    commit_reserve = reserve if COMMIT_RESERVE is None else min(reserve, COMMIT_RESERVE)
    physical = max(0, int(memory['free_physical_bytes']) - reserve)
    commit = max(0, int(memory['free_commit_bytes']) - commit_reserve)
    result.update(physical_reserve_bytes=reserve, commit_reserve_bytes=commit_reserve,
                  available_bytes=min(physical, commit),
                  reason='physical' if physical <= commit else 'commit',
                  blocked_by_reserve=min(int(memory['free_physical_bytes']),
                    int(memory['free_commit_bytes']))>=int(required)>min(physical,commit))
    return result


def record_memory_event(event, **details):
    # Plain immutable-size data only; never retain exceptions, arrays or frames.
    with _events_lock:
        _events.append(dict(event=event, pid=os.getpid(), time=time.time(), **details))


def memory_events():
    from copy import deepcopy
    with _events_lock:
        return deepcopy(list(_events))


class MemoryPressureError(MemoryError):
    def __init__(self, details):
        from dlss5tool.i18n import tr
        self.details = details
        size = lambda n: (f'{n/_GIB:.2f} GiB' if n >= _GIB else
                          f'{max(1,round(n/1024**2)) if n else 0} MiB')
        memory = details.get('memory') or {}
        reason = details.get('failure', 'reserve' if details.get('blocked_by_reserve') else
                             details.get('reason', 'physical'))
        message = tr('message.ram_pressure_' + reason)
        message += ' ' + tr('message.ram_pressure_values',
            required=size(details['required_bytes']),
            available=size(details.get('available_bytes') or 0),
            physical=size(memory.get('free_physical_bytes', 0)),
            commit=size(memory.get('free_commit_bytes', 0)))
        super().__init__(message)


def prefer_pool(pool):
    global _preferred_pool
    _preferred_pool = weakref.ref(pool)


def current_pool(*, create=True):
    global _default_pool
    pool = _pool_context.get()
    if pool is None and _preferred_pool is not None:
        pool = _preferred_pool()
    if pool is not None and getattr(pool, 'mapping', None) is not None:
        return pool
    if _default_pool is not None and _default_pool.mapping is not None:
        return _default_pool
    if not create or os.name != 'nt':
        return None
    with _pool_lock:
        if _default_pool is None or _default_pool.mapping is None:
            from dlss5tool.shared_cache_budget import SharedCacheBudget
            _default_pool = SharedCacheBudget(limit_bytes=8*_GIB)
        return _default_pool


def processing_priority():return _processing_priority.get()


def set_processing_priority(foreground):_processing_priority.set(1 if foreground else 0)


@contextmanager
def memory_scope(pool):
    """Bind one workflow's pool without changing another thread's workflow."""
    token = _pool_context.set(pool)
    priority_token=_processing_priority.set(1)
    try:
        yield pool
    finally:
        _pool_context.reset(token)
        _processing_priority.reset(priority_token)


def worker_with_memory_pool(target, name, arguments):
    """Spawn entry; worker/model descendants share the parent's ledger."""
    from dlss5tool.shared_cache_budget import SharedCacheBudget
    pool = SharedCacheBudget(name) if name else None
    if pool is not None:
        prefer_pool(pool)
    try:
        with memory_scope(pool):
            return target(*arguments)
    finally:
        if pool is not None:
            pool.close()


def cache_limit_bytes(requested, cached=0, *, memory=None):
    """Limit total cache bytes, crediting already allocated cached memory once.

    Subtract a headroom deficit too: clamping free headroom to zero first would
    keep a full cache intact under pressure. Active inputs/copies are not given
    cache credit and must fit within the reserved headroom.
    """
    requested, cached = max(0, int(requested)), max(0, int(cached))
    memory = system_memory() if memory is None else memory
    if not memory:
        return requested
    total = int(memory['total_physical_bytes'])
    if total <= 0:
        return requested
    details = memory_details(memory=memory)
    physical = int(memory['free_physical_bytes']) - details['physical_reserve_bytes']
    commit = int(memory['free_commit_bytes']) - details['commit_reserve_bytes']
    return max(0, min(requested, total // 4, cached + min(physical, commit)))


def memory_headroom_bytes(*, memory=None):
    """RAM and commit available after the system/model/active-frame reserve."""
    return memory_details(memory=memory)['available_bytes']


def bounded_frame_count(requested, frame_bytes):
    """Let a read-ahead/slot queue consume at most 1/4 of remaining headroom.

    Zero means even one entry does not fit. The rest remains available to the
    other processing stages; this is an upper bound, not an allocation promise.
    """
    requested = max(1, int(requested))
    headroom = memory_headroom_bytes()
    if headroom is None:
        return requested
    return min(requested, headroom // (4 * max(1, int(frame_bytes))))


def require_memory_headroom(required_bytes, *, pool=None, stage='allocation', cancel=None,
                            recommended_bytes=None):
    """Reject known upcoming frame allocations before creating their owners.

    This checks RAM and commit after the shared system reserve. It does not
    reserve memory globally or change policy when the OS snapshot is unknown.
    """
    required = max(0, int(required_bytes))
    if not required:return
    available = memory_headroom_bytes()
    pool = pool or current_pool(create=False)
    if pool is not None:
        # A check alone grants no allocation promise. Owners use memory_allocation
        # to hold their reservation until their actual allocation has completed.
        with pool.processing(required, stage=stage, cancel=cancel,recommended_bytes=recommended_bytes):
            return
    if available is not None and required > available:
        details = memory_details(required, memory=fresh_system_memory(), stage=stage)
        details['recommended_bytes']=recommended_bytes
        if details['available_bytes'] is not None and required > details['available_bytes']:
            record_memory_event('rejected', **details)
            raise MemoryPressureError(details)


@contextmanager
def memory_allocation(required_bytes, *, pool=None, stage='allocation', cancel=None):
    pool = pool or current_pool()
    if pool is None:
        require_memory_headroom(required_bytes, stage=stage, cancel=cancel)
        yield None
    else:
        with pool.processing(required_bytes, stage=stage, cancel=cancel) as ticket:
            try:
                yield ticket
            except MemoryError as error:
                if not isinstance(error,MemoryPressureError):
                    record_memory_event('allocation_failed',**memory_details(required_bytes,
                        memory=fresh_system_memory(),stage=stage))
                raise


def receive_worker_command(connection, frame_bytes, *, allocation_bytes=None):
    """Keep disposable GPU workers within the reserve while active or idle.

    The caller handles MemoryError by reporting failure and retiring the native
    owner. Stop commands remain possible under pressure. No model is restarted
    automatically and no temporal history or processing quality is changed.
    """
    required = max(1, int(frame_bytes)) * 2 if allocation_bytes is None else max(0,int(allocation_bytes))
    pressure_since = None
    while not connection.poll(.5):
        # Idle work has no upcoming two-frame allocation. Coordinate reclaim at
        # the normal line, retaining prompt retirement at the critical line.
        details = memory_details(required, stage='worker_idle')
        if details['available_bytes'] is None or details['available_bytes'] >= required:
            pressure_since = None
            continue
        pool = current_pool(create=False)
        if pool is not None:
            pool.request_reclaim(required)
        critical = memory_details(1, memory=details['memory'], stage='worker_idle', critical=True)
        if critical['available_bytes'] == 0:
            record_memory_event('idle_critical', **critical)
            raise MemoryPressureError(critical)
        pressure_since = pressure_since or time.monotonic()
        if time.monotonic() - pressure_since >= 5.0:
            details['failure'] = 'idle'
            record_memory_event('idle_retired', **details)
            raise MemoryPressureError(details)
    message = connection.recv()
    if message.get('operation') not in ('stop', 'close'):
        if not required:
            critical=memory_details(1,memory=fresh_system_memory(),stage='worker_command',critical=True)
            if critical['available_bytes']==0:
                record_memory_event('active_critical',**critical)
                raise MemoryPressureError(critical)
        require_memory_headroom(required, stage='worker_command')
    return message
