"""Exact synchronous half/packed-10 bridge inside a single CUDA frame owner."""
import ctypes as C
from contextlib import nullcontext
from pathlib import Path

from dlss5tool.cuda_session import bind, check
from dlss5tool.memory_budget import memory_allocation


class CudaHdrPacking:
    def __init__(self, session, ptx, width, height, scale, *, allocate_scratch=True):
        self.session, self.width, self.height, self.scale = session, width, height, scale
        self._module, self._stream = C.c_void_p(), C.c_void_p()
        self._functions, self._buffers, self._closed = {}, [], False
        self._validation = None
        self._launch = bind(session.cuda, 'cuLaunchKernel', [C.c_void_p]+[C.c_uint]*7+
                            [C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)])
        self._sync = bind(session.cuda, 'cuStreamSynchronize', [C.c_void_p])
        self._destroy = bind(session.cuda, 'cuStreamDestroy_v2', [C.c_void_p])
        self._unload = bind(session.cuda, 'cuModuleUnload', [C.c_void_p])
        try:
            with session.current():
                source = C.create_string_buffer(Path(ptx).read_bytes())
                check(bind(session.cuda, 'cuModuleLoadData', [C.POINTER(C.c_void_p), C.c_void_p])(
                    C.byref(self._module), source), 'HDR pack PTX')
                for name in ('vsr_pack', 'vsr_unpack'):
                    function = C.c_void_p()
                    check(bind(session.cuda, 'cuModuleGetFunction',
                               [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p])(
                        C.byref(function), self._module, name.encode()), name)
                    self._functions[name] = function
                check(bind(session.cuda, 'cuStreamCreate', [C.POINTER(C.c_void_p), C.c_uint])(
                    C.byref(self._stream), 1), 'HDR pack stream')
            size = (width*height + width*height*scale*scale)*4 if allocate_scratch else 0
            with (memory_allocation(size, stage='cuda_hdr_pack') if size else nullcontext()) as ticket:
                for count in ((width*height*4, width*height*scale*scale*4) if allocate_scratch else ()):
                    self._buffers.append(session.allocate(count))
                if ticket: ticket.consume(size)
        except BaseException:
            self.close()
            raise

    def convert(self, name, source, destination, width, height, source_pitch, destination_pitch):
        self.session._check()
        if self._closed:
            raise RuntimeError('HDR pack converter closed')
        values = [C.c_uint64(source), C.c_uint64(destination), C.c_uint(width), C.c_uint(height),
                  C.c_uint64(source_pitch), C.c_uint64(destination_pitch)]
        params = (C.c_void_p*len(values))(*[C.cast(C.byref(value), C.c_void_p).value for value in values])
        with self.session.current():
            check(self._launch(self._functions[name], (width+15)//16, (height+15)//16, 1,
                               16, 16, 1, 0, self._stream, params, None), 'HDR ' + name)
            check(self._sync(self._stream), 'HDR pack completion')

    def close(self):
        if self._closed:
            return
        with self.session.current():
            if self._stream.value:
                check(self._sync(self._stream), 'HDR pack final completion')
                check(self._destroy(self._stream), 'HDR pack stream destroy')
                self._stream = C.c_void_p()
            if self._module.value:
                check(self._unload(self._module), 'HDR pack module unload')
                self._module = C.c_void_p()
        for pointer in self._buffers:
            self.session.free(pointer)
        if self._validation:
            self.session.free(self._validation[1])
            self._validation = None
        self._buffers.clear()
        self._closed = True

    def validate(self, frame):
        frame._check()
        if self._closed or frame.session is not self.session or frame.layout != 'rgba16f':
            raise ValueError('Same-owner HDR frame required for validation')
        with self.session.current():
            if self._validation is None:
                function = C.c_void_p()
                check(bind(self.session.cuda, 'cuModuleGetFunction',
                    [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p])(
                        C.byref(function), self._module, b'validate_half'), 'HDR validity kernel')
                flag = self.session.allocate(4)
                self._validation = (function, flag)
            function, flag = self._validation
            check(bind(self.session.cuda, 'cuMemsetD32Async', [C.c_uint64, C.c_uint, C.c_size_t, C.c_void_p])(
                flag, 0, 1, self._stream), 'HDR validity reset')
            values = [C.c_uint64(frame.pointer), C.c_uint(frame.width), C.c_uint(frame.height),
                      C.c_uint64(frame.row_pitch), C.c_uint64(flag)]
            params = (C.c_void_p*len(values))(*[C.cast(C.byref(value), C.c_void_p).value for value in values])
            check(self._launch(function, (frame.width+15)//16, (frame.height+15)//16, 1,
                16, 16, 1, 0, self._stream, params, None), 'HDR validity launch')
            check(self._sync(self._stream), 'HDR validity completion')
        if int.from_bytes(self.session.download(flag, 4), 'little'):
            raise ValueError('FG input contains invalid HDR values; no clipping or fallback')
