"""Failure rollback, traceback ownership and pre-allocation RAM admission."""
from fractions import Fraction
import gc
import multiprocessing
from multiprocessing import shared_memory
import time
from types import SimpleNamespace
from unittest import mock
import weakref

import numpy as np
import pytest

from dlss5tool import memory_budget as mb, super_resolution as sr
from dlss5tool.deferred_error import ErrorSnapshot
from dlss5tool.dlss_host_process import ProcessLive, _HostSession
from dlss5tool.preview_jobs import LatestPreviewJob
from dlss5tool.render_cache import RenderSession

GIB = 1024**3
NO_MEMORY = dict(total_physical_bytes=32*GIB, free_physical_bytes=0, free_commit_bytes=0)


@pytest.mark.parametrize('failure', ['input_memory', 'output_memory', 'input_view',
    'output_view', 'pipe', 'process', 'start', 'handshake'])
def test_vsr_partial_construction_releases_resources_with_traceback_retained(failure):
    real_memory, real_array = shared_memory.SharedMemory, np.ndarray
    segments, counts = [], dict(memory=0, array=0)
    parent, child, process, context = mock.Mock(), mock.Mock(), mock.Mock(pid=None), mock.Mock()
    state = dict(alive=False)
    parent.poll.return_value = True
    parent.recv.return_value = dict(ok=True)
    context.Pipe.return_value = (parent, child)
    context.Process.return_value = process
    process.is_alive.side_effect = lambda: state['alive']

    def start():
        if failure == 'start':
            raise OSError('injected start failure')
        state['alive'], process.pid = True, 12345
    process.start.side_effect = start
    process.join.side_effect = lambda **kwargs: state.update(alive=False)
    if failure in ('pipe', 'process'):
        getattr(context, 'Pipe' if failure == 'pipe' else 'Process').side_effect = OSError('injected')

    def allocate(**kwargs):
        counts['memory'] += 1
        if failure == ('input_memory' if counts['memory'] == 1 else 'output_memory'):
            raise MemoryError('injected shared allocation failure')
        segment = real_memory(**kwargs)
        segment.close = mock.Mock(wraps=segment.close)
        segment.unlink = mock.Mock(wraps=segment.unlink)
        segments.append(segment)
        return segment

    def array(*args, **kwargs):
        counts['array'] += 1
        if failure == ('input_view' if counts['array'] == 1 else 'output_view'):
            raise MemoryError('injected view allocation failure')
        return real_array(*args, **kwargs)

    owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
    receive = mock.Mock(side_effect=RuntimeError('injected handshake failure')) if failure == 'handshake' else mock.Mock()
    try:
        with mock.patch.object(sr, 'memory_headroom_bytes', return_value=4*GIB), \
                mock.patch.object(sr.shared_memory, 'SharedMemory', side_effect=allocate), \
                mock.patch.object(sr.np, 'ndarray', side_effect=array), \
                mock.patch.object(sr.multiprocessing, 'get_context', return_value=context), \
                mock.patch.object(sr.ProcessSuperResolution, '_receive', receive):
            with pytest.raises((MemoryError, OSError, RuntimeError)) as retained:
                owner.__init__(8, 8, 2)
        assert retained.value.__traceback__ is not None
        assert owner._closed
        assert all(getattr(owner, name) is None for name in (
            '_input_memory', '_output_memory', '_input_frame', '_output_frame',
            '_connection', '_child_connection', '_process'))
        for segment in segments:
            segment.close.assert_called_once_with()
            segment.unlink.assert_called_once_with()
            assert segment._mmap is None
        if failure in ('process', 'start', 'handshake'):
            parent.close.assert_called_once_with()
            child.close.assert_called_once_with()
        if failure in ('start', 'handshake'):
            process.close.assert_called_once_with()
        owner.close()
        assert all(segment.close.call_count == 1 for segment in segments)
    finally:
        for segment in segments:
            segment.close()
            segment.unlink()


def test_vsr_validation_failure_can_be_closed_without_allocations():
    owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
    with pytest.raises(sr.SuperResolutionError):
        owner.__init__(0, 8, 2)
    owner.close()
    owner.close()


@pytest.mark.parametrize('failure', ['output_memory', 'process', 'start'])
def test_nr_partial_creation_releases_buffers_and_pipe_before_process_start(failure):
    from dlss5tool import dlss_host_process as host
    real_memory = shared_memory.SharedMemory
    segments = []
    parent, child, process, context = mock.Mock(), mock.Mock(), mock.Mock(pid=None), mock.Mock()
    process.is_alive.return_value = False
    process.start.side_effect = OSError('injected host start failure')
    context.Pipe.return_value = parent, child
    context.Process.return_value = process
    if failure == 'process':
        context.Process.side_effect = OSError('injected host process failure')
    def allocate(**kwargs):
        if failure == 'output_memory' and segments:
            raise MemoryError('injected host output allocation failure')
        segment = real_memory(**kwargs)
        segment.close = mock.Mock(wraps=segment.close)
        segment.unlink = mock.Mock(wraps=segment.unlink)
        segments.append(segment)
        return segment
    try:
        with mock.patch.object(host, '_open_shared_memory', side_effect=allocate), \
                mock.patch.object(host, 'require_memory_headroom'), \
                mock.patch.object(host.multiprocessing, 'get_context', return_value=context):
            with pytest.raises((OSError, MemoryError)) as retained:
                host._HostSession(8, 8, {}, live_factory=mock.Mock())
        assert retained.value.__traceback__ is not None
        for segment in segments:
            segment.close.assert_called_once_with()
            segment.unlink.assert_called_once_with()
            assert segment._mmap is None
        process.join.assert_not_called()
        if failure in ('process', 'start'):
            parent.close.assert_called_once_with()
            child.close.assert_called_once_with()
    finally:
        for segment in segments:
            segment.close()
            segment.unlink()


@pytest.mark.parametrize('unkillable', [False, True])
def test_vsr_broken_pipe_reaps_process_and_keeps_live_buffers(unkillable):
    owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
    process, connection, segment = mock.Mock(pid=12345), mock.Mock(), mock.Mock()
    state = dict(alive=True)
    process.is_alive.side_effect = lambda: state['alive']
    process.kill.side_effect = lambda: state.update(alive=unkillable)
    connection.send.side_effect = BrokenPipeError('broken close pipe')
    owner._closed, owner._process, owner._connection = False, process, connection
    owner._child_connection = None
    owner._input_memory, owner._output_memory = segment, None
    owner._input_frame, owner._output_frame, owner.log_path = object(), object(), None
    if unkillable:
        with pytest.raises(sr.SuperResolutionError, match='尚未退出'):
            owner.close()
        assert not owner._closed
        segment.close.assert_not_called()
        state['alive'] = False
    owner.close()
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()
    process.close.assert_called_once_with()
    segment.close.assert_called_once_with()
    segment.unlink.assert_called_once_with()
    assert owner._input_frame is owner._output_frame is None


def fake_vsr_worker(connection, width, height, scale, is_hdr, input_name, output_name, log_path):
    """Real spawn/Pipe/shared-memory protocol; no native DLL or GPU."""
    memories = [shared_memory.SharedMemory(name=name) for name in (input_name, output_name)]
    frames = None
    try:
        dtype = np.float16 if is_hdr else np.uint8
        frames = [np.ndarray((height, width, 4), dtype, buffer=memories[0].buf),
                  np.ndarray((height*scale, width*scale, 4), dtype, buffer=memories[1].buf)]
        connection.send(dict(ok=True, operation='initialize'))
        while True:
            operation = connection.recv()['operation']
            if operation == 'process':
                frames[1][:] = np.repeat(np.repeat(frames[0], scale, axis=0), scale, axis=1)
            connection.send(dict(ok=True, operation=operation))
            if operation == 'close':
                break
    finally:
        frames = None
        for memory in memories:
            memory.close()
        connection.close()


@pytest.mark.parametrize('hdr', [False, True])
def test_vsr_real_spawn_and_shared_memory_reaped_across_repeated_tasks(hdr):
    context = multiprocessing.get_context('spawn')
    def process(**kwargs):
        if kwargs.get('target') is sr.worker_with_memory_pool:
            kwargs['args']=kwargs['args'][2]
        return context.Process(**{**kwargs, 'target': fake_vsr_worker})
    replacement = SimpleNamespace(Pipe=context.Pipe, Process=process)
    with mock.patch.object(sr.multiprocessing, 'get_context', return_value=replacement), \
            mock.patch.object(sr, 'memory_headroom_bytes', return_value=4*GIB):
        for size in (8, 12, 8):
            owner = sr.ProcessSuperResolution(size, size, 2, is_hdr=hdr)
            pixels = np.ones((size, size, 4), np.float16 if hdr else np.uint8)
            result = owner.process(pixels)
            np.testing.assert_array_equal(result, np.ones((size*2, size*2, 4), pixels.dtype))
            process_owner, exits = owner._process, []
            real_close = process_owner.close
            def close():
                assert not process_owner.is_alive()
                exits.append(process_owner.exitcode)
                real_close()
            with mock.patch.object(process_owner, 'close', side_effect=close):
                owner.close()
            assert exits == [0]
            assert owner._process is owner._input_memory is owner._output_memory is None
            owner.close()


def failure_with_frames(references):
    pixels = np.ones((64, 64, 4), np.float32)
    references.append(weakref.ref(pixels))
    try:
        cause = ValueError('inner error')
        cause.pixels = pixels
        raise cause
    except ValueError as cause:
        error = RuntimeError('outer frame failure')
        error.pixels = pixels
        raise error from cause


def test_error_snapshot_retains_diagnostic_text_without_frames_or_causes():
    references = []
    try:
        failure_with_frames(references)
    except RuntimeError as error:
        snapshot = ErrorSnapshot(error)
    gc.collect()
    assert references[0]() is None
    first, second = snapshot.exception(), snapshot.exception()
    assert first is not second
    assert str(first) == 'outer frame failure'
    assert str(snapshot) == 'outer frame failure'
    assert first.__cause__ is first.__context__ is first.__traceback__ is None
    assert 'inner error' in first.diagnostic_traceback
    assert 'failure_with_frames' in first.diagnostic_traceback


def test_render_failure_releases_worker_and_repeated_consumer_frames():
    references = []
    def renderer(*args, **kwargs):
        failure_with_frames(references)
    session = RenderSession('synthetic', {}, 1024**2, renderer=renderer)
    session._produce()
    gc.collect()
    assert references[0]() is None
    assert str(session.error) == 'outer frame failure'
    assert session.error.__traceback__ is None
    def consume():
        pixels = np.ones((64, 64, 4), np.float32)
        references.append(weakref.ref(pixels))
        try:
            raise session.error
        except RuntimeError:
            pass
    for _ in range(8):
        consume()
    gc.collect()
    assert all(reference() is None for reference in references)
    session.close()
    assert session.error is None


def until(predicate):
    deadline = time.monotonic()+4
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.002)


def test_persistent_preview_errors_do_not_keep_frames_across_repeated_requests():
    references = []
    job = LatestPreviewJob(persistent=True, idle_seconds=.02)
    try:
        for i in range(12):
            job.request(i, lambda: failure_with_frames(references))
            until(job.ready)
            for _ in range(3):
                try:
                    job.request(i, None)
                except RuntimeError as error:
                    assert str(error) == 'outer frame failure'
            gc.collect()
            assert all(reference() is None for reference in references)
    finally:
        job.close()
        until(lambda: not job.is_alive())


def test_shared_preview_setup_releases_error_frames_and_consumed_mailbox():
    from dlss5tool.gui import App
    references = []
    app = App.__new__(App)
    app.video, app._frame, app._shared_revision = 'synthetic', 0, 1
    app._shared_config = lambda: {}
    app._freeze_preview_cache = app._cache_clear = mock.Mock()
    app._close_live = app._close_super_resolution = mock.Mock()
    manager = mock.Mock()
    manager.session.side_effect = lambda *args, **kwargs: failure_with_frames(references)
    app._shared_manager = lambda: manager
    assert app._shared_ready() is None
    app._shared_setup_thread.join(timeout=4)
    assert not app._shared_setup_thread.is_alive()
    gc.collect()
    assert references[0]() is None
    with pytest.raises(RuntimeError, match='outer frame failure'):
        app._shared_ready()
    assert app._shared_setup_result is None
    with pytest.raises(RuntimeError, match='outer frame failure'):
        app._shared_ready()


@pytest.mark.parametrize('width,height,scale', [(1920, 1080, 1), (1920, 1080, 2), (3840, 2160, 2)])
def test_zero_frame_capacity_is_preserved_at_all_sizes(width, height, scale):
    with mock.patch.object(sr, 'bounded_frame_count', return_value=0):
        assert sr.select_in_flight(width, height, scale, 16, is_hdr=True) == 0


@pytest.mark.parametrize('free,commit', [(0, 20*GIB), (20*GIB, 0), (4*GIB, 20*GIB)])
def test_admission_obeys_physical_and_commit_reserve(free, commit):
    memory = dict(total_physical_bytes=32*GIB, free_physical_bytes=free, free_commit_bytes=commit)
    with mock.patch.object(mb, 'system_memory', return_value=memory):
        with pytest.raises(MemoryError):
            mb.require_memory_headroom(1)


def test_unknown_snapshot_preserves_existing_admission_policy():
    with mock.patch.object(mb, 'system_memory', return_value=None):
        mb.require_memory_headroom(10*GIB)


@pytest.mark.parametrize('frame_format', ['rgba8', 'rgba16f'])
def test_nr_rejects_pressure_before_shared_memory_and_process_creation(frame_format):
    with mock.patch.object(mb, 'system_memory', return_value=NO_MEMORY), \
            mock.patch('dlss5tool.dlss_host_process._open_shared_memory') as allocate, \
            mock.patch('dlss5tool.dlss_host_process.multiprocessing.get_context') as context:
        with pytest.raises(MemoryError):
            _HostSession(1920, 1080, dict(frame_format=frame_format), live_factory=mock.Mock())
    allocate.assert_not_called()
    context.assert_not_called()


def test_memory_failure_does_not_retry_another_backend():
    owner = ProcessLive.__new__(ProcessLive)
    owner._live_factory = None
    with mock.patch.object(ProcessLive, '_auto_candidates', return_value=['v2', 'legacy']), \
            mock.patch('dlss5tool.dlss_host_process._HostSession', side_effect=MemoryError('RAM')) as start:
        with pytest.raises(MemoryError, match='RAM'):
            owner._start_session(8, 8, dict(host_backend='auto'))
    assert start.call_count == 1


@pytest.mark.parametrize('mode,scale', [('hdr', 1), ('hdr', 2), ('sdr', 2)])
def test_gui_export_rejects_before_reader_writer_or_host_creation(mode, scale):
    from dlss5tool import gui
    app = gui.App.__new__(gui.App)
    function = gui.App._export_hdr_video if mode == 'hdr' else gui.App._export_sdr_upscaled_video
    with mock.patch.object(mb, 'system_memory', return_value=NO_MEMORY), \
            mock.patch.object(gui, 'FFmpegHDRVideoReader') as reader, \
            mock.patch.object(gui, 'FFmpegVideoWriter') as writer, \
            mock.patch.object(gui, 'ProcessLive') as nr, \
            mock.patch.object(gui, 'ProcessSuperResolution') as vsr:
        with pytest.raises(MemoryError):
            function(app, 'source', dict(is_hdr=True, profile='hdr10_pq'), 'output',
                8, 24, 1920, 1080, {}, dict(super_resolution_scale=scale), 0, 1)
    for constructor in (reader, writer, nr, vsr):
        constructor.assert_not_called()


def test_gui_hdr_does_not_turn_denied_queue_into_one_slot():
    from dlss5tool import gui
    with mock.patch.object(gui, 'require_memory_headroom'), \
            mock.patch.object(gui, 'select_in_flight', return_value=0), \
            mock.patch.object(gui, 'query_gpu_memory', return_value=None), \
            mock.patch.object(gui, 'FFmpegHDRVideoReader') as reader:
        with pytest.raises(MemoryError):
            gui.App._export_hdr_video(gui.App.__new__(gui.App), 'source',
                dict(is_hdr=True, profile='hdr10_pq'), 'output', 8, 24, 1920, 1080, {}, {}, 0, 1)
    reader.assert_not_called()


@pytest.mark.parametrize('multiplier', [1, 2])
def test_generic_export_rejects_before_gpu_workers_or_decode(tmp_path, multiplier):
    from dlss5tool import frame_generation as fg
    source = tmp_path/'input.mp4'
    source.write_bytes(b'synthetic input; inspector is mocked')
    metadata = dict(is_hdr=False, profile='srgb', color_primaries='bt709')
    inspector = lambda *args, **kwargs: (metadata, Fraction(24), 128, 128, 8)
    with mock.patch.object(mb, 'system_memory', return_value=NO_MEMORY), \
            mock.patch.object(sr, 'query_gpu_memory', return_value=None), \
            mock.patch.object(fg, 'NativeStream') as native, \
            mock.patch.object(fg, 'open_capture') as capture:
        with pytest.raises(RuntimeError):
            fg.export_video(source, tmp_path/'output.mp4', multiplier=multiplier, enhance=True,
                log_dir=tmp_path/'logs', source_inspector=inspector)
    native.assert_not_called()
    capture.assert_not_called()
    assert not (tmp_path/'output.mp4').exists()


def test_hdr_reader_rejects_before_start_and_each_frame():
    from dlss5tool import video_export as video
    with mock.patch.object(mb, 'system_memory', return_value=NO_MEMORY), \
            mock.patch.object(video.subprocess, 'Popen') as start:
        with pytest.raises(MemoryError):
            video.FFmpegHDRVideoReader('source', 8, 8, dict(color_transfer='smpte2084'))
        reader = video.FFmpegHDRVideoReader.__new__(video.FFmpegHDRVideoReader)
        reader.width = reader.height = 8
        reader._sequence_reader = SimpleNamespace(closed=False, position=0,
            sequence=SimpleNamespace(files=['unread']), read=mock.Mock())
        with pytest.raises(MemoryError):
            reader.read()
    start.assert_not_called()
    reader._sequence_reader.read.assert_not_called()


def test_gpu_encoder_rejects_before_temp_or_shared_allocation(tmp_path):
    from dlss5tool import gpu_export_process as gpu
    with mock.patch.object(mb, 'system_memory', return_value=NO_MEMORY), \
            mock.patch.object(gpu.shared_memory, 'SharedMemory') as allocate, \
            mock.patch.object(gpu.tempfile, 'mkdtemp') as transaction, \
            mock.patch.object(gpu.multiprocessing, 'get_context') as context:
        with pytest.raises(MemoryError):
            gpu.ProcessGpuVideoWriter(str(tmp_path/'output.mp4'), 128, 128, 24)
    allocate.assert_not_called()
    transaction.assert_not_called()
    context.assert_not_called()
