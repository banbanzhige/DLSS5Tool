"""Unified zero-effect policy, precision, ownership and temporal restoration."""
from fractions import Fraction
from types import SimpleNamespace
import threading
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool import appearance, dlss_host_process as host, frame_generation as fg
from dlss5tool import render_cache as rc, residual_composition as residual
from dlss5tool.gui import App
from dlss5tool.video_export import compose_hdr_frame, compose_output_frame, resize_original


PROFILES = ('srgb', 'hdr10_pq', 'hdr10_hlg', 'scrgb')
ZERO_LOOKS = ({'total': 0}, {'darken': 0, 'brighten': 0}, {'color': 0, 'luminance': 0})
BASIC_ZERO = dict(intensity=0., local_tone=0., local_struct=0., skin_struct=0., use_auto_mask=False)


def pixels(profile, width=8, height=6):
    rng = np.random.default_rng(18)
    if profile == 'srgb':
        return rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    frame = rng.random((height, width, 4)).astype(np.float16)
    if profile == 'scrgb':
        frame[..., :3] = frame[..., :3]*6-.5
    else:
        frame[0, :4, 0] = [0, 2**-24, 2**-14, .001]
    return frame


def contract(profile, **extra):
    return dict(frame_format='rgba8' if profile == 'srgb' else 'rgba16f',
                color_profile=profile, host_backend='v2', **extra)


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('look', ZERO_LOOKS)
def test_source_identity_skips_native_and_transfer_functions(profile, look):
    source = pixels(profile)
    processed = np.zeros_like(source)
    settings = {'nr_quality': look, 'nr_residual': {'residualMultiplier': 1.8}}
    with mock.patch.object(residual, '_native_compose', side_effect=AssertionError('no GPU')), \
            mock.patch('dlss5tool.video_export._pq_eotf', side_effect=AssertionError('no decode')), \
            mock.patch('dlss5tool.video_export._hlg_eotf', side_effect=AssertionError('no decode')):
        assert residual.compose(source, processed, settings, profile=profile) is source
        for mix in (.25, 1, 5):
            result = (compose_output_frame(source[..., :3], processed[..., :3], mix=mix, settings=settings)
                      if profile == 'srgb' else
                      compose_hdr_frame(source, processed, mix=mix, profile=profile, settings=settings))
            np.testing.assert_array_equal(result, source[..., :3] if profile == 'srgb' else source)
            assert result.dtype == source.dtype


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('view', (0, 1, 2))
def test_zero_mix_does_not_hide_comparison_or_difference(profile, view):
    settings = contract(profile, output_mix=0, output_view=view)
    assert appearance.enhancement_required(settings) == (view != 0)
    a, b = pixels(profile), np.ones_like(pixels(profile))
    compose = compose_output_frame if profile == 'srgb' else compose_hdr_frame
    kwargs = {} if profile == 'srgb' else {'profile': profile}
    # Composition is unnecessary only in the final zero-mix view.
    with mock.patch.object(residual, 'compose', wraps=residual.compose) as operation:
        result = compose(a[..., :3] if profile == 'srgb' else a,
                         b[..., :3] if profile == 'srgb' else b,
                         view=view, mix=0, **kwargs)
        assert operation.call_count == int(view != 0)
        if view == 0:
            np.testing.assert_array_equal(result, a[..., :3] if profile == 'srgb' else a)


def test_policy_is_exact_validates_unused_fields_and_preserves_nonzero_work():
    assert appearance.enhancement_required({})
    assert appearance.enhancement_required({'intensity': 0})
    assert appearance.enhancement_required({'nr_quality': {'total': 1e-12}})
    assert appearance.enhancement_bypass(BASIC_ZERO) == 'native-zero'
    assert appearance.enhancement_required({**BASIC_ZERO, 'use_auto_mask': True})
    assert appearance.enhancement_required({**BASIC_ZERO, 'skin_struct': 1e-12})
    assert appearance.enhancement_required({**BASIC_ZERO, 'dlss_runtime': 'custom.dll'})
    for look in ZERO_LOOKS:
        assert appearance.enhancement_bypass({'nr_quality': look}) == 'source'
    for bad in ({'nr_quality': {'total': 0, 'regions': [[0, 0, 0, 1]]}},
                {'nr_quality': {'total': 0}, 'nr_residual': {'residualMultiplier': 0}},
                {'output_mix': float('nan')}, {'output_view': 5}, {'_nr_bypass': True}):
        with pytest.raises(ValueError):
            appearance.enhancement_required(bad)
    with pytest.raises(ValueError):
        residual.compose(np.zeros((1, 1, 4)), np.zeros((1, 2, 4)), {'nr_quality': {'total': 0}})
    with pytest.raises(ValueError, match='Unsupported HDR'):
        a = np.zeros((1, 1, 4), np.float16)
        residual.compose(a, a, {'nr_quality': {'total': 0}}, profile='invalid')


def test_raw_stage_policy_is_shared_with_interim_preview_identity():
    from dlss5tool import appearance
    neutral = dict(frame_generation_multiplier=1, output_mix=1, output_size=None)
    assert rc.render_identity('source', neutral) == rc.render_identity('source', appearance.raw_settings(neutral))
    assert appearance.raw_settings({'nr_quality': {'total': 0}})['_nr_bypass'] == 'source'
    assert appearance.raw_settings(BASIC_ZERO)['_nr_bypass'] == 'native-zero'


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('reason', ('source', 'native-zero'))
def test_bypass_session_owns_output_without_host_or_guidance(profile, reason):
    settings = contract(profile, guidance_mode=1, **(BASIC_ZERO if reason == 'native-zero'
                                                     else {'nr_quality': {'total': 0}}))
    with mock.patch.object(host, '_HostSession', side_effect=AssertionError('no NR host')), \
            mock.patch.object(host, '_open_shared_memory', side_effect=AssertionError('no IPC')):
        live = host.ProcessLive(8, 6, settings)
        assert live.backend == 'bypass' and not live.supports_async
        assert live.max_in_flight == 1 and live.pending == 0
        assert live.guidance_info == live.guidance_metrics == live.adapter_info == {}
        frame = pixels(profile)
        result = live.process(frame[:, ::-1])
        expected = frame[:, ::-1].copy()
        if reason == 'native-zero' and profile != 'srgb':
            expected[..., :3] = np.clip(expected[..., :3], 0, 1)
        np.testing.assert_array_equal(result, expected)
        assert result.flags.c_contiguous and not np.shares_memory(result, frame)
        assert live._session._input is None
        with pytest.raises(ValueError):
            live.process(frame[:2])
        with pytest.raises(host.HostProcessError):
            live.process_device(frame)
        assert live._session._input is None
        live.resize(4, 3)
        result = live.process(pixels(profile, width=4, height=3))
        assert result.shape == (3, 4, 4)
        live.close()
        live.close()


class HostFixture:
    backend = 'v2'
    adapter_info = {'name': 'fixture'}
    max_in_flight = 2
    supports_async = True
    tiled = False
    guidance_info = {}
    guidance_metrics = {}

    def __init__(self):
        self.calls, self._closed, self.queue = [], False, 0

    def copy_input(self, frame):
        self.input = frame.copy()

    def copy_output(self):
        return self.input.copy()

    def request(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if operation == 'pending':
            return {'pending': self.queue}
        if operation == 'guidance_preview':
            return {'images': {'flow': self.input.copy()}, 'reset': kwargs['reset']}
        return {'has_output': True, 'accepted': True}

    def close(self):
        self._closed = True


@pytest.mark.parametrize('profile', PROFILES)
def test_restoring_nr_replaces_source_cache_and_resets_first_frame(profile):
    first, second = HostFixture(), HostFixture()
    with mock.patch.object(host.ProcessLive, '_start_session', side_effect=[first, second]) as start:
        live = host.ProcessLive(8, 6, contract(profile))
        frame = pixels(profile)
        live.process(frame)
        live.update({'nr_quality': {'total': 0}})
        assert first._closed and live.enhancement_bypassed
        np.testing.assert_array_equal(live.process(frame), frame)
        live.update({'nr_quality': {'total': .5}})
        live.process(frame, reset=False)
        live.process(frame, reset=False)
        process = [kwargs['reset'] for operation, kwargs in second.calls if operation == 'process']
        assert process == [True, False]
        assert start.call_count == 2
        live.close()
        assert second._closed


def test_failed_restore_keeps_bypass_usable_and_pending_frames_block_switch():
    live = host.ProcessLive(8, 6, {'nr_quality': {'total': 0}})
    with mock.patch.object(live, '_start_session', side_effect=host.HostProcessError('cannot restore')):
        with pytest.raises(host.HostProcessError, match='cannot restore'):
            live.update({'nr_quality': {'total': 1}})
    assert live.enhancement_bypassed
    np.testing.assert_array_equal(live.process(pixels('srgb')), pixels('srgb'))
    live.close()
    fixture = HostFixture()
    with mock.patch.object(host.ProcessLive, '_start_session', return_value=fixture):
        live = host.ProcessLive(8, 6)
        fixture.queue = 1
        with pytest.raises(host.HostProcessError, match='Drain pending'):
            live.update({'nr_quality': {'total': 0}})
        assert not fixture._closed and not live.enhancement_bypassed
        live.close()


def test_guidance_inspection_during_bypass_is_explicit_and_temporary():
    fixture = HostFixture()
    live = host.ProcessLive(8, 6, {'nr_quality': {'total': 0}})
    with mock.patch.object(live, '_start_session', return_value=fixture):
        frame = pixels('srgb')
        images, reset = live.guidance_preview(frame, frame)
        np.testing.assert_array_equal(images['flow'], frame)
        assert not reset and fixture._closed
        assert all(operation == 'guidance_preview' for operation, _ in fixture.calls)
        assert live.enhancement_bypassed
        np.testing.assert_array_equal(live.process(frame), frame)
    live.close()


def test_preview_hash_only_invalidates_raw_cache_at_bypass_boundaries():
    values = dict(style=0, intensity=1., local_tone=1., local_struct=1., skin_struct=.5, use_auto_mask=False)
    key = lambda extra: App._hash_settings_dict(SimpleNamespace(), {**values, **extra})
    assert key({'nr_quality': {'total': .3}}) == key({'nr_quality': {'total': .7}})
    assert key({'nr_quality': {'total': 0}}) != key({'nr_quality': {'total': .7}})
    assert key({'output_mix': 0}) != key({'output_mix': 1})
    assert key({'output_mix': 0, 'output_view': 1}) == key({'output_mix': 1})


def test_preview_crossing_bypass_uses_model_refresh_but_regular_look_does_not():
    app = SimpleNamespace(_live=SimpleNamespace(settings={'nr_quality': {'total': 0}}),
        _uses_shared_render=lambda: False, _collect_settings=lambda: {'nr_quality': {'total': .5}},
        on_settings_change=mock.Mock())
    App.on_output_settings_change(app)
    app.on_settings_change.assert_called_once()


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('scale', (1, 2))
def test_real_cache_pipeline_zero_keeps_sr_source_and_restores_nr(tmp_path, monkeypatch, profile, scale):
    source = tmp_path / 'source.mp4'
    source.touch()
    monkeypatch.setenv('DLSS5TOOL_FG_LOG_ROOT', str(tmp_path / 'logs'))
    frame = pixels(profile, 128, 128)
    frame[..., 3] = 255 if profile == 'srgb' else 1
    hdr = profile != 'srgb'
    meta = dict(is_hdr=hdr, profile=profile, color_primaries='bt709' if profile in ('srgb', 'scrgb') else 'bt2020')
    capture = mock.Mock()
    capture.read.side_effect = [frame.copy(), None] if hdr else [(True, cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)), (False, None)]
    monkeypatch.setattr(fg, 'open_capture', lambda *_: capture)
    monkeypatch.setattr(fg, 'FFmpegHDRVideoReader', lambda *_: capture)
    monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **_: None)

    class SR:
        def __init__(self, *_, **__):
            pass
        def process(self, rgba):
            result = resize_original(rgba, (128*scale, 128*scale)).copy()
            result[..., :3] += .1 if hdr else 1
            return result
        def close(self):
            pass

    nr_frames = []
    class NR:
        def __init__(self, *_, **__):
            pass
        def process(self, rgba, reset=False):
            nr_frames.append(reset)
            result = rgba.copy()
            result[..., :3] += .125 if hdr else 5
            return result
        def close(self):
            pass
    monkeypatch.setattr('dlss5tool.super_resolution.ProcessSuperResolution', SR)
    monkeypatch.setattr(host, 'ProcessLive', NR)
    manager = rc.RenderCache(32*1024**2)
    manager.inspect = lambda *a, **k: (meta, Fraction(24), 128, 128, 1)
    values = dict(super_resolution_scale=scale, hdr_mode=True, nr_quality={'total': 0}, frame_generation_multiplier=1)
    try:
        session = manager.session(source, values)
        result, reference = session.wait(0, threading.Event())
        expected = SR().process(frame) if scale > 1 else frame
        np.testing.assert_array_equal(result, expected)
        np.testing.assert_array_equal(reference, resize_original(frame, (128*scale, 128*scale)))
        assert not nr_frames
        raw, spatial = manager.base, manager.spatial
        if scale > 1:
            np.testing.assert_array_equal(raw.peek(0).mix_source, expected)
        session = manager.session(source, {**values, 'nr_quality': {'total': .5}})
        result, _ = session.wait(0, threading.Event())
        assert raw.closed and manager.base is not raw and manager.spatial is spatial
        assert nr_frames == [True]
        assert manager.base.settings['_nr_bypass'] is None
    finally:
        manager.close()


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('kind', ('source', 'native-zero'))
def test_direct_render_still_composes_native_zero_hdr_residuals(tmp_path, monkeypatch, profile, kind):
    source = tmp_path / 'source.mp4'
    source.touch()
    frame = pixels(profile, 128, 128)
    frame[..., 3] = 255 if profile == 'srgb' else 1
    hdr = profile != 'srgb'
    meta = dict(is_hdr=hdr, profile=profile, color_primaries='bt709' if profile in ('srgb','scrgb') else 'bt2020')
    capture = mock.Mock()
    capture.read.side_effect = [frame.copy(), None] if hdr else [(True, cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)), (False, None)]
    monkeypatch.setattr(fg, 'open_capture', lambda *_: capture)
    monkeypatch.setattr(fg, 'FFmpegHDRVideoReader', lambda *_: capture)
    monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **_: None)
    factory = mock.Mock(side_effect=AssertionError('must not create NR'))
    monkeypatch.setattr(host, 'ProcessLive', factory)
    values = dict(hdr_mode=True, output_mix=.5, nr_quality={'total': 0 if kind == 'source' else .5})
    if kind == 'native-zero':
        values.update(BASIC_ZERO)
    emitted = []
    report = fg.export_video(source, None, multiplier=1, enhance=True, settings=values,
        frame_sink=lambda i, p, r: emitted.append(p.copy()), log_dir=tmp_path/'logs',
        source_inspector=lambda *a, **k: (meta, Fraction(24), 128, 128, 1))
    expected_nr = appearance.bypass_frame(frame, kind)
    if hdr:
        expected = compose_hdr_frame(frame, expected_nr, mix=.5, profile=profile, settings=values)
    else:
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
        expected = cv2.cvtColor(compose_output_frame(bgr, cv2.cvtColor(expected_nr, cv2.COLOR_RGBA2BGR), mix=.5, settings=values), cv2.COLOR_BGR2RGBA)
    np.testing.assert_array_equal(emitted[0], expected)
    assert report['enhancement_bypassed'] and report['enhancement_bypass'] == kind
    factory.assert_not_called()


@pytest.mark.parametrize('profile', ('hdr10_pq', 'hdr10_hlg'))
def test_gui_hdr_export_uses_bypass_without_changing_half_precision(tmp_path, monkeypatch, profile):
    from dlss5tool import gui
    frame = pixels(profile, 128, 128)
    reader = mock.Mock()
    reader.read.side_effect = [frame, None]
    written = []
    writer = SimpleNamespace(encoder_name='fixture', audio_mode='none', write=lambda a: written.append(a.copy()),
                             finish=lambda: None, abort=lambda: None)
    monkeypatch.setattr(gui, 'FFmpegHDRVideoReader', lambda *a, **k: reader)
    monkeypatch.setattr(gui, 'FFmpegVideoWriter', lambda *a, **k: writer)
    monkeypatch.setattr(gui, 'find_ffmpeg', lambda: 'not-used')
    monkeypatch.setattr(gui, 'select_in_flight', lambda *a, **k: 1)
    monkeypatch.setattr(gui, 'query_gpu_memory', lambda **k: None)
    app = SimpleNamespace(_guidance_started=mock.Mock(), logln=lambda *a: None,
                          _raise_if_export_cancelled=lambda: None)
    app._cleanup_video_export = lambda actions=(), **kwargs: App._cleanup_video_export(app, actions, **kwargs)
    values = dict(nvenc_preset='p5', rate_control='quality', quality_profile='high', video_bitrate_mbps=20)
    with mock.patch.object(host, '_HostSession', side_effect=AssertionError('no host')):
        result = App._export_hdr_video(app, 'synthetic', {'is_hdr':True, 'profile':profile},
            str(tmp_path/'out.mp4'), 1, 24, 128, 128, {'nr_quality': {'total': 0}}, values, 0, .5)
    np.testing.assert_array_equal(written[0], frame)
    assert written[0].dtype == np.float16 and result['host_backend'] == 'bypass'
    assert result['frames'] == 1
    reader.close.assert_called_once()


def test_parallel_worker_skips_native_host_for_source_only(monkeypatch, tmp_path):
    import json
    import sys
    from dlss5tool import parallel_export_worker as worker
    frames = [pixels('srgb', 128, 128)[..., :3].copy() for _ in range(2)]
    cap = mock.Mock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda key: {cv2.CAP_PROP_FPS:24, cv2.CAP_PROP_FRAME_WIDTH:128,
        cv2.CAP_PROP_FRAME_HEIGHT:128, cv2.CAP_PROP_POS_FRAMES:0}[key]
    cap.read.side_effect = [(True, frame.copy()) for frame in frames]
    monkeypatch.setattr(worker, 'open_capture', lambda *_: cap)
    native = mock.Mock(side_effect=AssertionError('no host'))
    monkeypatch.setattr(worker.dlss_engine, 'Live', native)
    monkeypatch.setattr(worker.dlss_engine, 'LOG_PATH', worker.dlss_engine.LOG_PATH)
    written, receipts = [], []
    writer = SimpleNamespace(encoder_name='fixture', write=lambda a: written.append(a.copy()),
                             finish=lambda: None, abort=lambda: None)
    monkeypatch.setattr(worker, 'FFmpegVideoWriter', lambda *a, **k: writer)
    monkeypatch.setattr(worker, '_write_json', lambda path, payload, **k: receipts.append(payload))
    monkeypatch.setattr(sys, 'argv', ['worker', '--input', str(tmp_path/'in.mp4'), '--output', str(tmp_path/'out.mp4'),
        '--start','0','--end','2','--warmup','0','--settings',json.dumps({'nr_quality': {'total':0}}),
        '--progress',str(tmp_path/'progress.json'),'--result',str(tmp_path/'result.json'),'--worker-id','0'])
    assert worker.main() == 0
    assert receipts[-1]['host_backend'] == 'bypass' and receipts[-1]['kept_frames'] == 2
    for actual, expected in zip(written, frames):
        np.testing.assert_array_equal(actual, expected)
    native.assert_not_called()
