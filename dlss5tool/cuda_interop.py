"""Scoped import of one validated D3D12 shared buffer into a CUDA session."""
import ctypes as C
import weakref

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
        self.session=session;self.pointer=C.c_uint64();self.external=C.c_void_p();self.size=0
        self.producer_pid,self.handle=None,None
        self._image_owners=0;self._failed=False;self._closing=False
        self._images=weakref.WeakSet()
        self._free=bind(session.cuda,'cuMemFree_v2',[C.c_uint64])
        self._destroy=bind(session.cuda,'cuDestroyExternalMemory',[C.c_void_p])
        try:
            pointer,external=self._import_external_memory(producer_pid,handle,allocation,size,producer_luid)
        except BaseException:
            if (self.pointer.value or self.external.value) and hasattr(self.session,'retain_external_cleanup'):
                self.session.retain_external_cleanup(self.pointer,self.external,self._free,self._destroy)
                self.pointer,self.external=C.c_uint64(),C.c_void_p()
            raise
        self.pointer,self.external=pointer,external
        self.size,self.producer_pid,self.handle=size,producer_pid,handle
        from dlss5tool.gpu_reconfigure import register
        register(self)

    def _import_external_memory(self,producer_pid,handle,allocation,size,producer_luid):
        if producer_luid!=self.session.luid:raise ValueError('D3D12/CUDA physical GPU mismatch')
        if any(type(v) is not int or v<=0 for v in (producer_pid,handle,allocation,size)) or size>allocation:
            raise ValueError('Invalid shared buffer descriptor')
        pointer,external=C.c_uint64(),C.c_void_p()
        imp=bind(self.session.cuda,'cuImportExternalMemory',[C.POINTER(C.c_void_p),C.POINTER(_MemoryDesc)])
        mapping=bind(self.session.cuda,'cuExternalMemoryGetMappedBuffer',[C.POINTER(C.c_uint64),C.c_void_p,C.POINTER(_BufferDesc)])
        kernel=C.WinDLL('kernel32',use_last_error=True)
        op=bind(kernel,'OpenProcess',[C.c_uint,C.c_int,C.c_uint],C.c_void_p)
        close=bind(kernel,'CloseHandle',[C.c_void_p])
        dup=bind(kernel,'DuplicateHandle',[C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(C.c_void_p),C.c_uint,C.c_int,C.c_uint])
        current=bind(kernel,'GetCurrentProcess',[],C.c_void_p)()
        process=op(0x40,0,producer_pid);copied=C.c_void_p()
        if not process:raise C.WinError(C.get_last_error())
        try:
            if not dup(process,handle,current,C.byref(copied),0,0,2):raise C.WinError(C.get_last_error())
            with self.session.current():
                descriptor=_MemoryDesc(type=5,size=allocation,flags=1)
                descriptor.handle.win32.handle=copied.value
                check(imp(C.byref(external),C.byref(descriptor)),'CUDA import D3D12')
                check(mapping(C.byref(pointer),external,C.byref(_BufferDesc(size=size))),'CUDA map D3D12')
            return pointer,external
        except BaseException as error:
            if pointer.value or external.value:
                cleanup_errors=[]
                try:
                    with self.session.current():
                        if pointer.value:
                            status=self._free(pointer)
                            if status:cleanup_errors.append(f'cuMemFree_v2={status}')
                            else:pointer=C.c_uint64()
                        if external.value:
                            status=self._destroy(external)
                            if status:cleanup_errors.append(f'cuDestroyExternalMemory={status}')
                            else:external=C.c_void_p()
                except BaseException as cleanup:
                    cleanup_errors.append(str(cleanup) or repr(cleanup))
                if pointer.value or external.value:
                    self._failed=True
                    if pointer.value and not self.pointer.value:self.pointer=pointer
                    if external.value and not self.external.value:self.external=external
                if cleanup_errors and hasattr(error,'add_note'):
                    error.add_note('CUDA import cleanup failed: '+', '.join(cleanup_errors))
            raise
        finally:
            if copied:close(copied)
            close(process)

    def check_reconfigure(self):
        self.session._check()
        if (getattr(self, '_closing', False) or self._failed
                or not self.pointer.value or not getattr(self.external, 'value', None)):
            raise RuntimeError('Shared GPU mapping is unavailable')
        images = list(self._images)
        if len(images) != self._image_owners or any(image._leases for image in images):
            raise RuntimeError('Finish GPU frame consumers before reconfiguration')

    def suspend_reconfigure(self):
        self.check_reconfigure()
        self.session.synchronize()
        for image in self._images:
            image._suspended = True
            image._ready = False
            image._generation += 1
        try:
            with self.session.current():
                if self.pointer.value:
                    check(self._free(self.pointer), 'CUDA reconfiguration unmap')
                    self.pointer = C.c_uint64()
                if self.external.value:
                    check(self._destroy(self.external), 'CUDA reconfiguration release')
                    self.external = C.c_void_p()
        except BaseException:
            self._failed = True
            raise

    def resume_reconfigure(self, descriptor, width, height, layout, profile):
        from dlss5tool.gpu_reconfigure import register, unregister
        if getattr(self, '_closing', False):
            raise RuntimeError('Shared GPU mapping is closing')
        if self.pointer.value or self.external.value:
            raise RuntimeError('Old GPU mapping was not retired')
        values = dict(descriptor)
        pitch = values.pop('pitch')
        pointer, external = self._import_external_memory(**values)
        unregister(self)
        self.pointer, self.external = pointer, external
        self.size = values['size']
        self.producer_pid = values['producer_pid']
        self.handle = values['handle']
        self._failed = False
        register(self)
        for image in list(self._images):
            image.rebind(width, height, pitch, layout=layout, color_profile=profile)

    def copy_from(self,frame,destination_pitch):
        """Complete one D2D upload before the D3D input owner is called."""
        from dlss5tool.cuda_frame import ReadyCudaFrame
        from dlss5tool.nvofa import Copy2D
        self.session._check()
        if not isinstance(frame,ReadyCudaFrame):raise TypeError('Completed CUDA frame lease required')
        frame._check()
        row_bytes=frame.width*(8 if frame.layout=='rgba16f' else 4)
        if (getattr(self,'_closing',False) or self._failed or not self.pointer.value
                or not getattr(self.external,'value',None) or frame.session is not self.session
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
        has_handles=bool(self.pointer.value or self.external.value)
        if has_handles or not getattr(self,'_closing',False):
            # Validate owning thread and active session before mutating closing
            # state so a wrong-thread close() cannot poison a live mapping.
            self.session._check()
        # Once closing begins on the owner, reject all future consumption, leasing
        # or rebinding, while keeping registry tracking until both handles release.
        self._closing=True
        self._failed=True
        from dlss5tool.gpu_reconfigure import unregister
        if not has_handles:
            unregister(self)
            return
        pop_error=None
        try:
            with self.session.current():
                if self.pointer.value:
                    status=self._free(self.pointer)
                    errors.append(status)
                    if not status:self.pointer=C.c_uint64()
                if self.external.value:
                    status=self._destroy(self.external)
                    errors.append(status)
                    if not status:self.external=C.c_void_p()
        except Exception as error:
            pop_error=error
        if not self.pointer.value and not self.external.value:
            unregister(self)
        if any(errors):
            err=RuntimeError(f'Shared GPU buffer cleanup failed: {errors}')
            if pop_error is not None and hasattr(err,'add_note'):
                err.add_note(str(pop_error))
            raise err
        if pop_error is not None:
            raise pop_error
