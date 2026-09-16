"""Source-first comparison, without changing the SR/NR mixing contract."""
from fractions import Fraction
from types import SimpleNamespace
import threading
import time
from unittest import mock

import cv2
import numpy as np
import pytest

from dlss5tool import frame_generation as fg, render_cache as rc
from dlss5tool.shared_render_preview import SharedRenderPreview
from dlss5tool.video_export import compose_hdr_frame, resize_original


@pytest.mark.parametrize('scale', [1, 2, 4])
@pytest.mark.parametrize('hdr', [False, True])
def test_real_pipeline_keeps_source_and_mix_separate(tmp_path, monkeypatch, scale, hdr):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'synthetic')
    monkeypatch.setenv('DLSS5TOOL_FG_LOG_ROOT', str(tmp_path / 'logs'))
    dtype = np.float16 if hdr else np.uint8
    pixels = np.full((128, 128, 4), .1 if hdr else 20, dtype)
    pixels[..., 3] = 1 if hdr else 255
    pixels[:, 64:, 0] = .15 if hdr else 30
    meta = {'is_hdr': hdr, 'profile': 'hdr10_pq' if hdr else 'srgb',
            'color_primaries': 'bt2020' if hdr else 'bt709'}

    class SR:
        def __init__(self, *args, **kwargs):
            pass
        def process(self, frame):
            result = resize_original(frame, (128*scale, 128*scale)).copy()
            result[..., :3] += .1 if hdr else 40
            return result
        def close(self):
            pass

    class NR:
        def __init__(self, *args, **kwargs):
            pass
        def process(self, frame, **kwargs):
            result = frame.copy()
            result[..., :3] += .1 if hdr else 60
            return result
        def close(self):
            pass

    capture = mock.Mock()
    capture.read.side_effect = ([pixels.copy(), None] if hdr else
        [(True, cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR)), (False, None)])
    monkeypatch.setattr(fg.cv2, 'VideoCapture', lambda *args: capture)
    monkeypatch.setattr(fg, 'FFmpegHDRVideoReader', lambda *args: capture)
    monkeypatch.setattr('dlss5tool.super_resolution.ProcessSuperResolution', SR)
    monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **kwargs: None)
    monkeypatch.setattr('dlss5tool.dlss_host_process.ProcessLive', NR)
    manager = rc.RenderCache(32*1024**2)
    manager.inspect = lambda *args, **kwargs: (meta, Fraction(24), 128, 128, 1)
    settings = {'super_resolution_scale': scale, 'frame_generation_multiplier': 1,
                'output_mix': .5, 'hdr_mode': True}
    try:
        session = manager.session(source, settings)
        processed, original = session.wait(0, threading.Event())
        expected_original = resize_original(pixels, (128*scale, 128*scale))
        expected_sr = SR().process(pixels) if scale > 1 else pixels
        expected_nr = NR().process(expected_sr)
        if hdr:
            expected = compose_hdr_frame(expected_sr, expected_nr, mix=.5)
        else:
            expected = cv2.addWeighted(expected_sr, .5, expected_nr, .5, 0)
        np.testing.assert_array_equal(original, expected_original)
        np.testing.assert_array_equal(processed, expected)
        base = manager.base
        pair = base.peek(0)
        np.testing.assert_array_equal(pair.mix_source, expected_sr)
        assert not pair.mix_source.flags.writeable
        assert base.bytes == pair.nbytes
        assert base.bytes <= base.limit
        # A mix-only change must reuse both inference stages, including SR pixels.
        next_session = manager.session(source, {**settings, 'output_mix': 0})
        next_processed, next_original = next_session.wait(0, threading.Event())
        assert manager.base is base
        np.testing.assert_array_equal(next_processed, expected_sr)
        np.testing.assert_array_equal(next_original, expected_original)
    finally:
        manager.close()


@pytest.mark.parametrize('multiplier', [2, 3, 4])
def test_generated_timestamps_hold_real_original(tmp_path, monkeypatch, multiplier):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'synthetic')
    pairs = [rc.RenderPair(np.full((128, 128, 4), 100+i, np.uint8),
                          np.full((128, 128, 4), 20+i, np.uint8)) for i in range(2)]
    metadata = {'source_metadata': {'is_hdr': False, 'profile': 'srgb', 'color_primaries': 'bt709'},
                'source_rate': '24', 'width': 128, 'height': 128,
                'source_frames': 2, 'scene_cuts': ()}
    upstream = SimpleNamespace(wait_metadata=lambda *_: metadata, metadata=metadata,
                               wait=lambda index, *_: pairs[index])
    native = mock.Mock(luid=0, runtime_hash='mock')
    native.process.return_value = ([np.full((128, 128, 4), 200, np.uint8)]*(multiplier-1), True)
    factory = mock.Mock(return_value=native)
    monkeypatch.setattr(fg, 'NativeStream', factory)
    monkeypatch.setattr('dlss5tool.super_resolution.query_gpu_memory', lambda **kwargs: None)
    flow = mock.Mock()
    flow._bind.return_value = lambda *_: 0
    flow.calculate.return_value = np.zeros((128, 128, 2), np.float32)
    monkeypatch.setattr('dlss5tool.nvofa.OpticalFlow', lambda *_args, **_kwargs: flow)
    emitted = []
    fg.export_video(source, None, multiplier=multiplier, input_session=upstream,
        settings={'_render_stage': 'output'}, frame_sink=lambda i, p, r: emitted.append((p.copy(), r.copy())),
        log_dir=tmp_path/'logs')
    assert factory.call_count == 1, 'the original side must not run a second interpolation model'
    assert len(emitted) == 2*multiplier
    for i, (_, original) in enumerate(emitted):
        np.testing.assert_array_equal(original, pairs[i//multiplier][1])
    assert emitted[1][0][0, 0, 0] == 200


@pytest.mark.parametrize('selection,hold', [('compare', False), ('original', False), ('dlss', True)])
def test_shared_display_uses_original_side(selection, hold):
    app = SharedRenderPreview()
    app._shared_display_config = {'output_view': 0}
    app._hold_original = hold
    app.view_var = SimpleNamespace(get=lambda: selection)
    app.video = 'synthetic.mp4'
    app.timeline = SimpleNamespace(get=lambda: 0)
    app._sync_transport_labels = mock.Mock()
    app._canvas_size = lambda: (400, 300)
    app._blit_play_split = mock.Mock()
    app._draw_fit = mock.Mock()
    app.playing = True
    app._shared_status_time = time.perf_counter()
    original = np.full((128, 128, 4), 20, np.uint8)
    processed = np.full_like(original, 120)
    mix_source = np.full_like(original, 60)
    app._shared_paint(SimpleNamespace(metadata={}, multiplier=1),
                      rc.RenderPair(processed, original, mix_source), 0)
    call = app._blit_play_split if selection == 'compare' and not hold else app._draw_fit
    np.testing.assert_array_equal(call.call_args.args[0], original[..., :3])


def test_cache_accounts_for_mix_buffer_and_releases_it():
    pixels = [np.full((8, 8, 4), value, np.uint8) for value in (20, 60, 120)]
    def renderer(*args, frame_sink, metadata_sink, **kwargs):
        metadata_sink({'source_frames': 1})
        frame_sink(0, pixels[2], pixels[0], mix_source=pixels[1])
        return {}
    session = rc.RenderSession('source', {}, 768, renderer=renderer)
    try:
        pair = session.wait(0, threading.Event())
        assert session.bytes == pair.nbytes == 768
        for original in pixels:
            original[:] = 0
        assert pair.mix_source[0, 0, 0] == 60
        session.set_limit(512)
        assert session.bytes == 0 and not session.cache
    finally:
        session.close()


@pytest.mark.parametrize('dtype', [np.uint8, np.float16])
def test_resize_original_is_identity_at_source_size(dtype):
    pixels = np.ones((128, 128, 4), dtype)
    assert resize_original(pixels, (128, 128)) is pixels
    result = resize_original(pixels, (256, 256))
    assert result.dtype == dtype and result.shape == (256, 256, 4)
    np.testing.assert_array_equal(result, np.ones_like(result))
