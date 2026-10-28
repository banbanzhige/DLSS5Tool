"""Required hardware decoding, exact VFR seeks, and bounded decoder ownership."""
from fractions import Fraction
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool import gpu_decode as gpu
from dlss5tool.image_sequence import open_capture


class Frame:
    def __init__(self, index, pts, key=False):
        self.index, self.pts, self.key_frame = index, pts, key
        self.time_base = Fraction(1, 1000)
        self.width = self.height = 16
        self.rotation = 0
        self.format = SimpleNamespace(name='yuv420p')

    def to_ndarray(self, **_kwargs):
        return np.full((16, 16, 3), self.index, np.uint8)


class Container:
    def __init__(self, frames, *, hardware=True):
        self.frames = frames
        self.offset = 0
        self.closed = False
        self.duration = 200000
        self.stream = SimpleNamespace(codec_context=SimpleNamespace(is_hwaccel=hardware),
            average_rate=Fraction(24), guessed_rate=Fraction(24), time_base=Fraction(1, 1000),
            start_time=0, duration=200, frames=len(frames))
        self.streams = SimpleNamespace(video=[self.stream])
        self.seeks = []

    def decode(self, _stream):
        yield from self.frames[self.offset:]

    def seek(self, pts, **_kwargs):
        self.seeks.append(pts)
        self.offset = max((frame.index for frame in self.frames
                           if frame.key_frame and frame.pts <= pts), default=0)

    def close(self):
        self.closed = True


@pytest.fixture
def video(tmp_path, monkeypatch):
    source = tmp_path / 'input.mp4'
    source.write_bytes(b'fixture')
    frames = [Frame(i, pts, i in (0, 3)) for i, pts in enumerate((0, 40, 95, 130, 190))]
    containers = []
    def create(_source):
        container = Container(frames)
        containers.append(container)
        return container, container.stream
    monkeypatch.setattr(gpu, '_open', create)
    monkeypatch.setattr('dlss5tool.memory_budget.require_memory_headroom', lambda *_: None)
    return source, frames, containers


def test_nvdec_explicitly_disables_software_fallback():
    container = Container([])
    with mock.patch('av.open', return_value=container) as opened, \
            mock.patch('av.codec.hwaccel.HWAccel') as hardware:
        gpu._open('input.mp4')
    hardware.assert_called_once_with('cuda', allow_software_fallback=False)
    assert opened.call_args.kwargs['hwaccel'] is hardware.return_value
    assert container.stream.codec_context.thread_count == 1


def test_open_failure_closes_container_and_never_opens_opencv(monkeypatch):
    container = SimpleNamespace(streams=SimpleNamespace(video=[]), close=mock.Mock())
    with mock.patch('av.open', return_value=container), mock.patch.object(cv2, 'VideoCapture') as cpu:
        with pytest.raises(gpu.GPUDecodeError, match='未回退 CPU'):
            gpu._open('input.mp4')
    container.close.assert_called_once()
    cpu.assert_not_called()


def test_runtime_software_decoder_is_rejected():
    container = Container([Frame(0, 0)], hardware=False)
    with pytest.raises(gpu.GPUDecodeError, match='未确认硬件'):
        next(gpu._frames(container, container.stream))


def test_av1_default_decoder_is_replaced_by_explicit_gpu_decoder():
    source_decoder = SimpleNamespace(codec=SimpleNamespace(canonical_name='av1'),
        width=320, height=180, extradata=b'header', format=SimpleNamespace(name='yuv420p'),
        decode=mock.Mock(side_effect=AssertionError('software decoding is forbidden')))
    stream = SimpleNamespace(codec_context=source_decoder)
    metadata = mock.Mock()
    metadata.streams.video = [stream]
    packet = object()
    metadata.demux.return_value = iter([packet])
    hardware_decoder = mock.Mock(is_hwaccel=True)
    hardware_decoder.decode.return_value = [Frame(0, 0, True)]
    with mock.patch('av.open', side_effect=[RuntimeError('no compatible automatic decoder'), metadata]), \
            mock.patch('av.CodecContext.create', return_value=hardware_decoder) as create:
        container, actual_stream = gpu._open('av1.mp4')
        assert len(list(gpu._frames(container, actual_stream))) == 1
        container.seek(0, stream=stream)
        container.close()
    assert create.call_args.args == ('av1', 'r')
    assert hardware_decoder.pix_fmt == 'yuv420p'
    assert hardware_decoder.thread_count == 1
    hardware_decoder.decode.assert_called_once_with(packet)
    hardware_decoder.flush_buffers.assert_called_once()
    source_decoder.decode.assert_not_called()
    metadata.decode.assert_not_called()
    metadata.close.assert_called_once()


def test_checkpoint_memory_is_bounded_and_keeps_beginning(monkeypatch):
    monkeypatch.setattr(gpu, '_MAX_SOURCES', 2)
    monkeypatch.setattr(gpu, '_MAX_CHECKPOINTS', 3)
    with gpu._INDEX_LOCK:
        gpu._CHECKPOINTS.clear()
    for identity in ('a', 'b', 'c'):
        for index in range(10):
            gpu._remember(identity, index, Frame(index, index * 40, True))
    with gpu._INDEX_LOCK:
        assert len(gpu._CHECKPOINTS) == 2
        assert all(len(points) == 3 and 0 in points for points in gpu._CHECKPOINTS.values())
        gpu._CHECKPOINTS.clear()


def test_factory_propagates_gpu_failure_without_cpu_retry(tmp_path):
    source = tmp_path / 'input.mp4'
    source.write_bytes(b'fixture')
    with mock.patch.object(gpu, '_open', side_effect=gpu.decode_error('unsupported profile')), \
            mock.patch.object(cv2, 'VideoCapture') as cpu:
        with pytest.raises(gpu.GPUDecodeError, match='unsupported profile'):
            open_capture(source)
    cpu.assert_not_called()


def test_vfr_seek_is_by_frame_index_and_reuses_verified_checkpoints(video):
    source, frames, containers = video
    assert list(gpu.iter_timestamps(source, lambda: None)) == [str(Fraction(f.pts, 1000)) for f in frames]
    assert containers[-1].closed
    capture = open_capture(source)
    for index in (4, 1, 3, 0, 2):
        assert capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        assert capture.get(cv2.CAP_PROP_POS_FRAMES) == index
        ok, image = capture.read()
        assert ok and image[0, 0, 0] == index
        assert capture.get(cv2.CAP_PROP_POS_FRAMES) == index + 1
    assert 130 in containers[-1].seeks
    assert not capture.set(cv2.CAP_PROP_POS_FRAMES, float('nan'))
    assert not capture.set(cv2.CAP_PROP_POS_FRAMES, -1)
    capture.release()
    assert containers[-1].closed and not capture.isOpened()
    assert capture.read() == (False, None)


def test_unvisited_vfr_interval_is_counted_without_average_fps_guess(video):
    source, _, _ = video
    with gpu._INDEX_LOCK:
        gpu._CHECKPOINTS.clear()
    capture = open_capture(source)
    capture.set(cv2.CAP_PROP_POS_FRAMES, 4)
    assert capture.read()[1][0, 0, 0] == 4
    assert capture.read() == (False, None)
    capture.release()


def test_timestamp_cancellation_releases_gpu_container(video):
    source, _, containers = video
    def cancel():
        raise InterruptedError('cancelled')
    with pytest.raises(InterruptedError):
        next(gpu.iter_timestamps(source, cancel))
    assert containers[-1].closed


def test_timestamp_inspection_does_not_convert_or_retain_pixels(video):
    source, frames, _ = video
    for frame in frames:
        frame.to_ndarray = mock.Mock(side_effect=AssertionError('unnecessary RGB conversion'))
    assert len(list(gpu.iter_timestamps(source, lambda: None))) == len(frames)
    with gpu._INDEX_LOCK:
        assert all(isinstance(pts, int) for pts in gpu._CHECKPOINTS[gpu._identity(source)].values())


def test_timestamp_inspection_reports_actual_final_frame_duration(video):
    source, frames, _ = video
    frames[-1].duration = 17
    durations = []
    assert len(list(gpu.iter_timestamps(source, lambda: None, duration_sink=durations.append))) == len(frames)
    assert durations == [Fraction(17, 1000)]


def test_decode_open_error_reaches_export_consumer_instead_of_hanging():
    from dlss5tool import gui
    app = SimpleNamespace(video='input.mp4', _video_color_info={})
    with mock.patch.object(gui, 'open_capture', side_effect=gpu.decode_error('unsupported')):
        with pytest.raises(gpu.GPUDecodeError, match='unsupported'):
            next(gui.App._iter_frames(app))


def test_hdr_requires_gpu_frames_before_unchanged_color_conversion():
    from dlss5tool import video_export as video
    with mock.patch.object(video.subprocess, 'Popen') as process, \
            mock.patch.object(video.threading, 'Thread'):
        video.FFmpegHDRVideoReader('input.mp4', 128, 128,
            {'color_transfer': 'smpte2084', 'pix_fmt': 'yuv420p10le'}, ffmpeg='ffmpeg')
    command = process.call_args.args[0]
    assert command[command.index('-hwaccel') + 1] == 'cuda'
    assert command[command.index('-hwaccel_output_format') + 1] == 'cuda'
    assert command[command.index('-threads') + 1] == '1'
    assert command[command.index('-vf') + 1].startswith('hwdownload,format=p010le,')
