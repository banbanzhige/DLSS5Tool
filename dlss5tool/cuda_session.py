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
        self._buffer_sizes={}
        self._retained_external=[]

    def retain_external_cleanup(self,pointer,external,free_fn,destroy_fn):
        """Adopt unreleased external handles from a failed initial import for retry."""
        if getattr(pointer,'value',0) or getattr(external,'value',None):
            self.__dict__.setdefault('_retained_external',[]).append(
                [pointer,external,free_fn,destroy_fn],
            )

    def _check(self):
        if self._closed or threading.get_ident()!=self._thread:raise RuntimeError('CUDA session closed or accessed from wrong thread')

    @contextlib.contextmanager
    def current(self):
        self._check();check(self._push(self.context),'CUDA push')
        body_error=None
        try:yield
        except BaseException as error:
            body_error=error
            raise
        finally:
            popped=C.c_void_p()
            status=self._pop(C.byref(popped))
            if status:
                if body_error is not None:
                    if hasattr(body_error,'add_note'):
                        body_error.add_note(f'CUDA pop: CUDA error {status}')
                else:
                    check(status,'CUDA pop')

    def allocate(self,size):
        if type(size) is not int or size<=0:raise ValueError('Positive allocation required')
        with self.current():
            pointer=C.c_uint64();check(self._alloc(C.byref(pointer),size),'CUDA allocate')
            self._buffers.add(pointer.value)
            self._buffer_sizes[pointer.value]=size
        return pointer.value

    def free(self,pointer):
        """Release only a buffer allocated by this session, on its owner thread."""
        self._check()
        if pointer not in self._buffers:raise ValueError('CUDA buffer is not owned by this session')
        with self.current():
            check(self._free(pointer),'CUDA free')
            self._buffers.remove(pointer)
            self._buffer_sizes.pop(pointer,None)

    def upload(self,pointer,array):
        with self.current():check(self._upload(pointer,array.ctypes.data,array.nbytes),'CUDA upload')

    def synchronize(self):
        """Complete producer work before publishing a cross-stream frame."""
        with self.current():check(bind(self.cuda,'cuCtxSynchronize',[])(),'CUDA producer completion')

    def download(self,pointer,size):
        data=C.create_string_buffer(size)
        with self.current():check(self._download(data,pointer,size),'CUDA download')
        return data.raw

    def close(self):
        if self._closed:return
        self._check();errors=[]
        retained=self.__dict__.setdefault('_retained_external',[])
        if self._buffers or retained:
            pop_error=None
            try:
                with self.current():
                    for pointer in list(self._buffers):
                        status=self._free(pointer)
                        if status:errors.append(status)
                        else:
                            self._buffers.discard(pointer)
                            self._buffer_sizes.pop(pointer,None)
                    still_retained=[]
                    for entry in retained:
                        pointer,external,free_fn,destroy_fn=entry
                        if getattr(pointer,'value',0):
                            status=free_fn(pointer)
                            if status:errors.append(status)
                            else:entry[0]=C.c_uint64()
                        if getattr(external,'value',None):
                            status=destroy_fn(external)
                            if status:errors.append(status)
                            else:entry[1]=C.c_void_p()
                        if getattr(entry[0],'value',0) or getattr(entry[1],'value',None):
                            still_retained.append(entry)
                    retained[:]=still_retained
            except Exception as error:
                pop_error=error
            if errors:
                err=RuntimeError(f'CUDA session cleanup failed: {errors}')
                if pop_error is not None and hasattr(err,'add_note'):
                    err.add_note(str(pop_error))
                raise err
            if pop_error is not None:
                raise pop_error
        status=self._release(self.device)
        if status:raise RuntimeError(f'CUDA session cleanup failed: {[status]}')
        self._closed=True

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
