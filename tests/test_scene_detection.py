"""Scene-cut equivalence and temporal contracts without native GPU fixtures."""
import ctypes
from fractions import Fraction
from types import SimpleNamespace
import threading
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool import frame_generation as fg, render_cache as rc
from dlss5tool.guidance_color import analysis_rgba8, validate_analysis_frame


PROFILES = ('srgb', 'hdr10_pq', 'hdr10_hlg', 'scrgb')


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('pure_sr', [False, True])
def test_export_window_restores_each_layers_preview_preference(monkeypatch, cancelled, pure_sr):
    layers = [rc.RenderSession('unused', {}, 65536) for _ in range(3)]
    for index, layer in enumerate(layers):
        layer.prefetch_ahead = 7+index
        layer.sequential_export = index == 1
        layer.upstream = layers[index+1] if index+1 < len(layers) else None
    def encode(*_args, **_kwargs):
        assert [layer.prefetch_ahead for layer in layers] == ([2, 2, 2] if pure_sr else [7, 8, 9])
        assert all(layer.sequential_export for layer in layers)
        assert all(layer.limit == 65536 for layer in layers)
        if cancelled:
            raise fg.Cancelled('fixture')
        return 'complete'
    monkeypatch.setattr(rc, '_encode_cached', encode)
    settings = {'nr_quality': {'total': 0.0 if pure_sr else .75}}
    try:
        if cancelled:
            with pytest.raises(fg.Cancelled):
                rc.encode_cached(layers[0], None, settings, threading.Event(), lambda *_: None)
        else:
            assert rc.encode_cached(layers[0], None, settings, threading.Event(), lambda *_: None) == 'complete'
        assert [layer.prefetch_ahead for layer in layers] == [7, 8, 9]
        assert [layer.sequential_export for layer in layers] == [False, True, False]
    finally:
        for layer in layers:
            layer.close()


def test_replay_keeps_known_cuts_for_reused_cached_tail(monkeypatch):
    def render(_source, _output, *, frame_sink, metadata_sink, render_gate, cancel, **_kwargs):
        def metadata(cuts):
            metadata_sink(dict(source_frames=4, scene_cuts=cuts))
        metadata(())
        for index in range(4):
            render_gate(index)
            fg.check_cancel(cancel)
            if index == 2:
                metadata((2,))
            frame = np.full((8, 8, 4), index, np.uint8)
            frame_sink(index, frame, frame)
        return {}
    session = rc.RenderSession('unused', {}, 3*8*8*4, renderer=render)
    # This fixture tests replay metadata at a fixed three-frame capacity;
    # pressure/reclaim behavior has its own coordinated-memory tests.
    monkeypatch.setattr(session, '_allowance', lambda: session.limit)
    try:
        for index in range(4):
            session.wait(index, threading.Event())
        assert session.metadata['scene_cuts'] == (2,)
        session.wait(0, threading.Event())
        assert 2 in session.cache
        session.wait(2, threading.Event())
        assert session.metadata['scene_cuts'] == (2,)
    finally:
        session.close()


@pytest.mark.parametrize('shape', [(64, 64), (128, 128), (130, 258), (540, 960), (2160, 3840)])
@pytest.mark.parametrize('channels', [3, 4])
def test_thumbnail_matches_legacy_rgb_and_owns_history(shape, channels):
    rng = np.random.default_rng(2610)
    frame = rng.integers(0, 256, (*shape, channels), dtype=np.uint8)
    frame.flags.writeable = False
    expected = cv2.resize(frame[..., :3], (64, 64), interpolation=cv2.INTER_AREA)
    actual = fg.scene_thumbnail(frame)
    np.testing.assert_array_equal(actual[..., :3], expected)
    assert not np.shares_memory(actual, frame)


@pytest.mark.parametrize('value,expected', [(114, False), (115, True), (0, False), (255, True)])
def test_cut_threshold_and_alpha_match_legacy(value, expected):
    previous = np.zeros((130, 258, 4), np.uint8)
    current = np.full_like(previous, value)
    current[..., 3] = 255
    old_a = cv2.resize(previous[..., :3], (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    old_b = cv2.resize(current[..., :3], (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    legacy = float(np.mean(np.abs(old_a-old_b)))/255 > .45
    assert legacy == expected
    assert fg.is_cut(previous, current) == legacy
    with mock.patch.object(fg.cv2, 'resize', side_effect=AssertionError('small proxy resized again')):
        assert fg.is_cut(fg_proxy(old_a), fg_proxy(old_b)) == legacy


def fg_proxy(rgb):
    return np.concatenate((rgb.astype(np.uint8), np.zeros((64, 64, 1), np.uint8)), axis=2)


@pytest.mark.parametrize('increment', [0, 1])
def test_cut_decision_at_threshold_matches_legacy_rounding(increment):
    thumbnail = np.full((64, 64, 4), 114, np.uint8)
    thumbnail[:48, :, :3] = 115  # RGB mean is exactly 114.75: the .45 boundary.
    thumbnail[-1, -1, 0] += increment
    current = np.repeat(np.repeat(thumbnail, 2, axis=0), 2, axis=1)
    previous = np.zeros_like(current)
    legacy = cv2.resize(current[..., :3], (64, 64), interpolation=cv2.INTER_AREA)
    expected = float(np.mean(legacy.astype(np.float32)))/255 > .45
    assert expected == bool(increment)
    assert fg.is_cut(previous, current) == expected


@pytest.mark.parametrize('profile', PROFILES)
def test_validation_keeps_legacy_rejections_and_alpha_contract(profile):
    hdr = profile != 'srgb'
    settings = dict(frame_format='rgba16f' if hdr else 'rgba8', color_profile=profile)
    good = np.zeros((129, 128, 4), np.float16 if hdr else np.uint8)
    validate_analysis_frame(good, settings)
    if hdr:
        # Alpha was never part of the guidance RGB finite check.
        good[..., 3] = np.nan
        validate_analysis_frame(good, settings)
        assert analysis_rgba8(good, settings).dtype == np.uint8
        for value in (np.nan, np.inf, -np.inf):
            bad = good.copy()
            bad[-1, -1, 1] = value
            with pytest.raises(ValueError, match='non-finite'):
                validate_analysis_frame(bad, settings)
            with pytest.raises(ValueError, match='non-finite'):
                analysis_rgba8(bad, settings)
        with pytest.raises(ValueError, match='RGBA16F/32F'):
            validate_analysis_frame(np.zeros(good.shape, np.uint8), settings)
        with pytest.raises(ValueError, match='explicit PQ'):
            validate_analysis_frame(good, {**settings, 'color_profile': 'unknown'})
    else:
        with pytest.raises(ValueError, match='RGBA8'):
            validate_analysis_frame(good.astype(np.float32), settings)


class Harness:
    def __init__(self, tmp_path, monkeypatch, profile, values=(0, 0, 1, 1)):
        self.path, self.profile = tmp_path, profile
        monkeypatch.setenv('DLSS5TOOL_FG_LOG_ROOT', str(tmp_path/'native-logs'))
        self.source = tmp_path/'source.mp4'
        self.source.touch()
        self.frames = []
        self.lives, self.natives, self.captures = [], [], []
        self.runs = 0
        hdr = profile != 'srgb'
        self.meta = dict(is_hdr=hdr, profile=profile,
            color_primaries='bt709' if profile in ('srgb', 'scrgb') else 'bt2020')
        for value in values:
            frame = np.full((128, 128, 4), value if hdr else value*255,
                            np.float16 if hdr else np.uint8)
            frame[..., 3] = 1 if hdr else 255
            self.frames.append(frame)
        self.settings = dict(hdr_mode=True, nr_quality={'total': .5}, output_mix=1)
        monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **_: None)
        monkeypatch.setattr(fg, 'compose_hdr_frame', lambda original, processed, **_: processed)
        monkeypatch.setattr('dlss5tool.video_export.compose_sdr_rgba',
                            lambda original, processed, **_: processed)
        harness = self

        class Capture:
            def __init__(self, *_args, **_kwargs):
                self.index, self.closed = 0, False
                harness.captures.append(self)
            def read(self):
                if self.index == len(harness.frames):
                    return None if hdr else (False, None)
                frame = harness.frames[self.index].copy()
                self.index += 1
                return frame if hdr else (True, cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR))
            def close(self):
                self.closed = True
            release = close

        class NR:
            adapter_info = None
            def __init__(self, *_args, **_kwargs):
                self.resets, self.closed = [], False
                harness.lives.append(self)
            def process(self, frame, reset=False):
                self.resets.append(reset)
                return frame.copy()
            def close(self):
                self.closed = True

        class FG:
            luid, runtime_hash = 1, 'test-fixture'
            def __init__(self, _w, _h, multiplier, *_args, **_kwargs):
                self.multiplier, self.resets, self.closed = multiplier, [], False
                harness.natives.append(self)
            def process(self, frame, _motion, reset=False):
                self.resets.append(reset)
                return [np.full_like(frame, .25 if hdr else 17)
                        for _ in range(self.multiplier-1)], True
            def close(self):
                self.closed = True

        class Flow:
            cuda, device = None, 0
            def __init__(self, w, h, **_kwargs):
                self.w, self.h = w, h
            def _bind(self, *_args):
                def luid(buffer, *_unused):
                    ctypes.memmove(buffer, (1).to_bytes(8, 'little'), 8)
                    return 0
                return luid
            def check(self, status, _label):
                assert status == 0
            def calculate(self, *_args):
                return np.zeros((self.h, self.w, 2), np.float32)
            def close(self):
                pass

        monkeypatch.setattr(fg, 'open_capture', Capture)
        monkeypatch.setattr(fg, 'FFmpegHDRVideoReader', Capture)
        monkeypatch.setattr('dlss5tool.dlss_host_process.ProcessLive', NR)
        monkeypatch.setattr(fg, 'NativeStream', FG)
        monkeypatch.setattr('dlss5tool.nvofa.OpticalFlow', Flow)

    def inspect(self, *_args, **_kwargs):
        return self.meta, Fraction(24), 128, 128, len(self.frames)

    def upstream(self):
        metadata = dict(source_metadata=self.meta, source_rate='24', output_rate='24',
            width=128, height=128, source_frames=len(self.frames), hdr_metadata=self.meta,
            scene_cuts=())
        def wait(index, cancel):
            fg.check_cancel(cancel)
            # Publish the authoritative cut before returning its frame.
            if index == 2:
                metadata['scene_cuts'] = (2,)
            frame = self.frames[index].copy()
            return frame, frame.copy()
        return SimpleNamespace(metadata=metadata, wait_metadata=lambda _: dict(metadata), wait=wait)

    def run(self, *, multiplier=1, upstream=None, cancel_after=None):
        self.runs += 1
        emitted, metadata, cancel = [], [], threading.Event()
        def sink(index, frame, reference):
            emitted.append((index, frame.copy(), reference.copy()))
            if cancel_after is not None and len(emitted) == cancel_after:
                cancel.set()
        settings = {**self.settings, **({'_render_stage': 'base'} if upstream is not None else {})}
        report = fg.export_video(self.source, None, multiplier=multiplier, enhance=True,
            settings=settings, frame_sink=sink, metadata_sink=metadata.append,
            input_session=upstream, source_inspector=self.inspect, cancel=cancel,
            log_dir=self.path/f'run-{self.runs}')
        return report, emitted, metadata


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('multiplier', [1, 2])
def test_direct_cut_resets_and_full_resolution_fg_history(tmp_path, monkeypatch, profile, multiplier):
    harness = Harness(tmp_path, monkeypatch, profile)
    with mock.patch.object(fg, 'scene_thumbnail', wraps=fg.scene_thumbnail) as thumbnails, \
            mock.patch.object(fg, 'analysis_rgba8', wraps=analysis_rgba8) as analysis:
        report, output, metadata = harness.run(multiplier=multiplier)
    assert thumbnails.call_count == 4
    assert all(call.args[0].shape == (128, 128, 4) for call in thumbnails.call_args_list)
    assert all(call.args[0].dtype == np.uint8 for call in thumbnails.call_args_list)
    assert analysis.call_count == (8 if multiplier == 2 else 4)
    assert harness.lives[0].resets == [True, False, True, False]
    assert harness.lives[0].closed and harness.captures[0].closed
    assert metadata[-1]['scene_cuts'] == (2,)
    assert report['real_frames'] == 4 and len(output) == 4*multiplier
    if multiplier == 2:
        assert harness.natives[0].resets == [True, False, True, False]
        assert harness.natives[0].closed
        # Cut interval holds the preceding source, retaining native HDR pixels.
        np.testing.assert_array_equal(output[3][1], harness.frames[1])
        assert report['cut_holds'] == 1 and report['generated_frames'] == 2


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('multiplier', [1, 2])
def test_downstream_uses_published_cuts_without_reanalysis(tmp_path, monkeypatch, profile, multiplier):
    harness = Harness(tmp_path, monkeypatch, profile, values=(0, 1, 0, 1))
    with mock.patch.object(fg, 'is_cut', side_effect=AssertionError('cuts recomputed')), \
            mock.patch.object(fg, 'scene_thumbnail', side_effect=AssertionError('scene recomputed')), \
            mock.patch.object(fg, 'analysis_rgba8', wraps=analysis_rgba8) as analysis:
        _, _, metadata = harness.run(multiplier=multiplier, upstream=harness.upstream())
    assert analysis.call_count == (4 if multiplier == 2 else 0)  # Full FG proxy remains.
    assert harness.lives[0].resets == [True, False, True, False]
    assert metadata[-1]['scene_cuts'] == (2,)


@pytest.mark.parametrize('profile', PROFILES[1:])
def test_downstream_still_rejects_nonfinite_hdr_before_nr(tmp_path, monkeypatch, profile):
    harness = Harness(tmp_path, monkeypatch, profile)
    harness.frames[0][0, 0, 0] = np.nan
    with pytest.raises(RuntimeError, match='non-finite'):
        harness.run(upstream=harness.upstream())
    assert harness.lives[0].resets == [] and harness.lives[0].closed


def test_downstream_preserves_verified_vsr_identity_in_report_and_metadata(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, 'srgb')
    upstream = harness.upstream()
    identity = dict(verified=True, backend='d3d12', luid_high=0, luid_low=321)
    upstream.metadata['super_resolution_adapter'] = identity
    report, _, metadata = harness.run(upstream=upstream)
    assert report['super_resolution_adapter'] == identity
    assert metadata[-1]['super_resolution_adapter'] == identity
    assert report['super_resolution_adapter'] is not identity


def test_cancel_and_new_session_reset_owned_scene_history(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, 'srgb')
    with pytest.raises(fg.Cancelled):
        harness.run(cancel_after=2)
    assert harness.lives[0].closed and harness.captures[0].closed
    _, _, metadata = harness.run()
    assert harness.lives[-1].resets == [True, False, True, False]
    assert metadata[-1]['scene_cuts'] == (2,)


@pytest.mark.parametrize('profile', ['srgb', 'hdr10_pq'])
def test_real_layered_cache_and_seek_rebuild_keep_reset_contract(tmp_path, monkeypatch, profile):
    harness = Harness(tmp_path, monkeypatch, profile)
    manager = rc.RenderCache(12*harness.frames[0].nbytes)
    manager.inspect = harness.inspect
    try:
        session = manager.session(harness.source, harness.settings)
        session.wait(3, threading.Event())
        first_live = harness.lives[0]
        assert first_live.resets == [True, False, True, False]
        assert session.metadata['scene_cuts'] == (2,)
        replayed, _ = session.wait(0, threading.Event())
        np.testing.assert_array_equal(replayed, harness.frames[0])
        assert len(harness.lives) >= 2
        assert harness.lives[-1].resets[0] is True
    finally:
        manager.close()
    assert all(live.closed for live in harness.lives)


def test_cached_export_reports_cuts_published_after_initial_metadata(tmp_path, monkeypatch):
    source = tmp_path/'source.mp4'
    source.touch()
    def render(_source, _output, *, frame_sink, metadata_sink, render_gate, cancel, **_kwargs):
        metadata = dict(source_frames=4, width=8, height=8, hdr_metadata=None,
                        source_metadata={}, source_rate='24', output_rate='24', scene_cuts=())
        metadata_sink(dict(metadata))
        for index in range(4):
            render_gate(index)
            fg.check_cancel(cancel)
            if index == 2:
                metadata['scene_cuts'] = (2,)
                metadata_sink(dict(metadata))
            frame = np.full((8, 8, 4), index, np.uint8)
            frame_sink(index, frame, frame)
        return {'generated_frames': 0}
    class Writer:
        def __init__(self, path, *_args, **_kwargs):
            self.path = path
        def write(self, _frame):
            pass
        def finish(self):
            self.path.write_bytes(b'test-fixture')
        def abort(self):
            pass
    monkeypatch.setattr('dlss5tool.gpu_export_runtime.create_video_writer', Writer)
    session = rc.RenderSession(source, {}, 65536, renderer=render)
    try:
        result = rc.encode_cached(session, tmp_path/'out.mp4', {}, threading.Event(), lambda *_: None)
        assert result['scene_cuts'] == (2,)
        assert result['real_frames'] == 4
    finally:
        session.close()
