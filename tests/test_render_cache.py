"""CPU contracts for shared SR/FG pixels, bounded history and encoding."""
import threading
import time
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from dlss5tool import render_cache as rc
from dlss5tool.frame_generation import Cancelled, check_cancel


def renderer(source, output, *, multiplier, frame_sink, metadata_sink,
             render_gate, cancel, input_session=None, **kwargs):
    metadata_sink({'source_frames': 12, 'hdr_metadata': None, 'width': 8, 'height': 8,
                   'output_rate': str(24*multiplier), 'source_rate': '24', 'source_metadata': {}})
    for i in range(12):
        render_gate(i)
        check_cancel(cancel)
        if input_session:
            pixels, reference = input_session.wait(i, cancel)
        else:
            pixels = np.full((8, 8, 4), i, np.uint8)
            reference = np.zeros_like(pixels)
        for j in range(multiplier):
            frame_sink(i*multiplier+j, pixels+j, reference)
    return {'generated_frames': 11*(multiplier-1)}


def wait_complete(session):
    deadline = time.monotonic()+3
    while not session.complete:
        if session.error:
            raise session.error
        assert time.monotonic() < deadline
        time.sleep(.005)


def test_pixel_key_ignores_encoding_but_not_pixels(tmp_path):
    source = tmp_path/'source'
    source.write_bytes(b'x')
    settings = {'frame_generation_multiplier': 2, 'super_resolution_scale': 2, 'output_mix': .7}
    key = rc.render_identity(source, settings)
    for value in ({'quality_profile': 'maximum'}, {'video_bitrate_mbps': 80},
                  {'output_view': 2}, {'guidance_cache_pool': 'other'}, {'ui_language': 'en_US'}):
        assert key == rc.render_identity(source, {**settings, **value})
    for value in ({'frame_generation_multiplier': 4}, {'super_resolution_scale': 4},
                  {'output_mix': .5}, {'output_size': (256, 144)}, {'hdr_mode': False},
                  {'dlss_runtime': '__bundled__'}):
        assert key != rc.render_identity(source, {**settings, **value})
    source.write_bytes(b'changed')
    assert key != rc.render_identity(source, settings)


@pytest.mark.parametrize('multiplier', [1, 2, 3, 4])
def test_complete_cache_reuse_and_immutable_pixels(multiplier):
    session = rc.RenderSession('missing-source', {'frame_generation_multiplier': multiplier},
                               100000, renderer=renderer)
    try:
        for i in range(12*multiplier):
            pixels, _ = session.wait(i, threading.Event())
            assert pixels[0, 0, 0] == i//multiplier+i%multiplier
            assert not pixels.flags.writeable
        wait_complete(session)
        before = session.new_frames
        for i in range(12*multiplier):
            assert session.request(i) is not None
        assert session.new_frames == before
    finally:
        session.close()


def test_one_pair_budget_sequential_seek_replay_and_close():
    session = rc.RenderSession('source', {}, 512, renderer=renderer)
    try:
        for i in range(12):
            assert session.wait(i, threading.Event())[0][0, 0, 0] == i
            assert session.bytes <= 512
        wait_complete(session)
        assert session.wait(2, threading.Event())[0][0, 0, 0] == 2
        assert session.replayed_frames >= 2
    finally:
        session.close()
    assert not session.thread.is_alive()
    with pytest.raises(Cancelled):
        session.wait_metadata(threading.Event())


def test_budget_too_small_errors_without_quality_downgrade():
    session = rc.RenderSession('source', {}, 1, renderer=renderer)
    try:
        with pytest.raises(RuntimeError, match='预算'):
            session.wait(0, threading.Event())
    finally:
        session.close()


def test_cancel_wait_does_not_destroy_cache():
    session = rc.RenderSession('source', {}, 512, renderer=renderer)
    try:
        session.wait(0, threading.Event())
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(Cancelled):
            session.wait(1, cancel)
        assert session.request(0) is not None
    finally:
        session.close()


def test_replay_fills_hole_without_recomputing_cached_tail():
    session = rc.RenderSession('source', {'frame_generation_multiplier': 2}, 100000, renderer=renderer)
    try:
        for i in range(24):
            session.wait(i, threading.Event())
        wait_complete(session)
        before = session.new_frames
        tail = session.request(23)
        with session.condition:
            for i in range(2, 6):
                session.bytes -= sum(p.nbytes for p in session.cache.pop(i))
        for i in range(24):
            session.wait(i, threading.Event())
        assert session.request(23) is tail
        assert session.new_frames-before <= 6
    finally:
        session.close()


def test_multiplier_change_reuses_upstream_and_other_pixels_invalidate():
    manager = rc.RenderCache(100000)
    with mock.patch.object(rc, 'export_video', renderer):
        try:
            first = manager.session('source', {'frame_generation_multiplier': 2})
            for i in range(24):
                first.wait(i, threading.Event())
            wait_complete(first)
            base = manager.base
            before = base.new_frames
            second = manager.session('source', {'frame_generation_multiplier': 4})
            assert first.closed
            assert manager.base is base
            for i in range(48):
                second.wait(i, threading.Event())
            wait_complete(second)
            assert base.new_frames == before
            assert manager.session('source', {'frame_generation_multiplier': 4, 'quality_profile': 'maximum'}) is second
            manager.session('source', {'frame_generation_multiplier': 4, 'intensity': .5})
            assert base.closed
        finally:
            manager.close()


class Writer:
    instances = []
    def __init__(self, path, *args, **kwargs):
        self.path = Path(path)
        self.frames = []
        self.aborted = False
        self.instances.append(self)
    def write(self, pixels):
        self.frames.append(pixels.copy())
    def finish(self):
        self.path.write_bytes(b'encoded')
    def abort(self):
        self.aborted = True


def test_warm_encoding_zero_compute_and_safe_cancellation(tmp_path):
    source = tmp_path/'source.mp4'
    source.write_bytes(b'source')
    session = rc.RenderSession(source, {'frame_generation_multiplier': 2}, 100000, renderer=renderer)
    try:
        for i in range(24):
            session.wait(i, threading.Event())
        wait_complete(session)
        with mock.patch('dlss5tool.video_export.FFmpegVideoWriter', Writer):
            report = rc.encode_cached(session, tmp_path/'output.mp4', {}, threading.Event(), lambda *args: None)
            assert report['cache_hits'] == 24
            assert report['new_frames'] == 0
            assert len(Writer.instances[-1].frames) == 24
            assert not Writer.instances[-1].aborted
            cancel = threading.Event()
            with pytest.raises(Cancelled):
                rc.encode_cached(session, tmp_path/'cancel.mp4', {}, cancel, lambda *args: cancel.set())
            assert Writer.instances[-1].aborted
            assert not (tmp_path/'cancel.mp4').exists()
            assert not list(tmp_path.glob('.*.cache-*'))
    finally:
        session.close()
