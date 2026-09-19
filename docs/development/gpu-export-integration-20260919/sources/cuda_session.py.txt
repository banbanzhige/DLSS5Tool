"""Small thread-owned CUDA driver session; no Torch dependency or global context."""
import contextlib
import ctypes as C
import threading


def bind(lib,name,args,result=C.c_int):
    fn=getattr(lib,name);fn.argtypes=args;fn.restype=result;return fn


def check(status,label):
    if status:raise RuntimeError(f'{label}: CUDA error {status}')


class CudaSession:
    def __init__(self, device_ordinal=0):
        if type(device_ordinal) is not int or device_ordinal<0:raise ValueError('CUDA device ordinal required')
        self._thread=threading.get_ident();self._closed=False
        self.cuda=C.WinDLL('nvcuda.dll',winmode=0x800)
        p,u,i=C.c_void_p,C.c_uint,C.c_int
        check(bind(self.cuda,'cuInit',[u])(0),'CUDA init')
        self.device=i();self.context=p()
        check(bind(self.cuda,'cuDeviceGet',[C.POINTER(i),i])(C.byref(self.device),device_ordinal),'CUDA device')
        self._push=bind(self.cuda,'cuCtxPushCurrent_v2',[p]);self._pop=bind(self.cuda,'cuCtxPopCurrent_v2',[C.POINTER(p)])
        self._release=bind(self.cuda,'cuDevicePrimaryCtxRelease_v2',[i])
        self._alloc=bind(self.cuda,'cuMemAlloc_v2',[C.POINTER(C.c_uint64),C.c_size_t])
        self._free=bind(self.cuda,'cuMemFree_v2',[C.c_uint64])
        self._upload=bind(self.cuda,'cuMemcpyHtoD_v2',[C.c_uint64,p,C.c_size_t])
        self._download=bind(self.cuda,'cuMemcpyDtoH_v2',[p,C.c_uint64,C.c_size_t])
        luid=C.create_string_buffer(8);mask=u()
        check(bind(self.cuda,'cuDeviceGetLuid',[p,C.POINTER(u),i])(luid,C.byref(mask),self.device),'CUDA adapter identity')
        if mask.value!=1:raise RuntimeError('Single-node CUDA adapter required')
        self.luid=int.from_bytes(luid.raw,'little')
        check(bind(self.cuda,'cuDevicePrimaryCtxRetain',[C.POINTER(p),i])(C.byref(self.context),self.device),'CUDA retain')
        self._buffers=set()

    def _check(self):
        if self._closed or threading.get_ident()!=self._thread:raise RuntimeError('CUDA session closed or accessed from wrong thread')

    @contextlib.contextmanager
    def current(self):
        self._check();check(self._push(self.context),'CUDA push')
        try:yield
        finally:
            popped=C.c_void_p();check(self._pop(C.byref(popped)),'CUDA pop')

    def allocate(self,size):
        if type(size) is not int or size<=0:raise ValueError('Positive allocation required')
        with self.current():
            pointer=C.c_uint64();check(self._alloc(C.byref(pointer),size),'CUDA allocate')
        self._buffers.add(pointer.value)
        return pointer.value

    def upload(self,pointer,array):
        with self.current():check(self._upload(pointer,array.ctypes.data,array.nbytes),'CUDA upload')

    def download(self,pointer,size):
        data=C.create_string_buffer(size)
        with self.current():check(self._download(data,pointer,size),'CUDA download')
        return data.raw

    def close(self):
        if self._closed:return
        self._check();errors=[]
        with self.current():
            for pointer in self._buffers:errors.append(self._free(pointer))
        self._buffers.clear();errors.append(self._release(self.device));self._closed=True
        if any(errors):raise RuntimeError(f'CUDA session cleanup failed: {errors}')

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
