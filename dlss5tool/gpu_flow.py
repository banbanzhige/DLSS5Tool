"""Negotiated CUDA/D3D12 flow resources. Importing this module never loads CUDA.

One CUDA source per native queue slot. The host waits for that slot's D3D12
fence before requesting a write; the worker synchronizes its CUDA stream before
acknowledging. The next GPU submission uses the acknowledged slot exactly once.
"""
import ctypes as C
import os

TRANSPORT = 'cuda_d3d12_flow_v1'

class Win32Handle(C.Structure):
    _fields_ = [('handle', C.c_void_p), ('name', C.c_void_p)]


class HandleUnion(C.Union):
    _fields_ = [('fd', C.c_int), ('win32', Win32Handle), ('nvSciBufObject', C.c_void_p)]


class ExternalMemoryDesc(C.Structure):
    _fields_ = [('type', C.c_int), ('handle', HandleUnion), ('size', C.c_uint64),
                ('flags', C.c_uint), ('reserved', C.c_uint * 16)]


class BufferDesc(C.Structure):
    _fields_ = [('offset', C.c_uint64), ('size', C.c_uint64),
                ('flags', C.c_uint), ('reserved', C.c_uint * 16)]


def bind(lib, name, args, result=C.c_int):
    fn = getattr(lib, name)
    fn.argtypes, fn.restype = args, result
    return fn


def check(code, label):
    if code:
        raise RuntimeError(f'{label}: CUDA error {code}')



def eligible(settings):
    return (settings.get('guidance_gpu_transport', 'auto') != 'off'
            and int(settings.get('guidance_mode', 0)) == 1
            and settings.get('guidance_device') != 'cpu'
            and settings.get('guidance_flow_backend', 'raft') == 'raft'
            and settings.get('host_persistent_buffers', True)
            and not settings.get('host_tiled_mode', False)
            and settings.get('guidance_transport', 'auto') != 'pipe')


class NativeFlow:
    def __init__(self, lib, settings, width, height, adapter):
        from dlss5tool.guidance_parameters import analysis_size, parameters
        self.lib = lib
        bind(lib, 'dlssnr_gpu_flow_create', [C.c_uint, C.c_uint])
        bind(lib, 'dlssnr_gpu_flow_handle', [C.c_int, C.POINTER(C.c_uint64)], C.c_void_p)
        bind(lib, 'dlssnr_gpu_flow_reserve', [])
        bind(lib, 'dlssnr_gpu_flow_arm', [C.c_int])
        bind(lib, 'dlssnr_gpu_flow_close', [], None)
        fw, fh = analysis_size(width, height, parameters(settings)['guidance_flow_edge'])
        if fw > width or fh > height:
            raise RuntimeError('Small-frame downsampling keeps the CPU flow path')
        count = lib.dlssnr_gpu_flow_create(fw, fh)
        if not 1 <= count <= 16:
            raise RuntimeError('Native GPU flow resource creation unavailable')
        try:
            slots = []
            for i in range(count):
                allocation = C.c_uint64()
                handle = lib.dlssnr_gpu_flow_handle(i, C.byref(allocation))
                if not handle:
                    raise RuntimeError('Missing GPU flow shared handle')
                slots.append(dict(handle=handle, allocation=allocation.value))
            self.descriptor = dict(transport=TRANSPORT, owner_pid=os.getpid(), width=width, height=height,
                                   flow_size=[fw, fh], slots=slots,
                                   luid=[adapter['luid_high'], adapter['luid_low']])
        except BaseException:
            self.close()
            raise

    def reserve(self):
        slot = self.lib.dlssnr_gpu_flow_reserve()
        if not 0 <= slot < len(self.descriptor['slots']):
            raise RuntimeError('GPU flow slot is busy or unavailable; recreate the session')
        return slot

    def arm(self, slot):
        if not self.lib.dlssnr_gpu_flow_arm(slot):
            raise RuntimeError('GPU flow slot acknowledgement no longer matches the native queue')

    def close(self):
        self.lib.dlssnr_gpu_flow_close()


def validate_descriptor(desc, width, height, flow_size):
    if (len(flow_size) != 2 or any(type(v) is not int or not 128 <= v <= 2048 for v in flow_size)):
        raise ValueError('Invalid GPU flow dimensions')
    if (not isinstance(desc, dict) or desc.get('transport') != TRANSPORT
            or desc.get('width') != width or desc.get('height') != height
            or desc.get('flow_size') != list(flow_size)
            or type(desc.get('owner_pid')) is not int or not 0 < desc['owner_pid'] < 2**32
            or not isinstance(desc.get('slots'), list) or not 1 <= len(desc['slots']) <= 16
            or not isinstance(desc.get('luid'), list) or len(desc['luid']) != 2
            or any(type(v) is not int for v in desc['luid'])):
        raise ValueError('Invalid GPU flow descriptor')
    size = flow_size[0]*flow_size[1]*8
    for slot in desc['slots']:
        if (not isinstance(slot, dict) or type(slot.get('handle')) is not int
                or not 0 < slot['handle'] < 2**64 or type(slot.get('allocation')) is not int
                or not size <= slot['allocation'] <= size + 4*1024*1024):
            raise ValueError('Invalid GPU flow allocation')
    return size


class CudaFlow:
    """Worker-only import of handles duplicated from its authenticated host."""
    def __init__(self, desc, width, height, flow_size, torch):
        self.torch = torch
        self.size = validate_descriptor(desc, width, height, flow_size)
        self.mappings = []
        self.cuda = C.WinDLL('nvcuda.dll')
        for name, args in (
            ('cuCtxGetDevice', [C.POINTER(C.c_int)]),
            ('cuDeviceGetLuid', [C.c_void_p, C.POINTER(C.c_uint), C.c_int]),
            ('cuImportExternalMemory', [C.POINTER(C.c_void_p), C.POINTER(ExternalMemoryDesc)]),
            ('cuExternalMemoryGetMappedBuffer', [C.POINTER(C.c_uint64), C.c_void_p, C.POINTER(BufferDesc)]),
            ('cuMemcpyDtoDAsync_v2', [C.c_uint64, C.c_uint64, C.c_size_t, C.c_void_p]),
            ('cuStreamSynchronize', [C.c_void_p]),
            ('cuMemFree_v2', [C.c_uint64]),
            ('cuDestroyExternalMemory', [C.c_void_p]),
        ):
            bind(self.cuda, name, args)
        torch.empty(0, device='cuda')
        device, luid, mask = C.c_int(), C.create_string_buffer(8), C.c_uint()
        check(self.cuda.cuCtxGetDevice(C.byref(device)), 'cuCtxGetDevice')
        check(self.cuda.cuDeviceGetLuid(luid, C.byref(mask), device), 'cuDeviceGetLuid')
        actual = [int.from_bytes(luid.raw[4:], 'little', signed=True), int.from_bytes(luid.raw[:4], 'little')]
        if actual != desc['luid'] or mask.value != 1:
            raise RuntimeError('GPU flow requires the same single-node CUDA/D3D12 physical adapter')
        kernel = C.WinDLL('kernel32', use_last_error=True)
        bind(kernel, 'OpenProcess', [C.c_uint, C.c_int, C.c_uint], C.c_void_p)
        bind(kernel, 'GetCurrentProcess', [], C.c_void_p)
        bind(kernel, 'DuplicateHandle', [C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p),
                                        C.c_uint, C.c_int, C.c_uint])
        bind(kernel, 'CloseHandle', [C.c_void_p])
        process = kernel.OpenProcess(0x0040, False, desc['owner_pid'])  # PROCESS_DUP_HANDLE only
        if not process:
            raise C.WinError(C.get_last_error())
        try:
            for item in desc['slots']:
                handle = C.c_void_p()
                if not kernel.DuplicateHandle(process, item['handle'], kernel.GetCurrentProcess(),
                                              C.byref(handle), 0, False, 2):
                    raise C.WinError(C.get_last_error())
                external, pointer = C.c_void_p(), C.c_uint64()
                self.mappings.append((external, pointer))
                try:
                    memory = ExternalMemoryDesc(type=5, size=item['allocation'], flags=1)
                    memory.handle.win32.handle = handle.value
                    check(self.cuda.cuImportExternalMemory(C.byref(external), C.byref(memory)), 'cuImportExternalMemory')
                    check(self.cuda.cuExternalMemoryGetMappedBuffer(C.byref(pointer), external,
                          C.byref(BufferDesc(size=self.size))), 'cuExternalMemoryGetMappedBuffer')
                finally:
                    kernel.CloseHandle(handle)
        except BaseException:
            self.close()
            raise
        finally:
            kernel.CloseHandle(process)

    def send(self, flow, slot):
        if (type(slot) is not int or not 0 <= slot < len(self.mappings)
                or flow.dtype != self.torch.float32 or not flow.is_cuda
                or not flow.is_contiguous() or flow.numel()*4 != self.size):
            raise ValueError('Invalid CUDA flow frame/slot')
        stream = self.torch.cuda.current_stream().cuda_stream
        check(self.cuda.cuMemcpyDtoDAsync_v2(self.mappings[slot][1], flow.data_ptr(), self.size, stream), 'GPU flow copy')
        check(self.cuda.cuStreamSynchronize(stream), 'GPU flow producer completion')

    def close(self):
        # Caller does not use the resource after acknowledgement; host fences
        # drain consumers before exporter close. Process termination is fallback.
        self.torch.cuda.synchronize()
        for external, pointer in self.mappings:
            if pointer.value:
                check(self.cuda.cuMemFree_v2(pointer), 'GPU flow unmap')
                pointer.value = 0
            if external.value:
                check(self.cuda.cuDestroyExternalMemory(external), 'GPU flow release')
                external.value = None
        self.mappings.clear()
