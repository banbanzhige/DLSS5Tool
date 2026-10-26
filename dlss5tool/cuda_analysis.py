"""Exact GPU analysis proxy and grayscale; render pixels keep their HDR format."""
import ctypes as C
from pathlib import Path

import cv2
import numpy as np

from dlss5tool.cuda_frame import CudaImage, ReadyCudaFrame
from dlss5tool.cuda_session import bind, check


class CudaAnalysis:
    def __init__(self, session, width, height, ptx, tables, *, primaries='bt2020'):
        from dlss5tool.nvofa import align_size
        if primaries not in ('bt709', 'bt2020'):
            raise ValueError('GPU analysis supports BT.709/2020 only')
        self.session, self.width, self.height, self.primaries = session, width, height, primaries
        self.fw, self.fh = align_size(width, height)
        self._module, self._stream = C.c_void_p(), C.c_void_p()
        self._buffers, self._tables = [], {}
        self._gray = self._rgba = None
        self._closed = self._failed = False
        self._launch = bind(session.cuda, 'cuLaunchKernel', [C.c_void_p]+[C.c_uint]*7+
                            [C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)])
        self._sync = bind(session.cuda, 'cuStreamSynchronize', [C.c_void_p])
        self._destroy = bind(session.cuda, 'cuStreamDestroy_v2', [C.c_void_p])
        self._unload = bind(session.cuda, 'cuModuleUnload', [C.c_void_p])
        try:
            self._gray = CudaImage(session, self.fw, self.fh, layout='gray8', color_profile='analysis')
            self._rgba = CudaImage(session, width, height)
            with np.load(tables, allow_pickle=False) as archive:
                fields = dict(pq=(65536,np.float32), hlg=(65536,np.float64), threshold32=(256,np.uint32),
                    threshold64=(256,np.uint64), matrix=(9,np.float32), correction32_keys=(None,np.uint32),
                    correction32_values=(None,np.uint8), correction64_keys=(None,np.uint64),
                    correction64_values=(None,np.uint8))
                counts = {}
                for name, (size,dtype) in fields.items():
                    array = archive[name]
                    if array.dtype != dtype or size is not None and array.size != size or array.size > 65536:
                        raise ValueError('Unexpected analysis table: '+name)
                    array = np.ascontiguousarray(array)
                    counts[name] = array.size
                    pointer = session.allocate(max(array.nbytes, 8))
                    self._buffers.append(pointer)
                    if array.size: session.upload(pointer, array)
                    self._tables[name] = pointer
                for bits in ('32','64'):
                    if counts['correction'+bits+'_keys'] != counts['correction'+bits+'_values']:
                        raise ValueError('Analysis correction key/value mismatch')
                self._counts = counts
            self._invalid = session.allocate(4)
            self._buffers.append(self._invalid)
            with session.current():
                source = C.create_string_buffer(Path(ptx).read_bytes())
                check(bind(session.cuda, 'cuModuleLoadData', [C.POINTER(C.c_void_p), C.c_void_p])(
                    C.byref(self._module), source), 'Analysis PTX')
                self._functions = {}
                for name in ('analysis_half', 'analysis_gray'):
                    function = C.c_void_p()
                    check(bind(session.cuda, 'cuModuleGetFunction',
                        [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p])(
                            C.byref(function), self._module, name.encode()), name)
                    self._functions[name] = function
                check(bind(session.cuda, 'cuStreamCreate', [C.POINTER(C.c_void_p), C.c_uint])(
                    C.byref(self._stream), 1), 'Analysis stream')
            session.synchronize()
        except BaseException:
            self.close()
            raise

    def _call(self, name, width, height, values):
        params = (C.c_void_p*len(values))(*[C.cast(C.byref(value), C.c_void_p).value for value in values])
        check(self._launch(self._functions[name], (width+15)//16, (height+15)//16, 1,
                           16,16,1,0,self._stream,params,None), name)

    def convert(self, frame):
        if self._closed or self._failed:
            raise RuntimeError('GPU analysis closed or failed')
        if not isinstance(frame, ReadyCudaFrame): raise TypeError('Completed CUDA frame required')
        frame._check()
        if (frame.session is not self.session or (frame.width,frame.height)!=(self.width,self.height)
                or (frame.layout,frame.color_profile) not in (('rgba8','srgb'),('rgba16f','hdr10_pq'),('rgba16f','hdr10_hlg'))):
            raise ValueError('GPU analysis owner/geometry/color mismatch')
        gray = self._gray.begin_write()
        try:
          with self.session.current():
            source, pitch = frame.pointer, frame.row_pitch
            if frame.layout == 'rgba16f':
                output = self._rgba.begin_write()
                check(bind(self.session.cuda,'cuMemsetD32Async',[C.c_uint64,C.c_uint,C.c_size_t,C.c_void_p])(
                    self._invalid,0,1,self._stream),'Analysis validity reset')
                values = [C.c_uint64(source),C.c_uint64(output),C.c_uint(self.width),C.c_uint(self.height),
                    C.c_uint64(pitch),C.c_uint64(self._rgba.row_pitch),
                    *[C.c_uint64(self._tables[key]) for key in ('pq','hlg','threshold32','threshold64','matrix')],
                    C.c_uint(frame.color_profile=='hdr10_hlg'),C.c_uint(self.primaries=='bt2020'),C.c_uint64(self._invalid)]
                for bits in ('32','64'):
                    values += [C.c_uint64(self._tables['correction'+bits+'_keys']),
                        C.c_uint64(self._tables['correction'+bits+'_values']),
                        C.c_uint(self._counts['correction'+bits+'_keys'])]
                self._call('analysis_half',self.width,self.height,values)
                source,pitch=output,self._rgba.row_pitch
            self._call('analysis_gray',self.fw,self.fh,[C.c_uint64(source),C.c_uint64(gray),
                C.c_uint(self.width),C.c_uint(self.height),C.c_uint(self.fw),C.c_uint(self.fh),
                C.c_uint64(pitch),C.c_uint64(self._gray.row_pitch)])
            check(self._sync(self._stream),'Analysis completion')
          if frame.layout == 'rgba16f' and int.from_bytes(self.session.download(self._invalid,4),'little'):
              raise ValueError('HDR analysis input contains non-finite RGB')
          self._timing = frame.timing
          self._source = frame
          return self._gray.complete(frame.timing,stream=self._stream.value)
        except BaseException:
            self._failed=True
            raise

    def thumbnail(self, frame):
        frame._check()
        # Preserve packed-RGBA INTER_AREA exactly. Only the 8-bit analysis
        # proxy crosses to CPU; full HDR/render pixels stay with their owner.
        return cv2.resize(self.rgba_snapshot(),(64,64),interpolation=cv2.INTER_AREA)

    def rgba_snapshot(self):
        """Read only the RGBA8 analysis proxy for exact CPU INTER_AREA resizing."""
        if self._source.layout == 'rgba8':
            return self._source._owner.download(self._source)
        with self._rgba.complete(self._timing) as frame:
            return self._rgba.download(frame)

    def close(self):
        if self._closed: return
        if self._gray and self._gray._leases: raise RuntimeError('Analysis gray output still leased')
        if self._rgba and self._rgba._leases: raise RuntimeError('Analysis RGB output still leased')
        with self.session.current():
            if self._stream.value:
                check(self._sync(self._stream),'Analysis final completion')
                check(self._destroy(self._stream),'Analysis stream destroy')
                self._stream=C.c_void_p()
            if self._module.value:
                check(self._unload(self._module),'Analysis module unload')
                self._module=C.c_void_p()
        for image in (self._gray,self._rgba):
            if image: image.close()
        for pointer in self._buffers: self.session.free(pointer)
        self._buffers.clear()
        self._closed=True
