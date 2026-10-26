"""Explicit synchronous CUDA VSR backend; one owner in an isolated GPU worker.

The caller supplies a verified DLL/runtime pair. This interface neither changes
the default VSR route nor treats device pointers as cross-process resources.
"""
import ctypes as C
import os
from pathlib import Path
import threading

from dlss5tool.cuda_frame import CudaImage, ReadyCudaFrame
from dlss5tool.cuda_session import bind, check
from dlss5tool.memory_budget import memory_allocation
from dlss5tool.super_resolution import validate_dimensions


class CudaVSR:
    @classmethod
    def from_runtime(cls, session, width, height, scale, *, log_path):
        from dlss5tool.cuda_vsr_runtime import load_components, verified_geometry
        if not verified_geometry(width, height, scale):
            raise ValueError('Verified CUDA VSR supports SDR 2x/4x up to 4K output')
        components = load_components()
        if components is None:
            raise RuntimeError('Verified CUDA VSR components are unavailable')
        return cls(session, width, height, scale, library_path=components['native_library'],
            cudart_directory=components['cudart_directory'], runtime_directory=components['runtime_directory'],
            log_path=log_path)

    def __init__(self, session, width, height, scale, *, library_path, cudart_directory,
                 runtime_directory, log_path, quality=4, color_profile='srgb', hdr_ptx=None):
        if any(type(value) is not int for value in (width, height, scale, quality)) or scale not in (2, 4):
            raise ValueError('Integer CUDA VSR geometry and 2x/4x scale required')
        if quality != 4:
            raise ValueError('CUDA VSR currently validates highest quality only')
        if color_profile not in ('srgb', 'hdr10_pq', 'hdr10_hlg'):
            raise ValueError('CUDA VSR requires SDR or transfer-coded PQ/HLG')
        hdr = color_profile != 'srgb'
        if hdr and hdr_ptx is None:
            raise ValueError('HDR VSR requires its verified packing PTX')
        ow, oh = validate_dimensions(width, height, scale)
        session._check()
        self.session, self.width, self.height, self.scale = session, width, height, scale
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._closed, self._failed = False, False
        self._handle, self._stream = C.c_void_p(), C.c_void_p()
        self._output, self._dll_directory = None, None
        self._library = None
        self._hdr_packing = None
        for value in (library_path, cudart_directory, runtime_directory):
            path = Path(value).absolute()
            if path.resolve() != path:
                raise ValueError('CUDA VSR dependency paths must not redirect')
            for parent in (path, *path.parents):
                if parent.exists() and parent.lstat().st_file_attributes & 0x400:
                    raise ValueError('CUDA VSR dependency path contains a reparse point')
        try:
            self._dll_directory = os.add_dll_directory(str(Path(cudart_directory).resolve(strict=True)))
            self._library = C.WinDLL(str(Path(library_path).resolve(strict=True)), winmode=0x1100)
            abi = bind(self._library, 'vsr_cuda_abi', [], C.c_uint)
            if abi() != 1:
                raise RuntimeError('CUDA VSR ABI mismatch')
            if hdr and bind(self._library, 'vsr_cuda_hdr_abi', [], C.c_uint)() != 1:
                raise RuntimeError('CUDA HDR VSR ABI mismatch')
            self._create = bind(self._library, 'vsr_cuda_create_hdr' if hdr else 'vsr_cuda_create',
                [C.c_void_p, C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_int,
                 C.c_wchar_p, C.c_wchar_p, C.POINTER(C.c_void_p)])
            self._process = bind(self._library, 'vsr_cuda_process',
                [C.c_void_p, C.c_uint64, C.c_size_t, C.c_uint64, C.c_size_t])
            self._destroy = bind(self._library, 'vsr_cuda_destroy', [C.c_void_p])
            self._stream_destroy = bind(session.cuda, 'cuStreamDestroy_v2', [C.c_void_p])
            self._stream_sync = bind(session.cuda, 'cuStreamSynchronize', [C.c_void_p])
            with session.current():
                check(bind(session.cuda, 'cuStreamCreate', [C.POINTER(C.c_void_p), C.c_uint])(
                    C.byref(self._stream), 1), 'CUDA VSR stream')
                with memory_allocation((width*height+ow*oh)*4, stage='cuda_vsr_initialize') as ticket:
                    if not self._create(session.context, self._stream, width, height, scale, quality,
                            str(Path(runtime_directory).resolve(strict=True)), str(log_path), C.byref(self._handle)):
                        raise RuntimeError('CUDA VSR initialization failed; inspect native log')
                    if ticket: ticket.consume((width*height+ow*oh)*4)
            self._output = CudaImage(session, ow, oh, layout='rgba16f' if hdr else 'rgba8',
                                     color_profile=color_profile)
            if hdr:
                from dlss5tool.cuda_hdr_pack import CudaHdrPacking
                self._hdr_packing = CudaHdrPacking(session, hdr_ptx, width, height, scale)
        except BaseException as error:
            try: self.close()
            except BaseException as cleanup:
                if hasattr(error, 'add_note'): error.add_note(f'CUDA VSR cleanup: {cleanup}')
            raise

    def _check(self):
        if self._closed or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise RuntimeError('CUDA VSR closed or accessed outside its owning process/thread')
        self.session._check()
        if self._failed:
            raise RuntimeError('CUDA VSR failed; retire this worker')

    def process_device(self, frame):
        self._check()
        if not isinstance(frame, ReadyCudaFrame):
            raise TypeError('Completed CUDA frame lease required')
        frame._check()
        if (frame.session is not self.session or frame.context != self.session.context.value or
                frame.adapter_luid != self.session.luid):
            raise ValueError('CUDA VSR input must belong to the same physical GPU and context owner')
        if (frame.width, frame.height, frame.layout, frame.color_profile) != (
                self.width, self.height, self._output.layout, self._output.color_profile):
            raise ValueError('CUDA VSR input geometry/color/layout mismatch')
        output = self._output.begin_write()
        try:
            with self.session.current():
                packing = getattr(self, '_hdr_packing', None)
                if packing:
                    source, target = packing._buffers
                    packing.convert('vsr_pack', frame.pointer, source, self.width, self.height,
                                    frame.row_pitch, self.width*4)
                    ok = self._process(self._handle, source, self.width*4, target, self.width*self.scale*4)
                    if ok:
                        packing.convert('vsr_unpack', target, output, self.width*self.scale,
                                        self.height*self.scale, self.width*self.scale*4, self._output.row_pitch)
                else:
                    ok = self._process(self._handle, frame.pointer, frame.row_pitch, output, self._output.row_pitch)
                if not ok:
                    raise RuntimeError('CUDA VSR evaluation failed; retire this worker')
        except BaseException:
            self._failed = True
            raise
        return self._output.complete(frame.timing, stream=self._stream.value)

    def close(self):
        if self._closed:
            return
        if os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise RuntimeError('CUDA VSR cleanup must run on its owner')
        if self._output and self._output._leases:
            raise RuntimeError('Cannot free CUDA VSR output held by a consumer')
        if self._stream.value:
            with self.session.current():
                check(self._stream_sync(self._stream), 'CUDA VSR final completion')
        if self._output:
            self._output.close()
            self._output = None
        if getattr(self, '_hdr_packing', None):
            self._hdr_packing.close()
            self._hdr_packing = None
        with self.session.current():
            if self._handle.value:
                if not self._destroy(self._handle):
                    raise RuntimeError('CUDA VSR native cleanup failed')
                self._handle = C.c_void_p()
            if self._stream.value:
                check(self._stream_destroy(self._stream), 'CUDA VSR stream destroy')
                self._stream = C.c_void_p()
        if self._dll_directory:
            self._dll_directory.close()
            self._dll_directory = None
        self._closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
