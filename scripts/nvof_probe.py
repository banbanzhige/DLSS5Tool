"""Isolated ctypes probe for NVIDIA's public Optical Flow SDK 2.0 CUDA API.

ABI transcribed from NVIDIA/NVIDIAOpticalFlowSDK public headers (BSD-3-Clause),
downloaded alongside experimental assets. Uses installed NVIDIA driver DLLs;
no replacement DLL, no third-party Python package, no production integration.
Temporal hints disabled for independently cacheable pairs and seek safety.
"""
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
import json
import time

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


class OpticalFlow:
    def __init__(self,w,h):
        import numpy as np
        self.np=np
        self.w,self.h=w,h
        self.grid=4
        self.gw,self.gh=(w+3)//4,(h+3)//4
        self.cuda=C.WinDLL('nvcuda.dll')
        self.dll=C.WinDLL('nvofapi64.dll')
        def bind(lib,name,result,*args):
            fn=getattr(lib,name);fn.restype=result;fn.argtypes=list(args);return fn
        self.sync=bind(self.cuda,'cuCtxSynchronize',I)
        self.copy=bind(self.cuda,'cuMemcpy2D_v2',I,C.POINTER(Copy2D))
        self.push=bind(self.cuda,'cuCtxPushCurrent_v2',I,P)
        self.pop=bind(self.cuda,'cuCtxPopCurrent_v2',I,C.POINTER(P))
        check= self.check
        check(bind(self.cuda,'cuInit',I,U)(0),'cuInit')
        device=I()
        check(bind(self.cuda,'cuDeviceGet',I,C.POINTER(I),I)(C.byref(device),0),'cuDeviceGet')
        self.context=P()
        # Retain primary context, shared with torch on device zero.
        check(bind(self.cuda,'cuDevicePrimaryCtxRetain',I,C.POINTER(P),I)(C.byref(self.context),device),'retain')
        self.device=device
        check(self.push(self.context),'push')
        self.table=(P*12)()
        check(bind(self.dll,'NvOFAPICreateInstanceCuda',I,U,P)(0x20,self.table),'create API 2.0')
        def api(index,result,*args): return C.WINFUNCTYPE(result,*args)(self.table[index])
        self.create=api(0,I,P,C.POINTER(P))
        self.init=api(1,I,P,C.POINTER(Init))
        self.allocate=api(2,I,P,C.POINTER(Buffer),I,C.POINTER(P))
        self.pointer=api(4,C.c_uint64,P)
        self.stride=api(5,I,P,C.POINTER(Stride))
        self.execute=api(7,I,P,C.POINTER(ExecuteIn),C.POINTER(ExecuteOut))
        self.free=api(8,I,P)
        self.destroy=api(9,I,P)
        self.error=api(10,I,P,P,C.POINTER(U))
        self.handle=P()
        check(self.create(self.context,C.byref(self.handle)),'create flow')
        params=Init(w,h,4,0,1,5,0,0,None,0,0) # SLOW/highest quality
        check(self.init(self.handle,C.byref(params)),'init flow')
        self.buffers=[]
        self.pointers=[]
        self.pitches=[]
        for desc in (Buffer(w,h,1,1),Buffer(w,h,1,1),Buffer(self.gw,self.gh,2,5)):
            buffer=P()
            check(self.allocate(self.handle,C.byref(desc),2,C.byref(buffer)),'buffer')
            self.buffers.append(buffer)
            self.pointers.append(self.pointer(buffer))
            stride=Stride()
            check(self.stride(buffer,C.byref(stride)),'stride')
            self.pitches.append(stride.strides[0])
        self.output=np.empty((self.gh,self.gw,2),np.int16)
        popped=P();check(self.pop(C.byref(popped)),'pop')

    def check(self,status,label):
        if status:
            message=''
            if hasattr(self,'handle') and self.handle and hasattr(self,'error'):
                buffer=C.create_string_buffer(512);size=U(512)
                self.error(self.handle,buffer,C.byref(size))
                message=buffer.value.decode(errors='replace')
            raise RuntimeError(f'{label}: status={status} {message}')

    def calculate(self,first,second):
        import cv2
        if first.shape != (self.h,self.w,3) or second.shape != first.shape:
            raise ValueError('Input dimensions must match fixed session')
        self.check(self.push(self.context),'push')
        try:
            for index,rgb in enumerate((first,second)):
                gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
                copy=Copy2D(stype=1,shost=gray.ctypes.data,spitch=self.w,dtype=2,
                    ddevice=self.pointers[index],dpitch=self.pitches[index],width=self.w,height=self.h)
                self.check(self.copy(C.byref(copy)),'upload')
            inp=ExecuteIn(self.buffers[0],self.buffers[1],None,1,0,None,0,0,None)
            out=ExecuteOut(self.buffers[2],None,None)
            self.check(self.execute(self.handle,C.byref(inp),C.byref(out)),'execute')
            self.check(self.sync(),'synchronize')
            copy=Copy2D(stype=2,sdevice=self.pointers[2],spitch=self.pitches[2],dtype=1,
                dhost=self.output.ctypes.data,dpitch=self.gw*4,width=self.gw*4,height=self.gh)
            self.check(self.copy(C.byref(copy)),'readback')
            # Vectors already measured in input pixels. Do NOT multiply by grid.
            return cv2.resize(self.output.astype(self.np.float32)/32,(self.w,self.h),interpolation=cv2.INTER_LINEAR)
        finally:
            popped=P();self.check(self.pop(C.byref(popped)),'pop')

    def close(self):
        if not self.handle:return
        self.check(self.push(self.context),'push')
        try:
            for buffer in self.buffers:self.check(self.free(buffer),'free')
            self.check(self.destroy(self.handle),'destroy')
            self.handle=None
        finally:
            popped=P();self.pop(C.byref(popped))


def main():
    import argparse
    from pathlib import Path
    import cv2
    import numpy as np
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    # Known synthetic translation checks ordering, scaling, fixed-point decode.
    rng=np.random.default_rng(19)
    first=rng.integers(0,256,(384,512,3),dtype=np.uint8)
    first=cv2.GaussianBlur(first,(5,5),0)
    second=cv2.warpAffine(first,np.float32([[1,0,8],[0,1,4]]),(512,384),borderMode=cv2.BORDER_REFLECT)
    engine=OpticalFlow(512,384)
    durations=[]
    for i in range(24):
        tick=time.perf_counter();flow=engine.calculate(first,second)
        if i>=4:durations.append((time.perf_counter()-tick)*1000)
    roi=flow[32:-32,32:-32]
    error=np.linalg.norm(roi-np.float32([8,4]),axis=-1)
    reverse=engine.calculate(second,first)[32:-32,32:-32]
    record={'api':'2.0','grid':4,'quality':'slow','temporal_hints':False,
        'mean_ms':float(np.mean(durations)),'p95_ms':float(np.percentile(durations,95)),
        'median_flow':np.median(roi,axis=(0,1)).tolist(),'epe_mean':float(error.mean()),
        'reverse_median_flow':np.median(reverse,axis=(0,1)).tolist(),
        'finite':bool(np.isfinite(flow).all())}
    print(json.dumps(record),flush=True)
    if args.output:
        with args.output.open('x',encoding='utf-8') as handle:
            json.dump(record,handle,indent=2)
    assert record['finite'] and record['epe_mean'] < 1.0,record
    assert np.max(np.abs(np.array(record['reverse_median_flow'])+np.array([8,4]))) < 1,record
    engine.close()


if __name__=='__main__':main()
