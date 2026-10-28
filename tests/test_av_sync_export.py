"""Real decode/encode/mux regressions for ordinary and parallel exports.

The GUI tests replace only enhancement pixels with an ordered identity worker;
source timing, queue draining, frame writing and audio muxing are production code.
"""
from collections import deque
from fractions import Fraction as F
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest import mock
import wave

import av
import cv2
import numpy as np
import pytest

from dlss5tool import app_settings, gui, parallel_export
from dlss5tool.gpu_export_runtime import create_video_writer
from dlss5tool.video_export import FFmpegVideoWriter, find_ffmpeg
from dlss5tool.video_export import probe_video_stream
from dlss5tool.video_timeline import inspect_video_timeline


def run(*args):
    result = subprocess.run([str(value) for value in args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))


@pytest.fixture(scope='module')
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp('av-sync')
    wav = root / 'pulse.wav'
    samples = np.zeros(4 * 48000, np.float64)
    pulse = np.arange(4800) / 48000
    samples[96000:100800] = np.sin(2 * np.pi * 880 * pulse) * .5
    with wave.open(str(wav), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes((samples * 32767).astype('<i2').tobytes())
    videos = {}
    for name in ('cfr', 'vfr'):
        path = root / f'{name}-video.mp4'
        stamps = tuple(str(F(i, 15)) for i in range(30)) + tuple(str(2 + F(i, 45)) for i in range(90))
        options = dict(frame_timestamps=stamps, timeline_end='4') if name == 'vfr' else {}
        writer = FFmpegVideoWriter(path, 128, 128, 30, use_nvenc=False, **options)
        try:
            for i in range(120):
                writer.write(np.full((128, 128, 3), 230 if i >= (30 if name == 'vfr' else 60) else 20, np.uint8))
            writer.finish()
        finally:
            writer.abort()
        videos[name] = path
    sources = {}
    for name, video, video_offset, audio_offset in (
        ('cfr', 'cfr', '0', '0'), ('vfr', 'vfr', '0', '0'),
        ('audio-late', 'cfr', '0', '0.75'), ('video-late', 'cfr', '1', '0'),
        ('both-offset', 'cfr', '1', '1.75'),
    ):
        path = root / f'{name}.mp4'
        run(find_ffmpeg(), '-v', 'error', '-nostdin', '-n', '-copyts',
            '-itsoffset', video_offset, '-i', videos[video],
            '-itsoffset', audio_offset, '-i', wav,
            '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', path)
        sources[name] = path
    return sources


def timeline(path):
    with av.open(str(path)) as source:
        video = source.streams.video[0]
        frames = list(source.decode(video))
        pts = [frame.pts * frame.time_base for frame in frames]
        event = next(pts[i] for i, frame in enumerate(frames)
                     if frame.to_ndarray(format='gray').mean() > 150)
        end = video.duration * video.time_base if video.duration is not None else None
    audio = []
    with av.open(str(path)) as source:
        for stream in source.streams.audio:
            audio.append([])
        for packet in source.demux(*source.streams.audio):
            if packet.dts is not None:
                index = next(i for i, stream in enumerate(source.streams.audio) if stream.index == packet.stream.index)
                audio[index].append((packet.pts * packet.time_base, bytes(packet)))
    return pts, event, audio, end


def assert_sync(source, target, tolerance=F(1, 1_000_000)):
    before, after = timeline(source), timeline(target)
    assert len(before[0]) == len(after[0]) == 120
    for a, b in zip(before[0], after[0]):
        assert abs((a - before[0][0]) - (b - after[0][0])) <= tolerance
    assert len(before[2]) == len(after[2]) > 0
    for source_audio, output_audio in zip(before[2], after[2]):
        assert [data for _, data in source_audio] == [data for _, data in output_audio]
        assert len(source_audio) == len(output_audio)
        for (a, _), (b, _) in zip(source_audio, output_audio):
            assert abs((before[1] - a) - (after[1] - b)) <= tolerance
    return after


class IdentityWorker:
    backend, adapter_info, max_in_flight = 'fixture', {}, 2

    def __init__(self, asynchronous=True):
        self.supports_async = asynchronous
        self.pending = deque()

    def update(self, settings):
        self.settings = dict(settings)

    def enqueue(self, frame, **kwargs):
        self.pending.append(frame.copy())
        return True

    def dequeue(self):
        return self.pending.popleft()

    def process(self, frame, **kwargs):
        return frame.copy()

    def close(self):
        self.pending.clear()


def export_app(monkeypatch, asynchronous=True):
    app = gui.App.__new__(gui.App)
    config = dict(app_settings.DEFAULTS, mode='single', frame_generation_multiplier=1,
                  super_resolution_scale=1, decode_cpu_fallback=True, guidance_mode=1,
                  output_view=0, output_mix=1)
    app._collect_settings = app._collect_export_settings = lambda: dict(config)
    app._ensure_shared_cache_pool = lambda: SimpleNamespace(name='fixture')
    app._confirm_super_resolution_export = lambda *args, **kwargs: True
    app._begin_export_ui = lambda text: setattr(app, '_export_t0', time.perf_counter())
    app._end_export_ui = app._remember_export = app.logln = app.set_progress = mock.Mock()
    app._export_cancel_event = threading.Event()
    app.root = mock.Mock()
    worker = IdentityWorker(asynchronous)
    app._ensure_live = lambda *args, **kwargs: worker
    app._close_live = mock.Mock()
    monkeypatch.setattr(gui, 'release_native_composer', lambda: None)
    monkeypatch.setattr(gui, '_postprocess_and_write',
        lambda writer, original, processed, *args, **kwargs: writer.write(cv2.cvtColor(processed, cv2.COLOR_RGBA2BGR)))
    monkeypatch.setattr(gui, 'FFmpegVideoWriter',
        lambda *args, **kwargs: create_video_writer(*args, use_nvenc=False, **kwargs))
    return app, worker


@pytest.mark.parametrize('name', ['cfr', 'vfr', 'audio-late', 'video-late', 'both-offset'])
def test_gui_export_preserves_source_frame_and_audio_timing(media, tmp_path, monkeypatch, name):
    app, worker = export_app(monkeypatch, asynchronous=name != 'cfr')
    output = tmp_path / 'export.mp4'
    result = app._export_video_source(media[name], {}, {}, {}, out_path=str(output), notify=False)
    assert result['success'], result['error']
    assert not worker.pending and worker.settings['guidance_mode'] == 1
    assert_sync(media[name], output)


def test_gui_does_not_publish_a_short_decode_as_success(media, tmp_path, monkeypatch):
    app, _ = export_app(monkeypatch)
    app._iter_frames = lambda *args, **kwargs: iter([(0, np.zeros((128, 128, 3), np.uint8))])
    output = tmp_path / 'truncated.mp4'
    result = app._export_video_source(media['cfr'], {}, {}, {}, out_path=str(output), notify=False)
    assert not result['success'] and '帧数' in result['error']
    assert not output.exists()


@pytest.mark.parametrize('name', ['vfr', 'video-late'])
def test_real_parallel_segments_preserve_source_timing(media, tmp_path, name):
    output = tmp_path / 'parallel.mp4'
    result = parallel_export.export_parallel(media[name], output,
        {'nr_quality': {'total': 0}, 'decode_cpu_fallback': True}, workers=2, warmup=3)
    assert result['frames'] == 120
    assert_sync(media[name], output)


@pytest.mark.parametrize('suffix', ['.mkv', '.mov'])
def test_audio_leading_video_is_preserved_across_containers(media, tmp_path, suffix):
    source = media['video-late']
    timing = inspect_video_timeline(source, allow_cpu_fallback=True)
    output = tmp_path / f'out{suffix}'
    capture = cv2.VideoCapture(str(source))
    writer = FFmpegVideoWriter(output, 128, 128, float(timing.rate), audio_source=source,
                               use_nvenc=False, **timing.writer_options())
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
        writer.finish()
    finally:
        writer.abort()
        capture.release()
    assert_sync(source, output, F(1, 1000) if suffix == '.mkv' else F(1, 1_000_000))


def test_multiple_audio_tracks_keep_individual_offsets(media, tmp_path):
    source = tmp_path / 'two-audio.mp4'
    run(find_ffmpeg(), '-v', 'error', '-nostdin', '-n', '-copyts',
        '-i', media['video-late'], '-itsoffset', '0.75', '-i', media['video-late'],
        '-map', '0:v:0', '-map', '0:a:0', '-map', '1:a:0', '-c', 'copy', source)
    timing = inspect_video_timeline(source, allow_cpu_fallback=True)
    output = tmp_path / 'multi-export.mp4'
    capture = cv2.VideoCapture(str(source))
    writer = FFmpegVideoWriter(output, 128, 128, float(timing.rate), audio_source=source,
                               use_nvenc=False, **timing.writer_options())
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
        writer.finish()
    finally:
        writer.abort()
        capture.release()
    assert len(assert_sync(source, output)[2]) == 2


@pytest.mark.parametrize('name', ['vfr', 'video-late'])
def test_gui_hdr_path_preserves_timing_with_real_hdr_decode_encode(media, tmp_path, monkeypatch, name):
    source = tmp_path / 'hdr-source.mp4'
    run(find_ffmpeg(), '-v', 'error', '-nostdin', '-n', '-copyts', '-i', media[name],
        '-map', '0:v:0', '-map', '0:a:0', '-c:a', 'copy', '-c:v', 'libx265',
        '-preset', 'ultrafast', '-x265-params',
        'log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc',
        '-pix_fmt', 'yuv420p10le', '-color_primaries', 'bt2020', '-color_trc', 'smpte2084',
        '-colorspace', 'bt2020nc', '-fps_mode', 'passthrough', '-enc_time_base', '1/1000000', source)
    app, worker = export_app(monkeypatch)
    monkeypatch.setattr(gui, 'ProcessLive', lambda *args, **kwargs: worker)
    from dlss5tool import video_export
    monkeypatch.setattr(video_export, 'write_composed_frame',
                        lambda writer, original, processed, *args, **kwargs: writer.write(processed))
    output = tmp_path / 'hdr-export.mp4'
    metadata = probe_video_stream(find_ffmpeg(), source)
    assert metadata['is_hdr']
    result = app._export_video_source(source, {}, {}, metadata, out_path=str(output), notify=False)
    assert result['success'], result['error']
    assert_sync(source, output)
    exported = probe_video_stream(find_ffmpeg(), output)
    assert exported['is_hdr'] and '10' in exported['pixel_format']


def audio_event(path):
    with av.open(str(path)) as source:
        for frame in source.decode(audio=0):
            samples = frame.to_ndarray().reshape(-1)
            scale = np.iinfo(samples.dtype).max if np.issubdtype(samples.dtype, np.integer) else 1
            found = np.flatnonzero(np.abs(samples.astype(np.float64)) / scale > .2)
            if len(found):
                return frame.pts * frame.time_base + F(int(found[0]), frame.sample_rate)
    raise AssertionError('Missing audio pulse')


def test_aac_fallback_keeps_audio_lead(media, tmp_path, monkeypatch):
    source = tmp_path / 'pcm-source.mkv'
    run(find_ffmpeg(), '-v', 'error', '-nostdin', '-n', '-copyts', '-i', media['video-late'],
        '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'pcm_s16le',
        '-avoid_negative_ts', 'disabled', source)
    app, _ = export_app(monkeypatch)
    output = tmp_path / 'aac-export.mp4'
    result = app._export_video_source(source, {}, {}, {}, out_path=str(output), notify=False)
    assert result['success'], result['error']
    source_delay = timeline(source)[1] - audio_event(source)
    output_delay = timeline(output)[1] - audio_event(output)
    assert abs(source_delay - output_delay) < F(2, 1000)
