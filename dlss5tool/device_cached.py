"""GPU consumers for immutable cached frames, preserving separate NR/FG owners."""
from pathlib import Path

import numpy as np

from dlss5tool.cuda_frame import CudaImage, SharedCudaImage, FrameTiming
from dlss5tool.cuda_interop import SharedCudaBuffer
from dlss5tool.cuda_session import CudaSession


def adapter_for(settings, components):
    from dlss5tool.dlss_engine import available_render_adapters
    adapters=available_render_adapters()
    requested=settings.get('render_gpu','auto')
    adapter=next((item for item in adapters if item['id']==requested),None) if requested!='auto' else next(iter(adapters),None)
    if adapter is None:raise RuntimeError('Selected cached-frame GPU is unavailable')
    if [adapter['vendor_id'],adapter['device_id']] not in components['verified_devices']:
        return None
    return adapter


class DeviceEnhancement:
    def __init__(self,width,height,settings,components,adapter):
        from dlss5tool.dlss_host_process import ProcessLive
        self._closed=self._failed=False;self.frames=0
        self.cuda=self.nr=self.input=self.output=None
        self.mappings=[]
        try:
            self.cuda=CudaSession(adapter['cuda_index'])
            profile=settings.get('color_profile','srgb')
            from dlss5tool.device_memory import estimate_vram, require_vram
            require_vram(self.cuda, estimate_vram(width,height,hdr=profile!='srgb',nr=True,super_resolution=False))
            layout='rgba8' if profile=='srgb' else 'rgba16f'
            self.input=CudaImage(self.cuda,width,height,layout=layout,color_profile=profile)
            self.nr=ProcessLive(width,height,dict(settings,_gpu_export_host=components['device_host'],
                host_backend='v2',host_auto_fallback=False,host_in_flight=1,
                host_persistent_buffers=True,host_submission='merged'))
            self.adapter_info=self.nr.adapter_info;self.backend=self.nr.backend
            self.max_in_flight=1;self.supports_async=False
            values=dict(self.nr.gpu_input_create());self.input_pitch=values.pop('pitch')
            self.input_mapping=SharedCudaBuffer(self.cuda,**values);self.mappings.append(self.input_mapping)
            values=dict(self.nr.gpu_output_create());pitch=values.pop('pitch')
            mapping=SharedCudaBuffer(self.cuda,**values);self.mappings.append(mapping)
            self.output=SharedCudaImage(mapping,width,height,pitch,layout=layout,color_profile=profile)
        except BaseException:
            self.close();raise

    def process(self,rgba,reset=False):
        if self._closed or self._failed:raise RuntimeError('Cached enhancement closed or failed')
        try:
            with self.input.upload(rgba,FrameTiming(self.frames,reset=bool(reset))) as frame:
                self.output.begin_write()
                self.nr.process_cuda_frame(frame,self.input_mapping,reset=reset)
                with self.output.complete(frame.timing) as processed:
                    result=self.output.download(processed)
            self.frames+=1
            return result
        except BaseException:
            self._failed=True;raise

    def close(self):
        if self._closed:return
        errors=[]
        for item in (self.nr,self.output,*reversed(self.mappings),self.input,self.cuda):
            if item:
                try:item.close()
                except Exception as error:errors.append(str(error))
        if errors:raise RuntimeError('Cached enhancement cleanup failed: '+'; '.join(errors))
        self._closed=True


class DeviceFrameGeneration:
    def __init__(self,width,height,multiplier,hdr,log_dir,cancel,settings,components,adapter):
        from dlss5tool.cuda_analysis import CudaAnalysis
        from dlss5tool.cuda_hdr_pack import CudaHdrPacking
        from dlss5tool.frame_generation import NativeStream
        from dlss5tool.nvofa import OpticalFlow,align_size
        self._closed=self._failed=False;self.frames=0
        self.cuda=self.native=self.flow=self.analyzer=self.validator=None
        self.input=self.output=self.mapping=self.previous_gray=self.previous_image=None
        self.width,self.height,self.hdr=width,height,hdr
        try:
            self.cuda=CudaSession(adapter['cuda_index'])
            profile=settings.get('color_profile','srgb')
            from dlss5tool.device_memory import estimate_vram, require_vram
            require_vram(self.cuda, estimate_vram(width,height,hdr=hdr,multiplier=multiplier,super_resolution=False))
            layout='rgba16f' if hdr else 'rgba8'
            self.input=CudaImage(self.cuda,width,height,layout=layout,color_profile=profile)
            self.analyzer=CudaAnalysis(self.cuda,width,height,components['analysis_ptx'],components['analysis_tables'],
                primaries=settings.get('color_primaries','bt2020') if hdr else 'bt709')
            self.fw,self.fh=align_size(width,height)
            self.previous_image=CudaImage(self.cuda,self.fw,self.fh,layout='gray8',color_profile='analysis')
            with self.cuda.current():self.flow=OpticalFlow(self.fw,self.fh,require_current=True,grid=1)
            if hdr:self.validator=CudaHdrPacking(self.cuda,components['hdr_pack_ptx'],width,height,1,allocate_scratch=False)
            self.native=NativeStream(width,height,multiplier,hdr,log_dir,cancel,
                gpu_worker=components['device_worker'],cuda_session=self.cuda,color_profile=profile,
                validate_cuda_frame=self.validator.validate if hdr else None)
            if self.native.luid!=self.cuda.luid:raise RuntimeError('Cached FG selected a different physical GPU')
            self.luid,self.runtime_hash=self.native.luid,self.native.runtime_hash
            self.mapping=SharedCudaBuffer(self.cuda,**self.native.shared_descriptor)
            self.output=SharedCudaImage(self.mapping,width,height,self.native.shared_pitch,
                layout=layout,color_profile=profile)
        except BaseException:
            self.close();raise

    def process_cached(self,rgba,reset,consume):
        """Consume one generated lease at a time before the native reuse ACK."""
        if self._closed or self._failed:raise RuntimeError('Cached FG closed or failed')
        try:
            with self.input.upload(rgba,FrameTiming(self.frames,reset=bool(reset))) as frame,self.analyzer.convert(frame) as gray:
                motion=(np.zeros((self.height,self.width,2),np.float32) if reset or self.previous_gray is None else
                    np.ascontiguousarray(self.flow.calculate_cuda_gray(gray,self.previous_gray)[:self.height,:self.width]))
                def generated(index,valid):
                    self.output.begin_write()
                    with self.output.complete(frame.timing) as pixels:
                        if self.hdr and not reset:self.validator.validate(pixels)
                        consume(index,valid,pixels)
                self.native.process_cuda_frame(frame,motion,reset,generated)
                if self.previous_gray:self.previous_gray.close();self.previous_gray=None
                self.previous_gray=self.previous_image.copy_from(gray)
            self.frames+=1
        except BaseException:
            self._failed=True;raise

    def close(self):
        if self._closed:return
        if self.previous_gray:self.previous_gray.close();self.previous_gray=None
        errors=[]
        for item in (self.native,self.output,self.mapping,self.flow,self.analyzer,self.validator,
                     self.previous_image,self.input,self.cuda):
            if item:
                try:item.close()
                except Exception as error:errors.append(str(error))
        if errors:raise RuntimeError('Cached FG cleanup failed: '+'; '.join(errors))
        self._closed=True
