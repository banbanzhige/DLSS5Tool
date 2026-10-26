"""Bounded CUDA-frame consumers for neutral enhancement, FG and preview sinks.

Scene/flow proxies preserve the existing CPU reference numerically. Native input
and output pixels travel through mapped D3D buffers, with a single output lease
and per-subframe ACK. This does not change residual/region composition settings.
"""
from contextlib import ExitStack
from fractions import Fraction
from pathlib import Path
import threading

import cv2
import numpy as np

from dlss5tool.cuda_frame import CudaImage, ReadyCudaFrame, SharedCudaImage, FrameTiming
from dlss5tool.cuda_interop import SharedCudaBuffer


class DeviceConsumers:
    def __init__(self, session, width, height, rate, settings, components, log_dir,
                 emit, *, validate_hdr=None, telemetry=None):
        from dlss5tool import appearance
        self.session, self.width, self.height, self.rate = session, width, height, Fraction(rate)
        self.settings = dict(settings)
        self.profile = self.settings.get('color_profile', 'srgb')
        self.hdr = self.profile != 'srgb'
        self.layout = 'rgba16f' if self.hdr else 'rgba8'
        self.multiplier = self.settings.get('frame_generation_multiplier', 1)
        self.emit, self.telemetry = emit, telemetry
        self.frames = self.outputs = self.generated = self.cut_holds = 0
        self.scene_cuts = []
        self.previous = self.previous_image = self.previous_proxy = self.previous_scene = None
        self.nr = self.fg = self.flow = self.nr_image = self.fg_image = self.nr_input = None
        self.analyzer = self.previous_gray = self.previous_gray_image = None
        self._images, self._mappings = [], []
        self._closed = False
        self._failed = self._finished = False
        try:
            if components.get('analysis_ptx') and components.get('analysis_tables'):
                from dlss5tool.cuda_analysis import CudaAnalysis
                self.analyzer = CudaAnalysis(session,width,height,components['analysis_ptx'],
                    components['analysis_tables'],primaries=self.settings.get('color_primaries','bt2020') if self.hdr else 'bt709')
            needs_nr = appearance.enhancement_required(self.settings)
            if needs_nr and (appearance.active(self.settings) or self.settings.get('output_mix', 1) != 1
                             or self.settings.get('guidance_mode', 0)):
                raise ValueError('Device enhancement requires neutral composition and no guidance')
            if needs_nr:
                from dlss5tool.dlss_host_process import ProcessLive
                config = dict(self.settings, _gpu_export_host=components['device_host'], host_backend='v2',
                    host_auto_fallback=False, host_in_flight=1, host_persistent_buffers=True,
                    host_submission='merged', frame_format=self.layout, color_profile=self.profile)
                self.nr = ProcessLive(width, height, config)
                self.nr_input, _ = self._map(self.nr.gpu_input_create())
                mapping, pitch = self._map(self.nr.gpu_output_create())
                self.nr_image = self._image(mapping, pitch)
            if self.multiplier > 1:
                from dlss5tool.frame_generation import NativeStream
                from dlss5tool.nvofa import OpticalFlow, align_size
                self.fw, self.fh = align_size(width, height)
                with session.current():
                    self.flow = OpticalFlow(self.fw, self.fh, require_current=True, grid=1)
                directory = Path(log_dir)/'device-fg'
                directory.mkdir(parents=True, exist_ok=True)
                self.fg = NativeStream(width, height, self.multiplier, self.hdr, directory,
                    threading.Event(), gpu_worker=components['device_worker'], cuda_session=session,
                    color_profile=self.profile, validate_cuda_frame=validate_hdr)
                if self.fg.luid != session.luid:
                    raise RuntimeError('FG and CUDA frame owner selected different physical GPUs')
                mapping, pitch = self._map(dict(self.fg.shared_descriptor, pitch=self.fg.shared_pitch))
                self.fg_image = self._image(mapping, pitch)
                self.previous_image = CudaImage(session, width, height, layout=self.layout, color_profile=self.profile)
                self._images.append(self.previous_image)
                if self.analyzer:
                    self.previous_gray_image = CudaImage(session,self.fw,self.fh,layout='gray8',color_profile='analysis')
                    self._images.append(self.previous_gray_image)
        except BaseException:
            self.close()
            raise

    def _map(self, descriptor):
        values = dict(descriptor)
        pitch = values.pop('pitch')
        mapping = SharedCudaBuffer(self.session, **values)
        self._mappings.append(mapping)
        return mapping, pitch

    def _image(self, mapping, pitch):
        image = SharedCudaImage(mapping, self.width, self.height, pitch,
                                layout=self.layout, color_profile=self.profile)
        self._images.append(image)
        return image

    def _measure(self, name):
        from contextlib import nullcontext
        return self.telemetry.measure(name) if self.telemetry else nullcontext()

    def _emit(self, frame, source_index, kind):
        timing = FrameTiming(self.outputs, self.outputs, 1/(self.rate*self.multiplier), frame.timing.reset)
        # A new immutable timing view borrows the same pixels. No pointer or
        # producer buffer may be reused until the callback returns and closes.
        with ReadyCudaFrame(frame._owner, timing) as ordered:
            self.emit(ordered, source_index, kind)
        self.outputs += 1

    def feed(self, frame):
        if self._failed or self._finished or self._closed:
            raise RuntimeError('Device consumer cannot resume after completion/failure')
        try:
            return self._feed(frame)
        except BaseException:
            self._failed = True
            raise

    def _feed(self, frame):
        from dlss5tool.frame_generation import scene_thumbnail, is_cut
        from dlss5tool.guidance_color import analysis_rgba8
        if self._closed:
            raise RuntimeError('Device consumers closed')
        frame._check()
        if (frame.session is not self.session or (frame.width, frame.height, frame.layout, frame.color_profile)
                != (self.width, self.height, self.layout, self.profile) or frame.timing.sequence != self.frames):
            raise ValueError('Device consumer frame owner/geometry/color/sequence mismatch')
        with self._measure('scene_analysis'):
            if self.analyzer:
                with self.analyzer.convert(frame) as gray:
                    scene = self.analyzer.thumbnail(gray)
                raw_proxy = None
            else:
                raw_proxy = analysis_rgba8(frame._owner.download(frame), self.settings)
                scene = scene_thumbnail(raw_proxy)
            cut = self.previous_scene is not None and is_cut(self.previous_scene, scene)
        if cut:
            self.scene_cuts.append(self.frames)
        with ExitStack() as leases:
            processed = frame
            if self.nr:
                self.nr_image.begin_write()
                with self._measure('enhancement_device'):
                    self.nr.process_cuda_frame(frame, self.nr_input, reset=self.frames == 0 or cut)
                processed = leases.enter_context(self.nr_image.complete(frame.timing))
            if self.fg:
                with self._measure('flow_analysis'):
                    if self.analyzer:
                        gray = leases.enter_context(self.analyzer.convert(processed) if self.nr else
                            ReadyCudaFrame(self.analyzer._gray,frame.timing))
                        proxy = None
                    else:
                        gray = None
                        proxy = analysis_rgba8(processed._owner.download(processed), self.settings) if self.nr else raw_proxy
                    if self.previous is None or cut:
                        motion = np.zeros((self.height, self.width, 2), np.float32)
                    elif gray is not None:
                        motion = np.ascontiguousarray(self.flow.calculate_cuda_gray(gray,self.previous_gray)[:self.height,:self.width])
                    else:
                        a = cv2.copyMakeBorder(proxy[..., :3], 0, self.fh-self.height, 0,
                                              self.fw-self.width, cv2.BORDER_REPLICATE)
                        b = cv2.copyMakeBorder(self.previous_proxy[..., :3], 0, self.fh-self.height, 0,
                                              self.fw-self.width, cv2.BORDER_REPLICATE)
                        motion = np.ascontiguousarray(self.flow.calculate(a, b)[:self.height, :self.width])

                def generated(subframe, valid):
                    if self.previous is None:
                        return
                    if cut:
                        self._emit(self.previous, self.frames-1, 'cut-hold')
                        self.cut_holds += 1
                    else:
                        if not valid:
                            raise RuntimeError('Invalid generated frame; no repeated-frame fallback')
                        self.fg_image.begin_write()
                        with self.fg_image.complete(frame.timing) as output:
                            self._emit(output, self.frames-1, 'generated')
                        self.generated += 1

                with self._measure('frame_generation_device'):
                    self.fg.process_cuda_frame(processed, motion, self.previous is None or cut, generated)
                self.previous_proxy = proxy
                if gray is not None:
                    if self.previous_gray:
                        self.previous_gray.close()
                        self.previous_gray = None
                    self.previous_gray = self.previous_gray_image.copy_from(gray)
            self._emit(processed, self.frames, 'real')
            if self.fg:
                if self.previous:
                    self.previous.close()
                    self.previous = None
                self.previous = self.previous_image.copy_from(processed)
        self.previous_scene = scene
        self.frames += 1

    def finish(self):
        if self._closed or self._failed or self._finished:
            raise RuntimeError('Device consumers closed')
        if self.fg:
            for _ in range(self.multiplier-1):
                self._emit(self.previous, self.frames-1, 'endpoint')
        self._finished = True
        return dict(real_frames=self.frames, generated_frames=self.generated, scene_cuts=self.scene_cuts,
                    cut_holds=self.cut_holds, endpoint_holds=self.multiplier-1,
                    output_frames=self.outputs)

    def close(self):
        if self._closed:
            return
        errors = []
        if self.previous:
            self.previous.close()
            self.previous = None
        if self.previous_gray:
            self.previous_gray.close()
            self.previous_gray = None
        # Stop native producers before unmapping their buffers on error.
        for item in (self.fg, self.nr, self.flow, self.analyzer, *reversed(self._images), *reversed(self._mappings)):
            if item:
                try:
                    item.close()
                except Exception as error:
                    errors.append(str(error))
        if errors:
            raise RuntimeError('Device consumer cleanup failed: ' + '; '.join(errors))
        self._closed = True
