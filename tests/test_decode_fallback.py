"""User-authorized software decode fallback and strict default policy."""
from types import SimpleNamespace
import threading
from unittest import mock

import cv2
import pytest
import numpy as np

from dlss5tool import app_settings, gpu_decode as gpu, render_cache
from dlss5tool.image_sequence import open_capture
from tests.test_gpu_decode import Container, Frame


@pytest.fixture
def software_source(tmp_path, monkeypatch):
    source = tmp_path / 'high10.mp4'
    source.write_bytes(b'fixture')
    containers = []
    def software(_source):
        container = Container([Frame(i, i * 40, i == 0) for i in range(4)], hardware=False)
        containers.append(container)
        return container, container.stream
    monkeypatch.setattr(gpu, '_open', mock.Mock(side_effect=gpu.decode_error('unsupported High10')))
    monkeypatch.setattr(gpu, '_open_software', mock.Mock(side_effect=software))
    class Capture:
        def __init__(self, *_):
            self.index = 0
            self.closed = False
            containers.append(self)
        def isOpened(self):
            return not self.closed
        def read(self):
            index = self.index
            self.index += 1
            return (True, np.full((16, 16, 3), index, np.uint8)) if index < 4 else (False, None)
        def set(self, prop, value):
            self.index = int(value)
            return True
        def get(self, prop):
            return self.index if prop == cv2.CAP_PROP_POS_FRAMES else 0
        def release(self):
            self.closed = True
    monkeypatch.setattr(gpu.cv2, 'VideoCapture', mock.Mock(side_effect=Capture))
    monkeypatch.setattr('dlss5tool.memory_budget.require_memory_headroom', lambda *_: None)
    return source, containers


def test_fallback_is_off_by_default_and_requires_explicit_boolean():
    assert app_settings.validate({})['decode_cpu_fallback'] is False
    for value in (None, False, 'true', 'false', 'yes'):
        assert gpu.decode_options({'decode_cpu_fallback': value}) == {}
    assert gpu.decode_options({'decode_cpu_fallback': True}) == {'allow_cpu_fallback': True}


def test_switch_persists_without_enabling_it_for_old_settings(tmp_path):
    path = tmp_path / 'settings.json'
    app_settings.save({'decode_cpu_fallback': True}, path)
    assert app_settings.load(path)['decode_cpu_fallback'] is True
    app_settings.save({'decode_cpu_fallback': False}, path)
    assert app_settings.load(path)['decode_cpu_fallback'] is False


def test_disabled_switch_never_starts_cpu_decoder(software_source):
    source, _ = software_source
    with pytest.raises(gpu.GPUDecodeError, match='先转码'):
        open_capture(source)
    gpu._open_software.assert_not_called()


def test_enabled_switch_decodes_and_seeks_on_cpu_only_after_gpu_failure(software_source):
    source, containers = software_source
    capture = open_capture(source, allow_cpu_fallback=True)
    assert capture.backend == gpu.CPU_BACKEND and 'unsupported High10' in capture.fallback_reason
    assert capture.read()[1][0, 0, 0] == 0
    assert capture.read()[1][0, 0, 0] == 1
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    assert capture.read()[1][0, 0, 0] == 0
    capture.release()
    gpu._open.assert_called_once()
    gpu.cv2.VideoCapture.assert_called_once()
    assert containers[-1].closed


def test_enabled_switch_keeps_high10_on_gpu_if_device_can_decode_it():
    hardware = SimpleNamespace(backend=gpu.BACKEND)
    with mock.patch.object(gpu, 'GPUVideoCapture', return_value=hardware) as opened, \
            mock.patch.object(gpu, 'SoftwareVideoCapture') as cpu:
        assert gpu.open_video_capture('high10.mp4', allow_cpu_fallback=True) is hardware
    opened.assert_called_once_with('high10.mp4')
    cpu.assert_not_called()


def test_missing_or_broken_cpu_decoder_reports_failure(software_source):
    source, _ = software_source
    with mock.patch.object(gpu.cv2, 'VideoCapture', side_effect=ValueError('damaged input')):
        with pytest.raises(RuntimeError, match='CPU 解码兜底失败.*damaged input'):
            open_capture(source, allow_cpu_fallback=True)


def test_memory_failure_does_not_trigger_a_decoder_retry():
    with mock.patch.object(gpu, 'GPUVideoCapture', side_effect=MemoryError('no RAM')), \
            mock.patch.object(gpu, 'SoftwareVideoCapture') as cpu:
        with pytest.raises(MemoryError):
            gpu.open_video_capture('source', allow_cpu_fallback=True)
    cpu.assert_not_called()


def test_timestamp_scan_uses_same_explicit_policy_and_reports_actual_backend(software_source):
    source, containers = software_source
    backends = []
    stamps = list(gpu.iter_timestamps(source, lambda: None,
        allow_cpu_fallback=True, backend_sink=backends.append))
    assert stamps == ['0', '1/25', '2/25', '3/25']
    assert backends == [gpu.CPU_BACKEND] and containers[-1].closed


def test_policy_change_invalidates_spatial_and_temporal_render_caches():
    for stage in ('spatial', 'base', 'output'):
        settings = {'_render_stage': stage, 'decode_cpu_fallback': False}
        assert render_cache.render_identity('source', settings) != render_cache.render_identity(
            'source', {**settings, 'decode_cpu_fallback': True})


def test_timestamp_cache_cannot_bypass_disabled_cpu_fallback():
    manager = render_cache.RenderCache(1024)
    with mock.patch.object(render_cache, 'inspect_source', return_value=('metadata', 24, 128, 128, 4)) as inspect:
        manager.inspect('source', threading.Event(), allow_cpu_fallback=True)
        manager.inspect('source', threading.Event(), allow_cpu_fallback=True)
        manager.inspect('source', threading.Event())
    assert inspect.call_count == 2
    assert inspect.call_args_list[0].kwargs['allow_cpu_fallback'] is True
    assert 'allow_cpu_fallback' not in inspect.call_args_list[1].kwargs


def test_hdr_retries_before_first_published_frame_and_reports_cpu_backend():
    from dlss5tool import video_export as video
    first = object()
    with mock.patch.object(video.FFmpegHDRVideoReader, '_start_decode') as start, \
            mock.patch.object(video.FFmpegHDRVideoReader, 'close') as close, \
            mock.patch.object(video.FFmpegHDRVideoReader, 'read', side_effect=[gpu.decode_error('unsupported'), first]):
        reader = video.FFmpegHDRVideoReader('source', 128, 128,
            {'color_transfer': 'smpte2084', 'pix_fmt': 'yuv420p10le'},
            ffmpeg='ffmpeg', allow_cpu_fallback=True)
    assert start.call_args_list == [mock.call(True), mock.call(False)]
    close.assert_called_once()
    assert reader._pending_frames == [first]


def test_export_uses_job_policy_and_logs_backend_on_consumer_thread():
    from dlss5tool import gui
    owner = threading.get_ident()
    logged = []
    capture = mock.Mock(backend=gpu.CPU_BACKEND)
    capture.read.side_effect = [(True, np.zeros((16, 16, 3), np.uint8)), (False, None)]
    app = SimpleNamespace(video='source', _video_color_info={},
        logln=lambda message: logged.append((threading.get_ident(), message)))
    with mock.patch.object(gui, 'open_capture', return_value=capture) as opened, \
            mock.patch.object(gui, 'bounded_frame_count', return_value=1):
        assert len(list(gui.App._iter_frames(app,
            decode_settings={'decode_cpu_fallback': True}))) == 1
    opened.assert_called_once_with('source', allow_cpu_fallback=True)
    assert logged == [(owner, '[解码] ' + gpu.CPU_BACKEND)]
    capture.release.assert_called_once()


def test_switching_fallback_off_releases_existing_cpu_preview_capture():
    from dlss5tool import gui
    app = gui.App.__new__(gui.App)
    previous = mock.Mock()
    app._cap = previous
    app.video, app._source_kind = 'source', 'video'
    app._export_settings = {'v_decode_cpu_fallback': SimpleNamespace(get=lambda: False)}
    app._background_preview_decoder = mock.Mock()
    app._schedule_settings_save = app.pause = app._on_effect_preview_change = mock.Mock()
    app.logln, app.set_status = mock.Mock(), mock.Mock()
    with mock.patch.object(gui, 'open_capture', side_effect=gpu.decode_error('unsupported')):
        app._on_decode_policy_change()
    previous.release.assert_called_once()
    assert app._cap is None
    assert app._decode_revision == 1
    app._background_preview_decoder.invalidate.assert_called_once()
    app.set_status.assert_called_once()
