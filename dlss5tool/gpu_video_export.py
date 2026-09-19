"""Explicit native export candidate. Existing FFmpeg remains the default route."""
from pathlib import Path

import numpy as np

from dlss5tool.cuda_session import CudaSession
from dlss5tool.cuda_sdr_yuv import CudaSdrConverter
from dlss5tool.encoding_contract import frame_encoding_contract
from dlss5tool.nvenc_yuv import NativeYuvEncoder,config_from_contract
from dlss5tool.packet_mux import PacketVideoWriter


class NativeGpuVideoWriter:
    def __init__(self,output_path,width,height,fps,audio_source=None,*,native_library,sdr_ptx,
                 hdr_ptx=None,
                 device_ordinal=0,use_nvenc=True,nvenc_preset='p5',hdr_metadata=None,
                 rate_control='quality',quality_profile='high',video_bitrate_mbps=20.0,
                 output_size=None,codec='auto'):
        from dlss5tool.video_export import classify_color_info,resolve_encoder_size
        if use_nvenc is False:raise ValueError('GPU export was explicitly selected with software encoding')
        metadata=classify_color_info(hdr_metadata) if hdr_metadata else None
        self.is_hdr=bool(metadata and metadata['is_hdr'])
        if self.is_hdr and hdr_ptx is None:raise ValueError('HDR export requires verified HDR PTX')
        if nvenc_preset!='p5' or quality_profile not in ('high','balanced') or rate_control!='quality':
            raise ValueError('Native export acceptance currently covers P5 quality high/balanced only')
        if codec not in (('auto','hevc') if self.is_hdr else ('auto','h264')):raise ValueError('Unsupported native codec contract')
        ow,oh=resolve_encoder_size(width,height,output_size)
        resize=output_size is not None and (ow,oh)!=(width,height)
        self.frame_contract=frame_encoding_contract(width,height,fps,ow,oh,resize,metadata if self.is_hdr else None)
        if resize:raise ValueError('GPU export scaling has not been accepted')
        self.codec='hevc' if self.is_hdr else 'h264'
        self.config=config_from_contract(self.frame_contract,ow,oh,codec=self.codec,
            cq=19 if quality_profile=='high' else 23)
        self.width,self.height=width,height;self.output_width,self.output_height=ow,oh
        self.output_path=str(Path(output_path).resolve());self.fps=float(fps)
        self.uses_nvenc=True;self.encoder_name='NVENC HEVC HDR GPU' if self.is_hdr else 'NVENC H.264 GPU'
        self._frames=0;self._finished=False;self._failed=False
        self._cuda=self._converter=self._encoder=self._mux=None
        self._interop=[]
        try:
            self._cuda=CudaSession(device_ordinal)
            self.adapter_luid=self._cuda.luid
            self._upload=self._cuda.allocate(width*height*(16 if self.is_hdr else 4))
            with self._cuda.current():
                if self.is_hdr:
                    from dlss5tool.cuda_hdr_yuv import CudaHdrConverter
                    self._converter=CudaHdrConverter.from_contract(hdr_ptx,self.frame_contract)
                else:self._converter=CudaSdrConverter.from_contract(sdr_ptx,self.frame_contract)
                self._encoder=NativeYuvEncoder(native_library,self.config)
            self._mux=PacketVideoWriter(output_path,self.config,self._encoder.headers,audio_source=audio_source)
        except BaseException:
            self.abort();raise

    def write(self,frame):
        if self.is_hdr:
            if frame.dtype not in (np.float16,np.float32) or frame.shape!=(self.height,self.width,4):
                raise ValueError('Expected HDR RGBA float16/32 input')
        elif frame.dtype!=np.uint8 or frame.shape!=(self.height,self.width,3):raise ValueError('Expected SDR BGR uint8 input')
        if self._finished or self._failed:raise RuntimeError('GPU writer closed or failed')
        contiguous=np.ascontiguousarray(frame)
        try:
            self._cuda.upload(self._upload,contiguous)
            layout=('rgba16f' if frame.dtype==np.float16 else 'rgba32f') if self.is_hdr else 'bgr24'
            self.write_device(self._upload,contiguous.nbytes,layout,self.adapter_luid,strict_hdr=False)
        except BaseException:self.abort();raise

    def write_device(self,pointer,nbytes,layout,adapter_luid,row_pitch=None,*,strict_hdr=True):
        """Producer must wait its GPU fence and hold its resource until return."""
        if self._finished or self._failed:raise RuntimeError('GPU writer closed or failed')
        if adapter_luid!=self.adapter_luid:raise ValueError('Producer/encoder physical GPU mismatch')
        try:
            with self._cuda.current():
                extra={'strict':strict_hdr} if self.is_hdr else {}
                frame=self._converter.convert(pointer,nbytes,layout,self._frames,row_pitch,**extra)
                packets=self._encoder.write(frame)
            for packet in packets:self._mux.write(packet)
            self._frames+=1
        except BaseException:self.abort();raise

    def import_shared_output(self,**descriptor):
        from dlss5tool.cuda_interop import SharedCudaBuffer
        if self._finished or self._failed:raise RuntimeError('GPU writer closed or failed')
        shared=SharedCudaBuffer(self._cuda,**descriptor)
        self._interop.append(shared)
        return shared

    def _close_gpu(self):
        errors=[]
        if self._cuda:
            for shared in self._interop:
                try:shared.close()
                except Exception as error:errors.append(error)
            self._interop.clear()
            with self._cuda.current():
                for item in (self._encoder,self._converter):
                    if item:
                        try:item.close()
                        except Exception as error:errors.append(error)
            self._encoder=self._converter=None
            try:self._cuda.close()
            except Exception as error:errors.append(error)
            self._cuda=None
        if errors:raise RuntimeError(f'GPU export cleanup failed: {errors}')

    def finish(self):
        if self._finished:
            if self._failed:raise RuntimeError('GPU export was aborted')
            return
        try:
            with self._cuda.current():packets=self._encoder.finish()
            for packet in packets:self._mux.write(packet)
            self._close_gpu()
            self._mux.finish(self._frames);self.audio_mode=self._mux.audio_mode
            self._finished=True
        except BaseException:self.abort();raise

    def abort(self):
        self._failed=True;self._finished=True
        try:
            if self._mux:self._mux.abort()
        finally:self._close_gpu()
