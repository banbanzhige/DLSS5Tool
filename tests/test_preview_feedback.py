"""Regression contracts for first-paint latency and latest-parameter feedback."""
import threading
import time
from types import SimpleNamespace
from unittest import mock

import numpy as np
try:
    import pytest
except ImportError:
    class pytest:  # unittest discover does not collect these pytest-style functions
        class mark:
            @staticmethod
            def parametrize(*_args, **_kwargs):
                return lambda fn: fn

from dlss5tool import render_cache as rc
from dlss5tool.shared_render_preview import SharedRenderPreview
from tests.test_render_cache import renderer, wait_complete


def test_real_frame_published_before_next_frame_is_requested(tmp_path):
    from fractions import Fraction
    from dlss5tool import frame_generation as fg
    source = tmp_path/'fake.mp4'
    source.write_bytes(b'fixture')
    meta = {'is_hdr': False, 'profile': 'srgb', 'color_primaries': 'bt709'}
    cap = mock.Mock()
    cap.read.side_effect = [(True, np.zeros((128, 128, 3), np.uint8)),
                            (True, np.ones((128, 128, 3), np.uint8)), (False, None)]
    emitted = []
    def gate(index):
        if index == 1:
            assert emitted == [0], 'first real frame was held for its neighbour'
    with mock.patch.object(fg.cv2, 'VideoCapture', return_value=cap), \
            mock.patch('dlss5tool.super_resolution.query_gpu_memory', return_value=None):
        result = fg.export_video(source, None, multiplier=1, enhance=False,
            frame_sink=lambda i, *_: emitted.append(i), render_gate=gate,
            source_inspector=lambda *_: (meta, Fraction(24), 128, 128, 2), log_dir=tmp_path/'logs')
    assert emitted == [0, 1]
    assert result['real_frames'] == 2 and result['endpoint_holds'] == 0


def test_strength_preserves_spatial_mix_preserves_enhancement():
    manager = rc.RenderCache(200000)
    with mock.patch.object(rc, 'export_video', renderer):
        try:
            config = {'frame_generation_multiplier': 2, 'intensity': .5, 'output_mix': 1}
            first = manager.session('source', config)
            for i in range(24):
                first.wait(i, threading.Event())
            wait_complete(first)
            spatial, base = manager.spatial, manager.base
            spatial_count = spatial.new_frames
            second = manager.session('source', {**config, 'intensity': .7})
            assert manager.spatial is spatial
            assert base.closed
            for i in range(24):
                second.wait(i, threading.Event())
            wait_complete(second)
            assert spatial.new_frames == spatial_count
            base, count = manager.base, manager.base.new_frames
            third = manager.session('source', {**config, 'intensity': .7, 'output_mix': .2})
            for i in range(24):
                third.wait(i, threading.Event())
            wait_complete(third)
            assert manager.base is base
            assert base.new_frames == count
            assert sum(s.bytes for s in (spatial, base, third)) <= manager.limit
        finally:
            manager.close()


def test_inspection_is_cached_by_source_not_effect_settings(tmp_path):
    source = tmp_path/'source'
    source.write_bytes(b'x')
    manager = rc.RenderCache(1000)
    with mock.patch.object(rc, 'inspect_source', return_value=('meta', 24, 8, 8, 12)) as inspect:
        assert manager.inspect(source, threading.Event()) == manager.inspect(source, threading.Event())
        assert inspect.call_count == 1
        source.write_bytes(b'new')
        manager.inspect(source, threading.Event())
        assert inspect.call_count == 2


def test_metadata_lookup_does_not_seek_producer_back_to_zero():
    session = rc.RenderSession('source', {}, 100000, renderer=renderer)
    try:
        session.wait(8, threading.Event())
        session.wait_metadata(threading.Event())
        assert session.target == 8
    finally:
        session.close()


def app_stub():
    app = SharedRenderPreview()
    app.video = 'source'
    app._frame = 0
    app._hold_original = False
    app.view_var = SimpleNamespace(get=lambda: 'dlss')
    app._video_color_info = {}
    app._canvas_size = lambda: (100, 100)
    app._draw_fit = mock.Mock()
    app._draw_work_status = mock.Mock()
    app.set_status = mock.Mock()
    app._cancel_after = mock.Mock()
    app.root = SimpleNamespace(after=mock.Mock())
    app._shared_config = lambda: {'frame_generation_multiplier': 2, 'output_mix': 1}
    return app


def test_original_first_frame_does_not_need_render_session():
    app = app_stub()
    pixels = np.zeros((8, 8, 3), np.uint8)
    cap = mock.Mock()
    cap.read.return_value = (True, pixels)
    with mock.patch('dlss5tool.shared_render_preview.cv2.VideoCapture', return_value=cap):
        app._shared_source_preview()
        app._shared_source_thread.join(1)
        assert app._shared_source_preview()
    app._draw_fit.assert_called_once()
    assert app._draw_fit.call_args.kwargs['badge']
    cap.release.assert_called_once()
    assert not hasattr(app, '_render_cache_manager')


def test_stale_source_decode_cannot_paint_new_video():
    app = app_stub()
    app._shared_source_result = ((rc.file_identity('previous'), 0), np.zeros((8, 8, 3), np.uint8))
    app._shared_source_thread = SimpleNamespace(is_alive=lambda: True)
    assert not app._shared_source_preview()
    app._draw_fit.assert_not_called()


def test_rapid_changes_acknowledge_immediately_and_debounce_setup():
    app = app_stub()
    app._shared_interim = mock.Mock(return_value=False)
    with mock.patch('dlss5tool.shared_render_preview.time.perf_counter', return_value=100):
        for _ in range(20):
            app._shared_feedback()
        assert app._shared_ready() is None
    assert app._shared_revision == 20
    assert app.set_status.call_count == 20
    assert not hasattr(app, '_shared_setup_thread')
    assert app.root.after.call_args.args[0] == 16


def test_stale_configuration_cannot_start_new_pipeline():
    manager = rc.RenderCache(10000)
    with pytest.raises(rc.Cancelled):
        manager.session('source', {}, is_current=lambda: False)
    assert manager.spatial is None and manager.base is None


def test_output_mix_repaints_real_frame_without_recomputing_or_retargeting():
    app = app_stub()
    app._shared_config = lambda: {'frame_generation_multiplier': 2, 'output_mix': .25}
    base_config = {'frame_generation_multiplier': 1, 'output_mix': 1, 'output_size': None}
    base = rc.RenderSession('source', base_config, 100000, renderer=renderer)
    try:
        base.wait(0, threading.Event())
        base.target = 5
        app._render_cache_manager = SimpleNamespace(base=base)
        app._shared_paint = mock.Mock()
        assert app._shared_interim()
        assert base.target == 5
        assert app._shared_paint.call_count == 1
        app._shared_config = lambda: {'frame_generation_multiplier': 2, 'output_mix': .25, 'intensity': .9}
        assert not app._shared_interim(), 'must not paint old strength with new-parameter label'
    finally:
        base.close()
