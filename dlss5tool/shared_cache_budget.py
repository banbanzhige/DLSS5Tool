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
import uuid

MAGIC = b'D5CACHE2'
SIZE = 4096
HEADER = 32
RECORD = struct.Struct('<QQQ')  # pid, cached bytes, minimum pending frame demand
SLOTS = 64


class SharedCacheBudget:
    def __init__(self, name=None, limit_bytes=0):
        self.mapping=None
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
            self.mapping=mmap.mmap(-1,SIZE,tagname=self.name)
            self.slot=None
            with self.locked():
                if name is None:
                    self.mapping[:]=bytes(SIZE)
                    self.mapping[:8]=MAGIC
                    struct.pack_into('<Q',self.mapping,8,max(0,int(limit_bytes)))
                elif self.mapping[:8]!=MAGIC:
                    raise ValueError('Cache pool is not initialized')
                self.reap_locked()
                for slot in range(SLOTS):
                    if not self.read(slot)[0]:
                        self.slot=slot;self.publish_locked(0);break
                if self.slot is None:raise RuntimeError('No free cache leases')
        except Exception:
            if self.mapping:self.mapping.close();self.mapping=None
            self._close(self._mutex);self._mutex=None
            raise

    @contextmanager
    def locked(self):
        status=self._wait(self._mutex,5000)
        if status not in (0,0x80):raise RuntimeError('Cache accounting lock unavailable')
        try:yield self
        finally:self._release(self._mutex)

    @property
    def limit_bytes(self):return struct.unpack_from('<Q',self.mapping,8)[0]

    @property
    def generation(self):return struct.unpack_from('<Q',self.mapping,16)[0]

    def invalidate_guidance(self):
        with self.locked():struct.pack_into('<Q',self.mapping,16,self.generation+1)

    def read(self,slot):return RECORD.unpack_from(self.mapping,HEADER+slot*RECORD.size)

    def publish_locked(self,size,demand=0):
        RECORD.pack_into(self.mapping,HEADER+self.slot*RECORD.size,os.getpid(),max(0,int(size)),max(0,int(demand)))

    def allowance_locked(self, respect_demand=False):
        other=sum(max(size,demand) if respect_demand else size for i in range(SLOTS)
                  for pid,size,demand in [self.read(i)] if pid and i!=self.slot)
        return max(0,self.limit_bytes-other)

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
            if dead:RECORD.pack_into(self.mapping,HEADER+i*RECORD.size,0,0,0)

    def snapshot(self):
        with self.locked():
            self.reap_locked()
            used=sum(self.read(i)[1] for i in range(SLOTS))
            own=self.read(self.slot)[1]
            return {'limit_bytes':self.limit_bytes,'used_bytes':used,'other_bytes':used-own}

    def set_limit(self,size):
        with self.locked():struct.pack_into('<Q',self.mapping,8,max(0,int(size)))

    def close(self):
        if self.mapping is None:return
        with self.locked():
            if self.slot is not None:RECORD.pack_into(self.mapping,HEADER+self.slot*RECORD.size,0,0,0)
        self.mapping.close();self.mapping=None
        self._close(self._mutex);self._mutex=None

    def __del__(self):
        try:self.close()
        except Exception:pass
