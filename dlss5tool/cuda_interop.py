"""Scoped import of one validated D3D12 shared buffer into a CUDA session."""
import ctypes as C

from dlss5tool.cuda_session import bind,check


class _WinHandle(C.Structure):
    _fields_=[('handle',C.c_void_p),('name',C.c_void_p)]


class _Handle(C.Union):
    _fields_=[('fd',C.c_int),('win32',_WinHandle),('nvSciBufObject',C.c_void_p)]


class _MemoryDesc(C.Structure):
    _fields_=[('type',C.c_int),('handle',_Handle),('size',C.c_uint64),('flags',C.c_uint),('reserved',C.c_uint*16)]


class _BufferDesc(C.Structure):
    _fields_=[('offset',C.c_uint64),('size',C.c_uint64),('flags',C.c_uint),('reserved',C.c_uint*16)]


class SharedCudaBuffer:
    def __init__(self,session,producer_pid,handle,allocation,size,producer_luid):
        if producer_luid!=session.luid:raise ValueError('D3D12/CUDA physical GPU mismatch')
        if any(type(v) is not int or v<=0 for v in (producer_pid,handle,allocation,size)) or size>allocation:
            raise ValueError('Invalid shared buffer descriptor')
        self.session=session;self.pointer=C.c_uint64();self.external=C.c_void_p();self.size=size
        self.producer_pid,self.handle=producer_pid,handle
        self._image_owners=0;self._failed=False
        self._free=bind(session.cuda,'cuMemFree_v2',[C.c_uint64])
        self._destroy=bind(session.cuda,'cuDestroyExternalMemory',[C.c_void_p])
        imp=bind(session.cuda,'cuImportExternalMemory',[C.POINTER(C.c_void_p),C.POINTER(_MemoryDesc)])
        mapping=bind(session.cuda,'cuExternalMemoryGetMappedBuffer',[C.POINTER(C.c_uint64),C.c_void_p,C.POINTER(_BufferDesc)])
        kernel=C.WinDLL('kernel32',use_last_error=True)
        op=bind(kernel,'OpenProcess',[C.c_uint,C.c_int,C.c_uint],C.c_void_p)
        close=bind(kernel,'CloseHandle',[C.c_void_p])
        dup=bind(kernel,'DuplicateHandle',[C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p),C.c_uint,C.c_int,C.c_uint])
        current=bind(kernel,'GetCurrentProcess',[],C.c_void_p)()
        process=op(0x40,0,producer_pid);copied=C.c_void_p()
        if not process:raise C.WinError(C.get_last_error())
        try:
            if not dup(process,handle,current,C.byref(copied),0,0,2):raise C.WinError(C.get_last_error())
            with session.current():
                descriptor=_MemoryDesc(type=5,size=allocation,flags=1)
                descriptor.handle.win32.handle=copied.value
                check(imp(C.byref(self.external),C.byref(descriptor)),'CUDA import D3D12')
                check(mapping(C.byref(self.pointer),self.external,C.byref(_BufferDesc(size=size))),'CUDA map D3D12')
        except BaseException:
            self.close();raise
        finally:
            if copied:close(copied)
            close(process)

    def copy_from(self,frame,destination_pitch):
        """Complete one D2D upload before the D3D input owner is called."""
        from dlss5tool.cuda_frame import ReadyCudaFrame
        from dlss5tool.nvofa import Copy2D
        self.session._check()
        if not isinstance(frame,ReadyCudaFrame):raise TypeError('Completed CUDA frame lease required')
        frame._check()
        row_bytes=frame.width*(8 if frame.layout=='rgba16f' else 4)
        if (self._failed or not self.pointer.value or frame.session is not self.session
                or type(destination_pitch) is not int or destination_pitch<row_bytes
                or destination_pitch*frame.height>self.size):
            raise ValueError('Shared input owner, pitch or allocation mismatch')
        descriptor=Copy2D(stype=2,sdevice=frame.pointer,spitch=frame.row_pitch,
            dtype=2,ddevice=self.pointer.value,dpitch=destination_pitch,width=row_bytes,height=frame.height)
        try:
            with self.session.current():
                check(bind(self.session.cuda,'cuMemcpy2D_v2',[C.POINTER(Copy2D)])(
                    C.byref(descriptor)),'CUDA shared input copy')
            self.session.synchronize()
        except BaseException:
            self._failed=True
            raise

    def close(self):
        errors=[]
        if self._image_owners:raise RuntimeError('Shared GPU buffer still has image owners')
        if not self.pointer.value and not self.external.value:return
        with self.session.current():
            if self.pointer.value:
                errors.append(self._free(self.pointer));self.pointer=C.c_uint64()
            if self.external.value:
                errors.append(self._destroy(self.external));self.external=C.c_void_p()
        if any(errors):raise RuntimeError(f'Shared GPU buffer cleanup failed: {errors}')
