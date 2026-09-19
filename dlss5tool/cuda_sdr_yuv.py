"""Explicit candidate converter: unscaled SDR uint8 RGB -> contiguous CUDA I420.

Not enabled by export. Requires separately built PTX and an existing CUDA
context; no Torch/NVRTC imports or implicit device selection. Producer work must
be complete before convert(). The source owner must retain memory until return.
"""
import ctypes as C
from pathlib import Path
import threading

from dlss5tool.nvenc_yuv import ReadyYuvFrame


class CudaSdrConverter:
    KERNEL_NAME=b'sdr_to_i420'
    FRAME_BYTES_NUMERATOR=3
    FRAME_BYTES_DENOMINATOR=2
    LAYOUTS={'bgr24':(3,2,0),'rgb24':(3,0,2),'bgra':(4,2,0),'rgba':(4,0,2)}

    @classmethod
    def from_contract(cls, ptx_path, contract):
        if (contract.wire_format!='bgr24' or contract.encoder_format!='yuv420p'
                or contract.filter_chain!='pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p'
                or contract.input_color_args or contract.output_color_args):
            raise ValueError('Only unscaled, untagged SDR production conversion is accepted')
        return cls(ptx_path,contract.width,contract.height)

    def __init__(self, ptx_path, width, height):
        if (type(width) is not int or type(height) is not int or width<1 or height<1
                or ((width+1)//2*2)*((height+1)//2*2)>3840*2160):
            raise ValueError('Positive dimensions within a padded 4K pixel budget required')
        self.width,self.height=width,height
        self.output_width,self.output_height=(width+1)//2*2,(height+1)//2*2
        self.nbytes=self.output_width*self.output_height*self.FRAME_BYTES_NUMERATOR//self.FRAME_BYTES_DENOMINATOR
        self._thread=threading.get_ident();self._closed=False;self._failed=False
        self._module=C.c_void_p();self._stream=C.c_void_p();self._pointer=C.c_uint64()
        self._cuda=C.WinDLL('nvcuda.dll')
        def bind(name,args):
            fn=getattr(self._cuda,name);fn.argtypes=args;fn.restype=C.c_int;return fn
        p=C.c_void_p;u=C.c_uint
        self._context=C.c_void_p()
        self._get_context=bind('cuCtxGetCurrent',[C.POINTER(p)])
        self._load=bind('cuModuleLoadData',[C.POINTER(p),p])
        self._unload=bind('cuModuleUnload',[p])
        self._get_fn=bind('cuModuleGetFunction',[C.POINTER(p),p,C.c_char_p])
        self._allocate=bind('cuMemAlloc_v2',[C.POINTER(C.c_uint64),C.c_size_t])
        self._free=bind('cuMemFree_v2',[C.c_uint64])
        self._create_stream=bind('cuStreamCreate',[C.POINTER(p),u])
        self._destroy_stream=bind('cuStreamDestroy_v2',[p])
        self._sync=bind('cuStreamSynchronize',[p])
        self._launch=bind('cuLaunchKernel',[p]+[u]*7+[p,C.POINTER(p),C.POINTER(p)])
        self._check_status(self._get_context(C.byref(self._context)),'context')
        if not self._context.value:raise RuntimeError('Calling worker needs an existing CUDA context')
        try:
            ptx=C.create_string_buffer(Path(ptx_path).read_bytes())
            self._check_status(self._load(C.byref(self._module),ptx),'load PTX')
            self._function=p()
            self._check_status(self._get_fn(C.byref(self._function),self._module,self.KERNEL_NAME),'kernel')
            self._check_status(self._allocate(C.byref(self._pointer),self.nbytes),'output allocation')
            self._check_status(self._create_stream(C.byref(self._stream),1),'stream')
        except BaseException:
            self.close();raise

    def _check_status(self, status, operation):
        if status:
            self._failed=True
            raise RuntimeError(f'CUDA {operation} failed ({status})')

    def _check_owner(self):
        if threading.get_ident()!=self._thread:raise RuntimeError('Converter must stay on its owning CUDA thread')
        current=C.c_void_p()
        if self._get_context(C.byref(current)) or current.value!=self._context.value:
            raise RuntimeError('Restore the owning CUDA context before converter access')

    def convert(self, pointer, nbytes, layout, sequence, row_pitch=None):
        """Return a lease valid UNTIL next convert/close. Feed the encoder first.

        The consumer must finish its synchronous D2D copy before output reuse.
        Caller waits for producer completion; this method synchronizes only its
        own conversion stream. Does not accept HDR, scaling or mixed views.
        """
        self._check_owner()
        if self._closed or self._failed:raise RuntimeError('Converter is closed or failed')
        if layout not in self.LAYOUTS:raise ValueError('Unsupported integer SDR layout')
        channels,red,blue=self.LAYOUTS[layout]
        pitch=self.width*channels if row_pitch is None else row_pitch
        if (type(pointer) is not int or not 0<pointer<2**64 or type(nbytes) is not int
                or type(pitch) is not int or not self.width*channels<=pitch<2**32
                or nbytes<pitch*(self.height-1)+self.width*channels
                or type(sequence) is not int or not 0<=sequence<2**32-1):
            raise ValueError('Invalid source pointer/size/pitch/sequence')
        values=[C.c_uint64(pointer),self._pointer,C.c_uint(self.width),C.c_uint(self.height),
                C.c_uint64(pitch),C.c_uint(channels),C.c_uint(red),C.c_uint(blue)]
        params=(C.c_void_p*len(values))(*[C.cast(C.byref(value),C.c_void_p).value for value in values])
        self._check_status(self._launch(self._function,(self.output_width//2+15)//16,
            (self.output_height//2+15)//16,1,16,16,1,0,self._stream,params,None),'launch')
        self._check_status(self._sync(self._stream),'completion')
        return ReadyYuvFrame(self._pointer.value,self.nbytes,'yuv420p',sequence)

    def close(self):
        if self._closed:return
        self._check_owner()
        errors=[]
        if self._stream.value:
            errors.append(self._sync(self._stream))
            errors.append(self._destroy_stream(self._stream));self._stream=C.c_void_p()
        if self._pointer.value:
            errors.append(self._free(self._pointer.value));self._pointer=C.c_uint64()
        if self._module.value:
            errors.append(self._unload(self._module));self._module=C.c_void_p()
        self._closed=True
        if any(errors):raise RuntimeError(f'CUDA converter cleanup failed: {errors}')

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
