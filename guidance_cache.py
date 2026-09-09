"""Bounded, session-local raw guidance cache. No Torch or GPU ownership.

The owning Models instance fixes weights, precision, direction and preprocessing.
Changing that configuration creates a new instance/cache. Keys identify actual
RGBA bytes, not paths/frame numbers. Store RAW depth, never temporal normalization.
"""
from collections import OrderedDict
from contextlib import nullcontext
import hashlib
import numpy as np

CACHE_VERSION = 'raw_lru_v2_shared'


def cache_budget_mib(settings):
    """Standalone default 1 GiB; no independent cap within a shared GUI pool."""
    try:
        return max(0, int(settings.get('guidance_cache_mb', 1024)))
    except (TypeError, ValueError, OverflowError):
        return 1024


def frame_digest(frame):
    digest = hashlib.sha256()
    digest.update(str((frame.shape, frame.dtype.str)).encode('ascii'))
    digest.update(memoryview(np.ascontiguousarray(frame)))
    return digest.digest()


class RawGuidanceCache:
    def __init__(self, limit_bytes, pool=None):
        self.pool = pool
        self.generation = pool.generation if pool else 0
        self.pending_bytes = 0
        self.limit_bytes = max(0, int(limit_bytes))
        self.bytes = 0
        self.hits = self.misses = self.evictions = 0
        self._items = OrderedDict()

    def get(self, key):
        value = self._items.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
            self._items.move_to_end(key)
        return value

    def put(self, key, value):
        with self.pool.locked() if self.pool else nullcontext():
            if self.pool:self.limit_bytes = self.pool.allowance_locked(respect_demand=True)
            self._trim()
            if self.pool and value.nbytes > self.limit_bytes and value.nbytes <= self.pool.limit_bytes:
                self.pending_bytes = value.nbytes
            self._put(key,value)
            if self.pool:
                if self.bytes >= self.pending_bytes:self.pending_bytes = 0
                self.pool.publish_locked(self.bytes,self.pending_bytes)

    def _put(self, key, value):
        if not self.limit_bytes or value.nbytes > self.limit_bytes:
            return
        # Inputs can alias GPU readbacks or caller buffers; own one immutable copy.
        if value.dtype != np.float32 or not np.isfinite(value).all():
            raise ValueError('Guidance cache accepts finite float32 predictions only')
        old = self._items.pop(key, None)
        if old is not None:
            self.bytes -= old.nbytes
        while self._items and self.bytes + value.nbytes > self.limit_bytes:
            _, removed = self._items.popitem(last=False)
            self.bytes -= removed.nbytes
            self.evictions += 1
        owned = np.array(value, copy=True, order='C')
        owned.flags.writeable = False
        self._items[key] = owned
        self.bytes += owned.nbytes

    def clear(self):
        self._items.clear()
        self.bytes = 0
        self.pending_bytes = 0
        if self.pool:
            with self.pool.locked():self.pool.publish_locked(0)

    def _trim(self):
        while self._items and self.bytes > self.limit_bytes:
            _,value=self._items.popitem(last=False)
            self.bytes-=value.nbytes;self.evictions+=1

    def trim(self):
        if self.pool:
            with self.pool.locked():
                self.pool.reap_locked()
                if self.generation != self.pool.generation:
                    self._items.clear();self.bytes=0;self.pending_bytes=0
                    self.generation=self.pool.generation
                if self.pending_bytes > self.pool.limit_bytes:self.pending_bytes=0
                self.limit_bytes=self.pool.allowance_locked(respect_demand=True)
                self._trim();self.pool.publish_locked(self.bytes,self.pending_bytes)

    def close(self):
        self.clear()
        if self.pool:self.pool.close();self.pool=None

    def metrics(self):
        return {'cache_version': CACHE_VERSION, 'cache_bytes': self.bytes,
                'cache_limit_bytes': self.limit_bytes, 'cache_entries': len(self._items),
                'cache_hits': self.hits, 'cache_misses': self.misses, 'cache_evictions': self.evictions}
