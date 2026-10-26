"""CPU contracts for shared SR/FG pixels, bounded history and encoding."""
import threading
import time
import unittest
from pathlib import Path
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


@pytest.mark.parametrize('dtype', [np.uint8, np.float16, np.float32])
def test_snapshot_shares_only_upstream_owned_immutable_pixels(dtype):
    pixels = np.full((8, 8, 4), 10, dtype)
    reference = np.zeros_like(pixels)
    owned, _, _ = rc._snapshot_pair(pixels, reference, pixels)
    shared, copied, borrowed = rc._snapshot_pair(*owned, owned.mix_source, source_pair=owned)
    assert shared[0] is owned[0] and shared[1] is owned[1]
    assert copied == 0 and borrowed == owned.nbytes
    pixels.fill(99)
    assert np.all(shared[0] == 10)
    del owned
    assert np.all(shared[0] == 10) and not shared[0].flags.writeable
    # External read-only arrays do not receive the cache's ownership promise.
    pixels.flags.writeable = False
    external, copied, _ = rc._snapshot_pair(pixels, pixels)
    assert external[0] is external[1] and external[0] is not pixels
    assert copied == pixels.nbytes and external.nbytes == pixels.nbytes


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


def test_failed_session_is_rebuilt_for_same_request():
    def failing_renderer(source, output, **kwargs):
        raise RuntimeError('synthetic render failure')

    manager = rc.RenderCache(100000)
    with mock.patch.object(rc, 'export_video', failing_renderer):
        try:
            first = manager.session('source', {'frame_generation_multiplier': 1})
            with pytest.raises(RuntimeError, match='synthetic render failure'):
                first.wait_metadata(threading.Event())
            assert first.error is not None
            second = manager.session('source', {'frame_generation_multiplier': 1})
            assert second is not first
            assert first.closed
            assert second.error is None
        finally:
            manager.close()


def test_failed_upstream_layers_are_rebuilt_for_same_request():
    manager = rc.RenderCache(100000)
    settings = {'frame_generation_multiplier': 2, 'output_mix': .75}
    try:
        first = manager.session('source', settings)
        old_base, old_spatial = manager.base, manager.spatial
        # The output worker can observe an upstream failure after its own
        # request has already been cached by the manager.
        first.error = RuntimeError('synthetic upstream failure')
        old_base.error = RuntimeError('synthetic upstream failure')
        second = manager.session('source', settings)
        assert second is not first
        assert manager.base is not old_base
        assert manager.spatial is old_spatial
    finally:
        manager.close()


def test_prefetch_ahead_is_bounded_and_configurable():
    entered_limit = threading.Event()
    rendered = []
    def bounded_renderer(source, output, *, frame_sink, metadata_sink,
                         render_gate, cancel, **kwargs):
        metadata_sink({'source_frames': 200})
        pixels = np.zeros((2, 2, 4), np.uint8)
        for index in range(200):
            if index == 26:
                entered_limit.set()
            render_gate(index)
            rendered.append(index)
            frame_sink(index, pixels, pixels)
        return {}

    session = rc.RenderSession('source', {}, 100000, renderer=bounded_renderer)
    try:
        session.set_prefetch_ahead(24)
        session.request(0)
        assert entered_limit.wait(3)
        time.sleep(.1)
        assert rendered == list(range(26))
        session.set_prefetch_ahead(48)
        deadline = time.monotonic() + 3
        while len(rendered) < 50 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(rendered) >= 50
    finally:
        session.close()


def test_sequential_export_retires_consumed_history_when_allowance_shrinks(monkeypatch):
    pixels = np.zeros((8, 8, 4), np.uint8)
    pair, _, _ = rc._snapshot_pair(pixels, pixels)
    session = rc.RenderSession('source', {}, 5*pair.nbytes)
    try:
        session.sequential_export = True
        session.target, session.produced = 5, 7
        session.complete = True
        session.cache.update((i, pair) for i in range(3, 8))
        session.bytes = 5*pair.nbytes
        monkeypatch.setattr(session, '_allowance', lambda: 3*pair.nbytes)
        # Ordinary nearest-frame eviction drops frame 7. It must survive a
        # processing reservation so export need not initialize the model again.
        assert session._reclaim()
        assert tuple(session.cache) == (5, 6, 7)
        for index in (5, 6, 7):
            assert session.request(index) is pair
            assert not session.restart
        assert session.bytes <= session._allowance()
    finally:
        session.close()


def test_sequential_export_bounds_lookahead_including_endpoint():
    session = rc.RenderSession('source', {}, 100000, renderer=renderer)
    session.prefetch_ahead = rc.EXPORT_PREFETCH_AHEAD
    session.sequential_export = True
    try:
        session.wait(0, threading.Event())
        with session.condition:
            assert session.condition.wait_for(lambda: session.produced >= 2, 3)
            assert not session.condition.wait_for(lambda: session.produced > 2, .15)
        assert set(session.cache) == {0, 1, 2}
        assert session.wait(1, threading.Event())[0][0, 0, 0] == 1
    finally:
        session.close()


def test_wait_cannot_miss_frame_published_after_cache_miss():
    release_producer, published = threading.Event(), threading.Event()
    reader = threading.current_thread()
    def render(_source, _output, *, frame_sink, metadata_sink, **_kwargs):
        metadata_sink({'source_frames': 1})
        assert release_producer.wait(3)
        frame = np.zeros((8, 8, 4), np.uint8)
        frame_sink(0, frame, frame)
        published.set()
        return {}
    session = rc.RenderSession('source', {}, 65536, renderer=render)
    class PublicationCondition(threading.Condition):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            if threading.current_thread() is reader and not self._is_owned() and not published.is_set():
                # Deterministically place publication in an unlocked gap between
                # the miss and wait, if the consumer ever introduces that gap.
                release_producer.set()
                assert published.wait(3)
            return result
        def wait(self, timeout=None):
            if threading.current_thread() is reader:
                assert 0 not in session.cache, 'wait lost a ready-frame notification'
                release_producer.set()
            return super().wait(timeout)
    session.condition = PublicationCondition(threading.RLock())
    try:
        assert session.wait(0, threading.Event())[0].shape == (8, 8, 4)
    finally:
        release_producer.set()
        session.close()


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
            session.set_prefetch(False)
            counts, measurements = [], []
            report = rc.encode_cached(session, tmp_path/'output.mp4', {}, threading.Event(), lambda *args: None,
                frame_progress=lambda done, total: counts.append((done, total)),
                performance_sink=measurements.append)
            assert counts == [(i, 24) for i in range(25)]
            assert measurements[0]['stages']['encode_write']['calls'] == 24
            assert session.prefetch, 'export restores buffering after a paused preview'
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


class ProgressForwardingTests(unittest.TestCase):
    def test_session_forwards_progress_into_renderer(self):
        seen = []
        def renderer(source, output, *, progress, metadata_sink, frame_sink,
                     render_gate, cancel, multiplier, **kwargs):
            progress('检查时间戳', 0.5, (12, 24))
            metadata_sink({
                'source_frames': 1, 'hdr_metadata': None, 'width': 8, 'height': 8,
                'output_rate': '24', 'source_rate': '24', 'source_metadata': {},
            })
            pixels = np.zeros((8, 8, 4), np.uint8)
            render_gate(0)
            frame_sink(0, pixels, pixels)
            return {}
        session = rc.RenderSession('source', {}, 100000, renderer=renderer)
        try:
            session.set_progress(lambda *args: seen.append(args))
            session.wait(0, threading.Event())
        finally:
            session.close()
        self.assertEqual(seen[0][0], '检查时间戳')
        self.assertEqual(seen[0][2], (12, 24))

    def test_set_progress_reaches_upstream_session(self):
        parent = rc.RenderSession('source', {}, 100000, renderer=renderer)
        child = rc.RenderSession('source', {'frame_generation_multiplier': 2}, 100000, renderer=renderer)
        try:
            child.upstream = parent
            marker = object()
            child.set_progress(marker)
            self.assertIs(child.progress, marker)
            self.assertIs(parent.progress, marker)
        finally:
            child.close()
            parent.close()
