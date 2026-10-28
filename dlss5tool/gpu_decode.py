"""NVIDIA video decoding; software fallback requires an explicit opt-in.

Frames are downloaded for the existing BGR/NGX interfaces. This does not claim
that conversion, frame transport, or the entire export stays on the GPU.
"""
from collections import OrderedDict
from fractions import Fraction
import math
import os
import threading

import cv2
import numpy as np


BACKEND = 'NVIDIA NVDEC（强制 GPU 解码）'
CPU_BACKEND = 'CPU 解码兜底（可能较慢）'
_CHECKPOINTS = OrderedDict()
_INDEX_LOCK = threading.Lock()
_MAX_SOURCES = 4
_MAX_CHECKPOINTS = 4096


class GPUDecodeError(RuntimeError):
    pass


def decode_error(detail):
    if isinstance(detail, GPUDecodeError):
        return detail
    return GPUDecodeError(
        'GPU 视频解码失败，任务已停止，未回退 CPU 解码。'
        '若素材规格不受显卡支持，请先转码后重新导入：'
        '8-bit SDR 可转 H.264 8-bit、4:2:0；'
        '10-bit 素材可转 H.265 Main10、4:2:0，保留原色彩、分辨率与帧率。'
        '也可在导出设置中开启“允许 CPU 解码兜底（较慢）”。'
        '若已使用兼容规格，请检查显卡驱动与解码组件。\n'
        + str(detail))


def decode_options(settings):
    """Only an explicit, validated true value allows software decode."""
    return {'allow_cpu_fallback': True} if (settings or {}).get('decode_cpu_fallback') is True else {}


def _open_software(source):
    import av
    container = av.open(str(source), mode='r', timeout=(20, 60))
    try:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 4
        return container, stream
    except Exception:
        container.close()
        raise


def _open(source):
    container = None
    try:
        import av
        from av.codec.hwaccel import HWAccel
        try:
            container = av.open(str(source), mode='r',
                hwaccel=HWAccel('cuda', allow_software_fallback=False), timeout=(20, 60))
        except RuntimeError as original:
            # PyAV's automatic AV1 choice is libdav1d, which has no NVDEC config.
            # Open only the demuxer, then use FFmpeg's explicit hardware AV1
            # decoder. No packets are decoded by the automatic software context.
            container = av.open(str(source), mode='r', timeout=(20, 60))
            stream = container.streams.video[0]
            if stream.codec_context.codec.canonical_name != 'av1':
                raise original
            container = _AV1Container(container)
            return container, stream
        stream = container.streams.video[0]
        if not stream.codec_context.is_hwaccel:
            raise decode_error('首个视频流不支持 GPU 解码')
        # FFmpeg's automatic CPU thread count can exhaust NVDEC decode surfaces.
        stream.codec_context.thread_count = 1
        return container, stream
    except Exception as error:
        if container is not None:
            container.close()
        if isinstance(error, (GPUDecodeError, MemoryError)):
            raise
        raise decode_error(error) from error


class _AV1Container:
    """Explicit NVDEC AV1 decoder for PyAV builds that default to libdav1d."""
    def __init__(self, container):
        import av
        from av.codec.hwaccel import HWAccel
        self.container = container
        source = container.streams.video[0].codec_context
        self.decoder = av.CodecContext.create('av1', 'r',
            hwaccel=HWAccel('cuda', allow_software_fallback=False))
        self.decoder.width, self.decoder.height = source.width, source.height
        self.decoder.extradata = source.extradata
        self.decoder.thread_count = 1
        # AV1 needs its coded software format before hardware format negotiation.
        # Leaving PyAV's initial CUDA format here makes get_buffer() fail.
        self.decoder.pix_fmt = source.format.name

    def __getattr__(self, name):
        return getattr(self.container, name)

    def decode(self, stream):
        for packet in self.container.demux(stream):
            yield from self.decoder.decode(packet)

    def seek(self, *args, **kwargs):
        self.container.seek(*args, **kwargs)
        self.decoder.flush_buffers()

    def close(self):
        self.container.close()
        self.decoder = None


def _frames(container, stream, *, require_hardware=True):
    try:
        for frame in container.decode(stream):
            decoder = getattr(container, 'decoder', stream.codec_context)
            if require_hardware and not decoder.is_hwaccel:
                raise decode_error('解码器未确认硬件加速')
            yield frame
    except Exception as error:
        if not require_hardware:
            raise RuntimeError('CPU 解码兜底失败：' + str(error)) from error
        if isinstance(error, GPUDecodeError):
            raise
        raise decode_error(error) from error


def _identity(source):
    path = os.path.abspath(source)
    stat = os.stat(path)
    return path, stat.st_size, stat.st_mtime_ns


def _remember(identity, index, frame):
    if not frame.key_frame or frame.pts is None:
        return
    with _INDEX_LOCK:
        points = _CHECKPOINTS.setdefault(identity, OrderedDict())
        points[index] = frame.pts
        # Keep the beginning as an exact fallback, plus recent keyframes.
        while len(points) > _MAX_CHECKPOINTS:
            del points[next(key for key in points if key != 0)]
        _CHECKPOINTS.move_to_end(identity)
        while len(_CHECKPOINTS) > _MAX_SOURCES:
            _CHECKPOINTS.popitem(last=False)


def iter_timestamps(source, check_cancel, *, allow_cpu_fallback=False, backend_sink=None, duration_sink=None):
    """Inspect real presentation timestamps without RGB conversion or pixel history."""
    capture = open_video_capture(source, allow_cpu_fallback=allow_cpu_fallback)
    last_duration = None
    try:
        if backend_sink:
            backend_sink(capture.backend)
        while True:
            check_cancel()
            frame = capture._next()
            if frame is None:
                break
            if frame.pts is None:
                raise decode_error('视频帧缺少时间戳')
            duration = getattr(frame, 'duration', None)
            last_duration = (Fraction(duration) * frame.time_base if duration and duration > 0 else None)
            yield str(Fraction(frame.pts) * frame.time_base)
        if duration_sink:
            duration_sink(last_duration)
    finally:
        capture.release()


class GPUVideoCapture:
    """Capture-compatible GPU reader with exact frame-index seeking.

    A small shared keyframe index is populated by sequential reads and timestamp
    inspection. Unknown intervals are decoded on GPU rather than guessing a VFR
    frame index from average FPS. No decoded-frame history is retained.
    """
    backend = BACKEND
    _software = False

    def __init__(self, source):
        self.source = str(source)
        self.container = None
        self._pending = None
        self.position = 0
        self._target = None
        try:
            self.identity = _identity(source)
            self._open_source()
            self._pending = next(self._iterator, None)
            if self._pending is None:
                raise decode_error('视频没有可解码画面')
            _remember(self.identity, 0, self._pending)
            self.rotation = int(round(getattr(self._pending, 'rotation', 0) / 90)) % 4
            self.width, self.height = self._pending.width, self._pending.height
            if self.rotation % 2:
                self.width, self.height = self.height, self.width
            rate = self.stream.average_rate or self.stream.guessed_rate
            self.fps = float(rate) if rate else 0.0
            self.count = self.stream.frames
            if not self.count and self.fps:
                seconds = (self.stream.duration * self.stream.time_base if self.stream.duration
                           else Fraction(self.container.duration or 0, 1_000_000))
                self.count = round(seconds * self.fps)
            self.timestamp_ms = 0.0
        except Exception as error:
            self.release()
            if isinstance(error, (GPUDecodeError, MemoryError)):
                raise
            if self._software:
                raise RuntimeError('CPU 解码兜底失败：' + str(error)) from error
            raise decode_error(error) from error

    def _open_source(self):
        self.container, self.stream = (_open_software(self.source) if self._software else _open(self.source))
        self._iterator = (_frames(self.container, self.stream, require_hardware=False)
                          if self._software else _frames(self.container, self.stream))

    def isOpened(self):
        return self.container is not None

    def get(self, prop):
        return {cv2.CAP_PROP_FRAME_COUNT: self.count, cv2.CAP_PROP_FPS: self.fps,
                cv2.CAP_PROP_FRAME_WIDTH: self.width, cv2.CAP_PROP_FRAME_HEIGHT: self.height,
                cv2.CAP_PROP_POS_FRAMES: self.position if self._target is None else self._target,
                cv2.CAP_PROP_POS_MSEC: self.timestamp_ms}.get(prop, 0)

    def set(self, prop, value):
        if prop != cv2.CAP_PROP_POS_FRAMES or not math.isfinite(value) or value < 0:
            return False
        self._target = int(value)
        return True

    def _next(self):
        frame, self._pending = self._pending, None
        if frame is None:
            frame = next(self._iterator, None)
        if frame is not None:
            _remember(self.identity, self.position, frame)
            self.position += 1
        return frame

    def _seek(self, target):
        with _INDEX_LOCK:
            points = _CHECKPOINTS.get(self.identity, {})
            anchor = max((index for index in points if index <= target), default=0)
            pts = points.get(anchor)
        if target < self.position or anchor > self.position:
            self.container.seek(pts if pts is not None else (self.stream.start_time or 0),
                                stream=self.stream, backward=True, any_frame=False)
            self._iterator = (_frames(self.container, self.stream, require_hardware=False)
                              if self._software else _frames(self.container, self.stream))
            self._pending = None
            self.position = anchor
            if pts is not None:
                found = False
                for frame in self._iterator:
                    if frame.pts == pts:
                        self._pending = frame
                        found = True
                        break
                    if frame.pts is not None and frame.pts > pts:
                        break
                if not found:
                    # An open GOP may not reproduce its checkpoint after seek.
                    # Reopen from the beginning to retain exact frame ownership.
                    self.container.close()
                    self._open_source()
                    self.position = 0
        while self.position < target:
            if self._next() is None:
                return

    def read(self):
        if not self.isOpened():
            return False, None
        try:
            if self._target is not None:
                target, self._target = self._target, None
                self._seek(target)
            frame = self._next()
            if frame is None:
                return False, None
            from dlss5tool.memory_budget import memory_allocation
            # Match the existing planar-YUV -> BGR color conversion. Passing NV12
            # directly to swscale can select a different rounding implementation.
            planar = {'nv12': 'yuv420p', 'p010le': 'yuv420p10le', 'p016le': 'yuv420p16le'}
            with memory_allocation(frame.width*frame.height*24,stage='decode_sdr'):
                if frame.format.name in planar:
                    frame = frame.reformat(format=planar[frame.format.name])
                image = frame.to_ndarray(format='bgr24')
                if self.rotation:
                    image = np.ascontiguousarray(np.rot90(image, self.rotation))
            if frame.pts is not None:
                self.timestamp_ms = float(frame.pts * frame.time_base) * 1000
            return True, image
        except Exception as error:
            self.release()
            if isinstance(error, (GPUDecodeError, MemoryError)):
                raise
            if self._software:
                raise RuntimeError('CPU 解码兜底失败：' + str(error)) from error
            raise decode_error(error) from error

    def release(self):
        self._pending = None
        self._iterator = iter(())
        if self.container is not None:
            self.container.close()
            self.container = None
        self.stream = None


class SoftwareVideoCapture:
    """Explicit legacy CPU BGR path; raw timestamp inspection owns a separate reader."""
    backend = CPU_BACKEND

    def __init__(self, source):
        self.source = str(source)
        self.container = None
        self.capture = None
        self._pending = None
        self._raw_frames = None
        try:
            self.capture = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE])
            if not self.capture.isOpened():
                raise RuntimeError('无法打开视频')
            ok, self._pending = self.capture.read()
            if not ok:
                raise RuntimeError('视频没有可解码画面')
        except Exception as error:
            self.release()
            raise RuntimeError('CPU 解码兜底失败：' + str(error)) from error

    def isOpened(self):
        return self.capture is not None and self.capture.isOpened()

    def get(self, prop):
        if prop == cv2.CAP_PROP_POS_FRAMES and self._pending is not None:
            return 0
        return self.capture.get(prop) if self.capture is not None else 0

    def set(self, prop, value):
        if self.capture is None:
            return False
        self._pending = None
        return self.capture.set(prop, value)

    def read(self):
        if self._pending is not None:
            frame, self._pending = self._pending, None
            return True, frame
        return self.capture.read() if self.capture is not None else (False, None)

    def _next(self):
        # Timestamp scanning needs exact PTS, not BGR conversion or FPS guesses.
        if self._raw_frames is None:
            self.capture.release()
            self.capture = None
            self._pending = None
            self.container, stream = _open_software(self.source)
            self._raw_frames = _frames(self.container, stream, require_hardware=False)
        return next(self._raw_frames, None)

    def release(self):
        self._pending = None
        self._raw_frames = None
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        if self.container is not None:
            self.container.close()
            self.container = None


def open_video_capture(source, *, allow_cpu_fallback=False):
    """Try GPU first. Opted-in fallback is only allowed before publishing a frame."""
    try:
        return GPUVideoCapture(source)
    except GPUDecodeError as error:
        if allow_cpu_fallback is not True:
            raise
        capture = SoftwareVideoCapture(source)
        capture.fallback_reason = str(error)
        return capture
