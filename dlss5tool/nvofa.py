"""Worker-only NVIDIA Optical Flow API 2.0; importing does not load DLLs."""
# NVIDIA ABI definitions adapted under the following license:
# Copyright(c) 2020, NVIDIA CORPORATION. All rights reserved.
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
# 3. Neither the name of the copyright holder nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
import ctypes as C
import contextlib
import sys

P=C.c_void_p
U=C.c_uint32
I=C.c_int
Z=C.c_size_t


class Init(C.Structure):
    _fields_=[('width',U),('height',U),('grid',I),('hint_grid',I),('mode',I),('perf',I),
              ('external',I),('cost',I),('private',P),('disparity',I),('roi',I)]


class Buffer(C.Structure):
    _fields_=[('width',U),('height',U),('usage',I),('format',I)]


class ExecuteIn(C.Structure):
    _fields_=[('input',P),('reference',P),('hints',P),('disable_temporal',I),('padding',U),
              ('private',P),('padding2',U),('num_rois',U),('rois',P)]


class ExecuteOut(C.Structure):
    _fields_=[('output',P),('cost',P),('private',P)]


class Stride(C.Structure):
    _fields_=[('strides',U*6),('planes',U)]


class Copy2D(C.Structure):
    # CUDA_MEMCPY2D driver ABI, x64 size_t and CUdeviceptr.
    _fields_=[('sx',Z),('sy',Z),('stype',I),('shost',P),('sdevice',C.c_uint64),('sarray',P),('spitch',Z),
              ('dx',Z),('dy',Z),('dtype',I),('dhost',P),('ddevice',C.c_uint64),('darray',P),('dpitch',Z),
              ('width',Z),('height',Z)]



def align_size(width, height):
    if width <= 0 or height <= 0:
        raise ValueError('Positive dimensions required')
    return ((int(width) + 15) // 16 * 16, (int(height) + 15) // 16 * 16)


def output_shape(width, height, grid=4):
    if grid not in (1, 2, 4):
        raise ValueError('NVOFA grid must be 1, 2, or 4')
    return (int(height) + grid - 1) // grid, (int(width) + grid - 1) // grid


def decode_flow(values, width, height):
    import cv2
    import numpy as np
    if values.dtype != np.int16 or values.ndim != 3 or values.shape[2] != 2:
        raise ValueError('Expected S10.5 int16 HxWx2')
    return cv2.resize(values.astype(np.float32) / 32.0, (width, height),
                      interpolation=cv2.INTER_LINEAR)


class OpticalFlow:
    """Synchronous, thread-confined session. Uses Torch's current CUDA context."""
    def __init__(self, w, h, *, require_current=False, grid=4):
        import numpy as np
        self.np = np
        self.w, self.h = w, h
        self.grid = grid
        self.gh, self.gw = output_shape(w, h, grid)
        self.handle = self.context = None
        self.buffers, self.pointers, self.pitches = [], [], []
        self._retained = False
        self._closed = False
        self.cleanup_errors = []
        try:
            self._load(require_current)
            with self._current():
                self._initialize()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _bind(lib, name, result, *args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, list(args)
        return fn

    def _load(self, require_current):
        # LOAD_LIBRARY_SEARCH_SYSTEM32, never search mods/current directory.
        self.cuda = C.WinDLL('nvcuda.dll', winmode=0x800)
        self.dll = C.WinDLL('nvofapi64.dll', winmode=0x800)
        bind, check = self._bind, self.check
        self.sync = bind(self.cuda, 'cuCtxSynchronize', I)
        self.copy = bind(self.cuda, 'cuMemcpy2D_v2', I, C.POINTER(Copy2D))
        self.push = bind(self.cuda, 'cuCtxPushCurrent_v2', I, P)
        self.pop = bind(self.cuda, 'cuCtxPopCurrent_v2', I, C.POINTER(P))
        self.release = bind(self.cuda, 'cuDevicePrimaryCtxRelease_v2', I, I)
        check(bind(self.cuda, 'cuInit', I, U)(0), 'cuInit')
        current, device = P(), I()
        check(bind(self.cuda, 'cuCtxGetCurrent', I, C.POINTER(P))(C.byref(current)), 'current context')
        if current:
            check(bind(self.cuda, 'cuCtxGetDevice', I, C.POINTER(I))(C.byref(device)), 'current device')
        elif require_current:
            raise RuntimeError('Torch CUDA context is not current')
        else:
            check(bind(self.cuda, 'cuDeviceGet', I, C.POINTER(I), I)(C.byref(device), 0), 'probe device')
        self.device, self.context = device, P()
        check(bind(self.cuda, 'cuDevicePrimaryCtxRetain', I, C.POINTER(P), I)(
            C.byref(self.context), device), 'retain')
        self._retained = True
        self.table = (P * 12)()
        check(bind(self.dll, 'NvOFAPICreateInstanceCuda', I, U, P)(0x20, self.table), 'create API 2.0')
        def api(index, result, *args):
            if not self.table[index]:
                raise RuntimeError(f'Missing API function {index}')
            return C.WINFUNCTYPE(result, *args)(self.table[index])
        self.create = api(0, I, P, C.POINTER(P))
        self.init = api(1, I, P, C.POINTER(Init))
        self.allocate = api(2, I, P, C.POINTER(Buffer), I, C.POINTER(P))
        self.pointer = api(4, C.c_uint64, P)
        self.stride = api(5, I, P, C.POINTER(Stride))
        self.execute = api(7, I, P, C.POINTER(ExecuteIn), C.POINTER(ExecuteOut))
        self.free = api(8, I, P)
        self.destroy = api(9, I, P)
        self.error = api(10, I, P, P, C.POINTER(U))
        self.getcaps = api(11, I, P, I, C.POINTER(U), C.POINTER(U))

    @contextlib.contextmanager
    def _current(self):
        self.check(self.push(self.context), 'push')
        try:
            yield
        finally:
            failed = sys.exc_info()[0] is not None
            popped = P()
            status = self.pop(C.byref(popped))
            if not failed:
                self.check(status, 'pop')

    def _caps(self, kind):
        count = U()
        self.check(self.getcaps(self.handle, kind, None, C.byref(count)), 'cap count')
        if not 0 < count.value <= 64:
            raise RuntimeError('Invalid capability count')
        values = (U * count.value)()
        self.check(self.getcaps(self.handle, kind, values, C.byref(count)), 'cap values')
        return list(values)

    def _initialize(self):
        self.handle = P()
        self.check(self.create(self.context, C.byref(self.handle)), 'create flow')
        supported = self._caps(0)
        if self.grid not in supported:
            raise RuntimeError(f'NVOFA unsupported grid {self.grid}; supported {supported}')
        if (not self._caps(4)[0] <= self.w <= self._caps(6)[0] or
                not self._caps(5)[0] <= self.h <= self._caps(7)[0]):
            raise RuntimeError(f'NVOFA unsupported dimensions: {self.w}x{self.h}')
        params = Init(self.w, self.h, self.grid, 0, 1, 5, 0, 0, None, 0, 0)
        self.check(self.init(self.handle, C.byref(params)), 'init flow')
        for desc in (Buffer(self.w, self.h, 1, 1), Buffer(self.w, self.h, 1, 1),
                     Buffer(self.gw, self.gh, 2, 5)):
            buffer = P()
            self.check(self.allocate(self.handle, C.byref(desc), 2, C.byref(buffer)), 'buffer')
            self.buffers.append(buffer)
            pointer = self.pointer(buffer)
            if not pointer:
                raise RuntimeError('Null GPU buffer')
            self.pointers.append(pointer)
            stride = Stride()
            self.check(self.stride(buffer, C.byref(stride)), 'stride')
            self.pitches.append(stride.strides[0])
        self.output = self.np.empty((self.gh, self.gw, 2), self.np.int16)

    def check(self, status, label):
        if status:
            message = ''
            if self.handle and hasattr(self, 'error'):
                buffer, size = C.create_string_buffer(512), U(512)
                self.error(self.handle, buffer, C.byref(size))
                message = buffer.value.decode(errors='replace')
            raise RuntimeError(f'{label}: status={status} {message}')

    def calculate(self, first, second):
        import cv2
        if self._closed:
            raise RuntimeError('NVOFA session closed')
        if any(x.dtype != self.np.uint8 or x.shape != (self.h, self.w, 3) for x in (first, second)):
            raise ValueError('Expected uint8 RGB at fixed session dimensions')
        with self._current():
            for index, rgb in enumerate((first, second)):
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                copy = Copy2D(stype=1, shost=gray.ctypes.data, spitch=self.w, dtype=2,
                              ddevice=self.pointers[index], dpitch=self.pitches[index],
                              width=self.w, height=self.h)
                self.check(self.copy(C.byref(copy)), 'upload')
            inp = ExecuteIn(self.buffers[0], self.buffers[1], None, 1, 0, None, 0, 0, None)
            out = ExecuteOut(self.buffers[2], None, None)
            self.check(self.execute(self.handle, C.byref(inp), C.byref(out)), 'execute')
            self.check(self.sync(), 'synchronize')
            copy = Copy2D(stype=2, sdevice=self.pointers[2], spitch=self.pitches[2], dtype=1,
                          dhost=self.output.ctypes.data, dpitch=self.gw * 4,
                          width=self.gw * 4, height=self.gh)
            self.check(self.copy(C.byref(copy)), 'readback')
            return decode_flow(self.output, self.w, self.h)

    def close(self):
        if self._closed:
            return
        self._closed = True
        def attempt(fn, *args):
            try:
                status = fn(*args)
                if status:
                    self.cleanup_errors.append(str(status))
            except Exception as exc:
                self.cleanup_errors.append(str(exc))
        if self.handle:
            try:
                with self._current():
                    for buffer in reversed(self.buffers):
                        attempt(self.free, buffer)
                    attempt(self.destroy, self.handle)
            except Exception as exc:
                self.cleanup_errors.append(str(exc))
        self.handle = None
        self.buffers.clear()
        if self._retained:
            attempt(self.release, self.device)
            self._retained = False
        self.context = None
