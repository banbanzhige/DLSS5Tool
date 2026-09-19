"""Unscaled HDR candidate, preserving tested FFmpeg 7.1.1 P010 conversion.

PQ/HLG remain transfer-coded; no tone mapping. One GPU horizontal intermediate,
two kernels, no per-frame CPU image conversion. Geometry coefficients computed
once on CPU. Producer must complete before access, lease lives until reuse.
"""
import ctypes as C
import numpy as np

from dlss5tool.cuda_sdr_yuv import CudaSdrConverter
from dlss5tool.nvenc_yuv import ReadyYuvFrame


def chroma_tables(size,precision):
    if type(size) is not int or size<16 or size%2:raise ValueError('Even geometry >=16 required')
    indices=np.zeros((size//2,8),np.int32);weights=np.zeros_like(indices)
    for position in range(size//2):
        taps={}
        for offset in range(-3,5):
            distance=abs((offset-.5)/2)
            value=((1.4*distance**3-2.4*distance**2+1) if distance<1 else
                   (-.6*distance**3+3*distance**2-4.8*distance+2.4))/2
            index=min(max(2*position+offset,0),size-1)
            taps[index]=taps.get(index,0)+value
        total=0.;previous=0
        for tap,(index,value) in enumerate(sorted(taps.items())):
            total+=value;rounded=round(total*precision)
            indices[position,tap]=index;weights[position,tap]=rounded-previous;previous=rounded
    return indices,weights


class CudaHdrConverter(CudaSdrConverter):
    KERNEL_NAME=b'hdr_horizontal'
    FRAME_BYTES_DENOMINATOR=1

    @classmethod
    def from_contract(cls,ptx_path,contract):
        from dlss5tool.encoding_contract import frame_encoding_contract
        colors=dict(zip(contract.output_color_args[::2],contract.output_color_args[1::2]))
        metadata=dict(color_transfer=colors.get('-color_trc'),color_primaries=colors.get('-color_primaries'),
                      color_space=colors.get('-colorspace'))
        if (metadata['color_transfer'] not in ('smpte2084','arib-std-b67') or metadata['color_primaries']!='bt2020'
                or metadata['color_space']!='bt2020nc' or contract!=frame_encoding_contract(
                    contract.width,contract.height,contract.fps,contract.width,contract.height,False,metadata)):
            raise ValueError('Only unscaled BT2020 PQ/HLG production HDR contract is accepted')
        return cls(ptx_path,contract.width,contract.height)

    def __init__(self,ptx_path,width,height):
        if width<16 or height<16:raise ValueError('HDR conversion dimensions >=16 required')
        self._extra=[]
        super().__init__(ptx_path,width,height)
        try:
            self._vertical=C.c_void_p()
            self._check_status(self._get_fn(C.byref(self._vertical),self._module,b'hdr_vertical'),'vertical kernel')
            upload=self._cuda.cuMemcpyHtoD_v2
            upload.argtypes=[C.c_uint64,C.c_void_p,C.c_size_t];upload.restype=C.c_int
            self._tables=[]
            for size,precision in ((self.output_width,16384),(self.output_height,4096)):
                for array in chroma_tables(size,precision):
                    pointer=self._new_buffer(array.nbytes)
                    self._check_status(upload(pointer,array.ctypes.data,array.nbytes),'filter table upload')
                    self._tables.append(pointer)
            self._rows=self._new_buffer(self.output_width*self.output_height*4)
            self._invalid=self._new_buffer(4);self._validate=C.c_void_p()
            self._check_status(self._get_fn(C.byref(self._validate),self._module,b'hdr_validate'),'HDR validity kernel')
            self._reset=self._cuda.cuMemsetD32_v2;self._reset.argtypes=[C.c_uint64,C.c_uint,C.c_size_t];self._reset.restype=C.c_int
            self._read=self._cuda.cuMemcpyDtoH_v2;self._read.argtypes=[C.c_void_p,C.c_uint64,C.c_size_t];self._read.restype=C.c_int
        except BaseException:self.close();raise

    def _new_buffer(self,size):
        pointer=C.c_uint64()
        self._check_status(self._allocate(C.byref(pointer),size),'HDR allocation')
        self._extra.append(pointer)
        return pointer

    def convert(self,pointer,nbytes,layout,sequence,row_pitch=None,*,strict=False):
        self._check_owner()
        if self._closed or self._failed:raise RuntimeError('HDR converter closed or failed')
        if layout not in ('rgba16f','rgba32f'):raise ValueError('HDR requires RGBA float16/32')
        pixel_bytes=8 if layout=='rgba16f' else 16
        pitch=self.width*pixel_bytes if row_pitch is None else row_pitch
        if (type(pointer) is not int or not 0<pointer<2**64 or type(nbytes) is not int or type(pitch) is not int
                or not self.width*pixel_bytes<=pitch<2**32 or pitch%(pixel_bytes//4)
                or pointer%(pixel_bytes//4) or nbytes<pitch*(self.height-1)+self.width*pixel_bytes
                or type(sequence) is not int or not 0<=sequence<2**32-1):raise ValueError('Invalid HDR memory contract')
        def launch(function,width,height,values):
            params=(C.c_void_p*len(values))(*[C.cast(C.byref(value),C.c_void_p).value for value in values])
            self._check_status(self._launch(function,(width+15)//16,(height+15)//16,1,16,16,1,0,
                self._stream,params,None),'HDR launch')
        if strict:
            self._check_status(self._reset(self._invalid,0,1),'HDR validity reset')
            launch(self._validate,self.width,self.height,[C.c_uint64(pointer),C.c_uint(self.width),C.c_uint(self.height),
                C.c_uint64(pitch),C.c_uint(layout=='rgba32f'),self._invalid])
            self._check_status(self._sync(self._stream),'HDR validity completion')
            invalid=C.c_uint()
            self._check_status(self._read(C.byref(invalid),self._invalid,4),'HDR validity flag')
            if invalid.value:raise ValueError('DLSSG returned invalid HDR values; no clipping or SDR fallback')
        launch(self._function,self.output_width,self.output_height,[C.c_uint64(pointer),self._pointer,
            self._rows,*self._tables[:2],C.c_uint(self.width),C.c_uint(self.height),C.c_uint64(pitch),
            C.c_uint(layout=='rgba32f')])
        launch(self._vertical,self.output_width//2,self.output_height//2,[self._rows,self._pointer,
            *self._tables[2:],C.c_uint(self.output_width),C.c_uint(self.output_height)])
        self._check_status(self._sync(self._stream),'HDR completion')
        return ReadyYuvFrame(self._pointer.value,self.nbytes,'p010le',sequence)

    def close(self):
        if getattr(self,'_closed',True):return
        self._check_owner()
        errors=[]
        if self._stream.value:errors.append(self._sync(self._stream))
        for pointer in self._extra:errors.append(self._free(pointer))
        self._extra=[]
        super().close()
        if any(errors):raise RuntimeError(f'HDR cleanup failed: {errors}')
