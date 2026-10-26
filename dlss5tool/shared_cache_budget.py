"""Windows session-local cache accounting shared by GUI and model processes.

Only a tiny ledger is shared, not images. A named mutex protects reservations;
no inference, IPC or hashing may run while holding it. Each cache
owns a lease, so worker replacement cannot overwrite another worker's usage.
Windows frees the mapping/mutex after the last handle closes.
"""
from contextlib import contextmanager
import ctypes as C
import mmap
import os
import struct
import threading
import time
import uuid
import weakref
from dlss5tool.memory_budget import cache_limit_bytes

MAGIC = b'D5CACHE3'
SIZE = 8192
HEADER = 128
CACHE_MAGIC = b'D5CACHE2'
CACHE_SIZE = 4096
CACHE_HEADER = 32
CACHE_RECORD = struct.Struct('<3Q')
# pid, cached, cache demand, pending processing, kind/state, ack, order, created
RECORD = struct.Struct('<8Q')
SLOTS = 64
PROCESSING = 1
ADMITTED = 2
BACKGROUND = 4
_leases = weakref.WeakSet()
_service_lock = threading.Lock()
_wake = threading.Event()
_service_thread = None


def service_reclaims():
    """Callbacks execute without the ledger mutex and may decline if busy."""
    with _service_lock:
        leases = list(_leases)
    for lease in leases:
        try:
            lease.service_reclaim()
        except Exception:
            # A busy/closing cache cannot acknowledge this generation. The
            # allocation's bounded wait reports a timeout if it cannot recover.
            continue


def _service_loop():
    while True:
        _wake.wait(.05)
        _wake.clear()
        service_reclaims()


def _register(lease):
    global _service_thread
    with _service_lock:
        _leases.add(lease)
        if _service_thread is None:
            _service_thread = threading.Thread(target=_service_loop,
                name='memory-cache-reclaim', daemon=True)
            _service_thread.start()


class SharedCacheBudget:
    def __init__(self, name=None, limit_bytes=0, *, _processing=False):
        self._local_lock = threading.RLock()
        self._reclaim_lock = threading.Lock()
        self._reclaimer = None
        self.mapping=None
        self.cache_mapping=None
        self._mutex=None
        self.name = name or ('Local\\DLSS5-cache-' + uuid.uuid4().hex)
        if not self.name.startswith('Local\\DLSS5-cache-') or len(self.name)>100:
            raise ValueError('Invalid cache pool name')
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        def bind(name,result,*args):
            fn=getattr(self.kernel,name);fn.restype=result;fn.argtypes=list(args);return fn
        self._wait=bind('WaitForSingleObject',C.c_uint32,C.c_void_p,C.c_uint32)
        self._release=bind('ReleaseMutex',C.c_int,C.c_void_p)
        self._close=bind('CloseHandle',C.c_int,C.c_void_p)
        self._open_process=bind('OpenProcess',C.c_void_p,C.c_uint32,C.c_int,C.c_uint32)
        self._mutex=bind('CreateMutexW',C.c_void_p,C.c_void_p,C.c_int,C.c_wchar_p)(None,False,self.name+'-lock')
        if not self._mutex:raise C.WinError(C.get_last_error())
        try:
            # Frozen model workers still understand D5CACHE2. Keep that mapping
            # byte-for-byte compatible; new processing/ack data lives separately.
            self.cache_mapping=mmap.mmap(-1,CACHE_SIZE,tagname=self.name)
            self.mapping=mmap.mmap(-1,SIZE,tagname=self.name+'-processing-v3')
            self.slot=None
            with self.locked():
                if name is None:
                    self.mapping[:]=bytes(SIZE)
                    self.mapping[:8]=MAGIC
                    struct.pack_into('<Q',self.mapping,8,max(0,int(limit_bytes)))
                    self.cache_mapping[:]=bytes(CACHE_SIZE)
                    self.cache_mapping[:8]=CACHE_MAGIC
                    struct.pack_into('<Q',self.cache_mapping,8,max(0,int(limit_bytes)))
                elif self.mapping[:8]!=MAGIC or self.cache_mapping[:8]!=CACHE_MAGIC:
                    raise ValueError('Cache pool is not initialized')
                self.reap_locked()
                for slot in range(SLOTS):
                    if not self.read(slot)[0]:
                        self.slot=slot
                        RECORD.pack_into(self.mapping,HEADER+slot*RECORD.size,
                            os.getpid(),0,0,0,PROCESSING if _processing else 0,
                            self.reclaim_epoch,0,time.monotonic_ns())
                        CACHE_RECORD.pack_into(self.cache_mapping,CACHE_HEADER+slot*CACHE_RECORD.size,
                                               os.getpid(),0,0)
                        break
                if self.slot is None:raise RuntimeError('No free cache leases')
        except Exception:
            if self.mapping:self.mapping.close();self.mapping=None
            if self.cache_mapping:self.cache_mapping.close();self.cache_mapping=None
            self._close(self._mutex);self._mutex=None
            raise

    @contextmanager
    def locked(self):
        with self._local_lock:
            if self.mapping is None:
                raise RuntimeError('Cache accounting is closed')
            status=self._wait(self._mutex,5000)
            if status not in (0,0x80):raise RuntimeError('Cache accounting lock unavailable')
            try:yield self
            finally:self._release(self._mutex)

    @property
    def limit_bytes(self):return struct.unpack_from('<Q',self.mapping,8)[0]

    @property
    def generation(self):return struct.unpack_from('<Q',self.mapping,16)[0]

    def invalidate_guidance(self):
        with self.locked():
            value=self.generation+1
            struct.pack_into('<Q',self.mapping,16,value)
            struct.pack_into('<Q',self.cache_mapping,16,value)

    @property
    def reclaim_epoch(self):return struct.unpack_from('<Q',self.mapping,24)[0]

    def _record(self,slot):
        cached=CACHE_RECORD.unpack_from(self.cache_mapping,CACHE_HEADER+slot*CACHE_RECORD.size)
        extra=RECORD.unpack_from(self.mapping,HEADER+slot*RECORD.size)
        return (*cached,*extra[3:]) if cached[0]==extra[0] else (*cached,0,0,0,0,0)

    def _has_metadata(self,slot):
        return self._record(slot)[0]==RECORD.unpack_from(self.mapping,HEADER+slot*RECORD.size)[0]

    def _write(self,slot,**changes):
        values=list(self._record(slot))
        for field,value in changes.items():
            values[('pid','cached','demand','pending','kind','ack','order','created').index(field)]=max(0,int(value))
        RECORD.pack_into(self.mapping,HEADER+slot*RECORD.size,*values)
        CACHE_RECORD.pack_into(self.cache_mapping,CACHE_HEADER+slot*CACHE_RECORD.size,*values[:3])

    def _clear_slot(self,slot):
        RECORD.pack_into(self.mapping,HEADER+slot*RECORD.size,*([0]*8))
        CACHE_RECORD.pack_into(self.cache_mapping,CACHE_HEADER+slot*CACHE_RECORD.size,0,0,0)

    def read(self,slot):return self._record(slot)[:3]

    def publish_locked(self,size,demand=0):
        self._write(self.slot,cached=size,demand=demand)

    def _processing_locked(self):
        records=[(i,self._record(i)) for i in range(SLOTS)
                 if self._record(i)[0] and self._record(i)[4]&PROCESSING]
        reserved=sum(r[3] for _,r in records if r[4]&ADMITTED)
        waiting=sorted((((bool(r[4]&BACKGROUND),r[6]),i,r[3])
                        for i,r in records if r[3] and not r[4]&ADMITTED))
        return reserved,waiting

    def _limit_locked(self):
        from dlss5tool.memory_budget import RECOVERY_MARGIN, memory_headroom_bytes
        used=sum(self.read(i)[1] for i in range(SLOTS))
        limit=cache_limit_bytes(self.limit_bytes,used)
        reserved,waiting=self._processing_locked()
        demand=reserved+(waiting[0][2] if waiting else 0)
        if demand:
            limit=max(0,limit-demand-RECOVERY_MARGIN)
            struct.pack_into('<Q',self.cache_mapping,8,limit)
            return limit
        target,until=struct.unpack_from('<QQ',self.mapping,32)
        if until:
            headroom=memory_headroom_bytes()
            if time.monotonic_ns()>=until and (headroom is None or headroom>=2*RECOVERY_MARGIN):
                struct.pack_into('<QQ',self.mapping,32,0,0)
            else:
                limit=min(limit,target)
        struct.pack_into('<Q',self.cache_mapping,8,limit)
        return limit

    def allowance_locked(self, respect_demand=False):
        other=sum(max(size,demand) if respect_demand else size for i in range(SLOTS)
                  for pid,size,demand in [self.read(i)] if pid and i!=self.slot)
        return max(0,self._limit_locked()-other)

    def set_reclaimer(self,callback):
        self._reclaimer=weakref.WeakMethod(callback) if getattr(callback,'__self__',None) else callback
        _register(self)

    def service_reclaim(self):
        if not self._reclaim_lock.acquire(blocking=False):return
        try:self._service_reclaim()
        finally:self._reclaim_lock.release()

    def _service_reclaim(self):
        callback=self._reclaimer
        if callback is None or self.mapping is None:return
        if isinstance(callback,weakref.WeakMethod):callback=callback()
        if callback is None:return
        with self.locked():
            epoch=self.reclaim_epoch
            if self._record(self.slot)[5]>=epoch:return
        if callback() is False:return
        with self.locked():
            self._write(self.slot,ack=epoch)

    def _request_locked(self,required):
        from dlss5tool.memory_budget import RECOVERY_MARGIN
        used=sum(self.read(i)[1] for i in range(SLOTS))
        target=max(0,cache_limit_bytes(self.limit_bytes,used)-int(required)-RECOVERY_MARGIN)
        old_target,until=struct.unpack_from('<QQ',self.mapping,32)
        target=min(target,old_target) if until else target
        # A new generation only when asking for additional shrink, or when no
        # request is active. Repeated pollers must not invalidate every ack.
        if not until or target<old_target:
            struct.pack_into('<Q',self.mapping,24,self.reclaim_epoch+1)
        struct.pack_into('<QQ',self.mapping,32,target,time.monotonic_ns()+500_000_000)
        struct.pack_into('<Q',self.cache_mapping,8,target)
        _wake.set()
        return self.reclaim_epoch

    def request_reclaim(self,required):
        with self.locked():return self._request_locked(required)

    @property
    def under_pressure(self):
        with self.locked():
            _,waiting=self._processing_locked()
            self._limit_locked()  # evaluate the recovery hysteresis
            # Admitted allocations already reduce the cache allowance. Pausing
            # for every healthy decode/readback reservation serializes the
            # pipeline; only blocked allocations or active reclaim need a pause.
            return bool(waiting or struct.unpack_from('<Q',self.mapping,40)[0])

    @contextmanager
    def processing(self,required,*,stage='allocation',cancel=None,timeout=None,recommended_bytes=None):
        from dlss5tool import memory_budget as mb
        required=max(0,int(required))
        ticket=SharedCacheBudget(self.name,_processing=True)
        started=time.monotonic()
        deadline=started+(mb.RECLAIM_TIMEOUT if timeout is None else max(0,float(timeout)))
        epoch=None
        try:
            with ticket.locked():
                ticket._limit_locked()
                order=struct.unpack_from('<Q',self.mapping,48)[0]+1
                struct.pack_into('<Q',self.mapping,48,order)
                ticket._write(ticket.slot,pending=required,order=order,
                              kind=PROCESSING|(BACKGROUND if not mb.processing_priority() else 0))
            while True:
                if cancel is not None:
                    if callable(cancel):cancel()
                    elif cancel.is_set():raise InterruptedError('Memory allocation cancelled')
                with ticket.locked():
                    ticket.reap_locked()
                    reserved,waiting=ticket._processing_locked()
                    details=mb.memory_details(required,memory=mb.fresh_system_memory(),stage=stage)
                    details['recommended_bytes']=recommended_bytes
                    available=details['available_bytes']
                    turn=not required or not waiting or waiting[0][1]==ticket.slot
                    cache_total=sum(ticket.read(i)[1] for i in range(SLOTS))
                    cache_target=struct.unpack_from('<Q',ticket.mapping,32)[0]
                    acknowledgements=all(not r[0] or not r[1] or r[4]&PROCESSING or
                        (r[5]>=epoch if ticket._has_metadata(i) else cache_total<=cache_target)
                        for i,r in ((i,ticket._record(i)) for i in range(SLOTS))) if epoch is not None else True
                    if turn and acknowledgements and (not required or available is None or required+reserved<=available):
                        ticket._write(ticket.slot,kind=PROCESSING|ADMITTED)
                        break
                    if epoch is None:
                        epoch=ticket._request_locked(required+reserved)
                    details.update(reserved_processing_bytes=reserved,reclaim_epoch=epoch,
                        cache_bytes=sum(ticket.read(i)[1] for i in range(SLOTS)),
                        reclaim_acknowledged=acknowledgements)
                    no_reclaim_possible=not details['cache_bytes'] and not reserved and turn
                service_reclaims()
                if no_reclaim_possible or time.monotonic()>=deadline:
                    if not acknowledgements:details['failure']='reclaim_timeout'
                    elif not turn or (available is not None and required<=available<required+reserved):details['failure']='busy'
                    mb.record_memory_event('rejected',**details)
                    raise mb.MemoryPressureError(details)
                time.sleep(.025)
            if epoch is not None:
                mb.record_memory_event('reclaimed',elapsed_seconds=time.monotonic()-started,**details)
            yield ticket
        finally:
            if not getattr(ticket,'_deferred_release',False):ticket.close()

    def defer_until_retired(self,owner):
        """Keep future-allocation credit while a failed startup still owns work."""
        self._deferred_release=True
        def retire():
            from dlss5tool.memory_budget import record_memory_event
            while True:
                process=getattr(owner,'_process',None)
                try:alive=process is not None and process.is_alive()
                except ValueError:alive=False  # handle closed only after joining
                if alive:
                    time.sleep(.1)
                    continue
                try:owner.close()
                except Exception:
                    time.sleep(.1)
                    continue
                self.close()
                record_memory_event('failed_owner_retired')
                return
        threading.Thread(target=retire,name='memory-owner-retire',daemon=True).start()

    def consume(self,size):
        """Allocated bytes are already in the OS snapshot, not pending again."""
        with self.locked():
            self._write(self.slot,pending=max(0,self._record(self.slot)[3]-max(0,int(size))))

    def reap_locked(self):
        for i in range(SLOTS):
            pid,_,_=self.read(i)
            if not pid or pid==os.getpid():continue
            handle=self._open_process(0x00100000,False,pid) # SYNCHRONIZE only
            if handle:
                dead=self._wait(handle,0)==0
                self._close(handle)
            else:
                # Access denied is not proof of death; ERROR_INVALID_PARAMETER is.
                dead=C.get_last_error()==87
            if dead:self._clear_slot(i)

    def snapshot(self):
        with self.locked():
            self.reap_locked()
            used=sum(self.read(i)[1] for i in range(SLOTS))
            own=self.read(self.slot)[1]
            return {'limit_bytes':self.limit_bytes,'used_bytes':used,'other_bytes':used-own,
                    'effective_limit_bytes':self._limit_locked(),
                    'reserved_processing_bytes':self._processing_locked()[0],
                    'waiting_processing_bytes':sum(r[2] for r in self._processing_locked()[1]),
                    'reclaim_epoch':self.reclaim_epoch}

    def set_limit(self,size):
        with self.locked():
            struct.pack_into('<Q',self.mapping,8,max(0,int(size)))
            self._limit_locked()

    def close(self):
        with self._local_lock:
            if self.mapping is None:return
            with self.locked():
                if self.slot is not None:self._clear_slot(self.slot)
            self.mapping.close();self.mapping=None
            self.cache_mapping.close();self.cache_mapping=None
            self._close(self._mutex);self._mutex=None

    def __del__(self):
        try:self.close()
        except Exception:pass
