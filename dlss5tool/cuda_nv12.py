"""Synchronous resident NV12 -> RGBA8, owned by one isolated CUDA worker.

The DLPack capsules retain decoder surfaces until our conversion completes.
They are inspected in place, never transferred or renamed as consumed. The
result is an owned CudaImage lease; no decoded pointer escapes this method.
"""
import ctypes as C
from pathlib import Path

from dlss5tool.cuda_frame import CudaImage, FrameTiming
from dlss5tool.cuda_session import bind, check


class _Device(C.Structure):
    _fields_ = [('kind', C.c_int), ('index', C.c_int)]


class _Dtype(C.Structure):
    _fields_ = [('code', C.c_uint8), ('bits', C.c_uint8), ('lanes', C.c_uint16)]


class _Tensor(C.Structure):
    _fields_ = [('data', C.c_void_p), ('device', _Device), ('ndim', C.c_int),
                ('dtype', _Dtype), ('shape', C.POINTER(C.c_int64)),
                ('strides', C.POINTER(C.c_int64)), ('offset', C.c_uint64)]


def conversion_coefficients(matrix, color_range):
    """BT.601/709 coefficients and production signed fixed-point rounding."""
    if matrix not in (1, 2, 5, 6) or color_range not in (0, 1, 2):
        raise ValueError('Resident SDR conversion supports BT.601/709 limited/full only')
    kr, kb = (.2126, .0722) if matrix == 1 else (.299, .114)
    kg = 1-kr-kb
    factors = (2*(1-kr), 2*(1-kb), -2*kb*(1-kb)/kg, -2*kr*(1-kr)/kg)
    scaled = [round(value*255/224*65536) for value in factors]
    full = color_range == 2
    if full:
        scaled = [int(value*224/255) for value in scaled]
    cy = 65536 if full else 65536*255//219
    return ((cy+4)//8, *[(value+4)//8 for value in scaled], 0 if full else 16*8)


def verified_geometry(width, height):
    # The production planar SIMD conversion has a width dispatch boundary.
    return (type(width) is int and type(height) is int and width >= 32 and height >= 16
            and width % 8 == 0 and height % 2 == 0 and width*height <= 1920*1080)


class CudaNv12Converter:
    software_format, dtype_bits, color_layout = 'nv12', 8, 'rgba8'
    kernel_name = b'nv12_to_rgba'

    def __init__(self, session, ptx_path, width, height, *, color_profile='srgb'):
        if not verified_geometry(width, height):
            raise ValueError('Resident decoder requires even height, width divisible by 8 and <=1080p input')
        session._check()
        self.session, self.width, self.height = session, width, height
        self._closed, self._failed = False, False
        self._module, self._stream, self._function = C.c_void_p(), C.c_void_p(), C.c_void_p()
        self._output = None
        self._load = bind(session.cuda, 'cuModuleLoadData', [C.POINTER(C.c_void_p), C.c_void_p])
        self._unload = bind(session.cuda, 'cuModuleUnload', [C.c_void_p])
        self._launch = bind(session.cuda, 'cuLaunchKernel', [C.c_void_p]+[C.c_uint]*7+
                            [C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)])
        self._sync = bind(session.cuda, 'cuStreamSynchronize', [C.c_void_p])
        self._destroy = bind(session.cuda, 'cuStreamDestroy_v2', [C.c_void_p])
        self._pointer_context = bind(session.cuda, 'cuPointerGetAttribute', [C.c_void_p, C.c_int, C.c_uint64])
        try:
            self._output = CudaImage(session, width, height, layout=self.color_layout, color_profile=color_profile)
            with session.current():
                ptx = C.create_string_buffer(Path(ptx_path).read_bytes())
                check(self._load(C.byref(self._module), ptx), 'NV12 PTX load')
                check(bind(session.cuda, 'cuModuleGetFunction',
                    [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p])(
                        C.byref(self._function), self._module, self.kernel_name), 'NVDEC conversion kernel')
                check(bind(session.cuda, 'cuStreamCreate', [C.POINTER(C.c_void_p), C.c_uint])(
                    C.byref(self._stream), 1), 'NV12 stream')
        except BaseException:
            self.close()
            raise

    def _planes(self, frame):
        if (frame.format.name != 'cuda' or frame.sw_format.name != self.software_format or
                (frame.width, frame.height) != (self.width, self.height) or len(frame.planes) != 2):
            raise ValueError('Full-size resident CUDA NV12 frame required')
        capsule_pointer = C.pythonapi.PyCapsule_GetPointer
        capsule_pointer.argtypes, capsule_pointer.restype = [C.py_object, C.c_char_p], C.c_void_p
        capsules, addresses, pitches = [], [], []
        for index, plane in enumerate(frame.planes):
            if plane.__dlpack_device__() != (2, self.session.device.value):
                raise ValueError('NVDEC physical CUDA device mismatch')
            capsule = plane.__dlpack__()
            capsules.append(capsule)
            tensor = C.cast(capsule_pointer(capsule, b'dltensor'), C.POINTER(_Tensor)).contents
            shape = ((self.height, self.width) if index == 0 else (self.height//2, self.width//2, 2))
            pitch = plane.line_size
            element_bytes = self.dtype_bits//8
            strides = (pitch//element_bytes, 1) if index == 0 else (pitch//element_bytes, 2, 1)
            if (tensor.ndim != len(shape) or not tensor.shape or not tensor.strides or
                    tuple(tensor.shape[:tensor.ndim]) != shape or tuple(tensor.strides[:tensor.ndim]) != strides or
                    (tensor.dtype.code, tensor.dtype.bits, tensor.dtype.lanes) != (1, self.dtype_bits, 1) or
                    (tensor.device.kind, tensor.device.index) != (2, self.session.device.value) or
                    type(pitch) is not int or pitch % element_bytes or not self.width*element_bytes <= pitch <= self.width*element_bytes+65536 or
                    not tensor.data or tensor.offset != 0):
                raise ValueError('Unexpected NVDEC plane shape/dtype/pitch/device/offset')
            context = C.c_void_p()
            check(self._pointer_context(C.byref(context), 1, tensor.data), 'NVDEC pointer context')
            if context.value != self.session.context.value:
                raise ValueError('NVDEC frame belongs to a different CUDA context owner')
            addresses.append(tensor.data); pitches.append(pitch)
        return capsules, addresses, pitches

    def convert(self, frame, timing):
        self.session._check()
        if self._closed or self._failed:
            raise RuntimeError('NV12 converter closed or failed')
        if not isinstance(timing, FrameTiming):
            raise TypeError('Source FrameTiming required')
        coeff = self._coefficients(frame)
        # Windows PyAV does not implement DLPack stream synchronization.
        # Complete the shared-context producer before exporting its capsules.
        self.session.synchronize()
        with self.session.current():
            capsules, addresses, pitches = self._planes(frame)
            try:
                return self._produce(addresses, pitches, coeff, timing)
            finally:
                capsules.clear()

    def _coefficients(self, frame):
        return conversion_coefficients(frame.colorspace, frame.color_range)

    def _produce(self, addresses, pitches, coeff, timing):
        """Internal synchronized launch; also used by the bounded fixture probe."""
        output = self._output.begin_write()
        values = [*[C.c_uint64(value) for value in (*addresses, output, *pitches, self._output.row_pitch)],
                  C.c_uint(self.width), C.c_uint(self.height), *[C.c_int(value) for value in coeff]]
        params = (C.c_void_p*len(values))(*[C.cast(C.byref(value), C.c_void_p).value for value in values])
        try:
            check(self._launch(self._function, (self.width+31)//32, (self.height+7)//8, 1,
                32, 8, 1, 0, self._stream, params, None), 'NV12 conversion')
            check(self._sync(self._stream), 'NV12 conversion completion')
        except BaseException:
            self._failed = True
            raise
        return self._output.complete(timing, stream=self._stream.value)

    def close(self):
        if self._closed:
            return
        self.session._check()
        if self._output and self._output._leases:
            raise RuntimeError('NV12 output is still held by a consumer')
        with self.session.current():
            if self._stream.value:
                check(self._sync(self._stream), 'NV12 final completion')
                check(self._destroy(self._stream), 'NV12 stream destroy')
                self._stream = C.c_void_p()
            if self._module.value:
                check(self._unload(self._module), 'NV12 module unload')
                self._module = C.c_void_p()
        if self._output:
            self._output.close(); self._output = None
        self._closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()
