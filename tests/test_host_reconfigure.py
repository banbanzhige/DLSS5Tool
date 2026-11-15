"""Resolution handoff and unified native configuration, without GPU dependencies."""
from multiprocessing import shared_memory
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from dlss5tool import dlss_engine as engine
from dlss5tool.host_reconfigure import FramePair, worker_candidate, reconfigure_session
from dlss5tool.dlss_host_process import ProcessLive
from tests.test_dlss_host_process import FakeLive


class ReconfigurableLive(FakeLive):
    def reconfigure(self, width, height, settings, **kwargs):
        if settings.get('fixture_native_failure'):
            raise RuntimeError('native feature was already released')
        self.width, self.height = width, height
        self._guidance_previous = None
        self.update(settings)


class ProtocolProxy(ProcessLive):
    """Exercise orchestration before enabling it in the public proxy."""
    def update(self, settings):
        from dlss5tool.host_reconfigure import reconfigure_proxy
        from dlss5tool.guidance_client import normalize_public_settings
        reconfigure_proxy(self, self._w, self._h, normalize_public_settings({**self.settings, **settings}))

    def resize(self, width, height, preset=None, settings=None):
        from dlss5tool.host_reconfigure import reconfigure_proxy
        from dlss5tool.guidance_client import normalize_public_settings
        updated = {**self.settings, **(settings or {})}
        if preset is not None:
            updated['preset'] = preset
        reconfigure_proxy(self, width, height, normalize_public_settings(updated), force=True)

    def dequeue(self):
        from dlss5tool.host_reconfigure import retained_outputs
        outputs = retained_outputs(self)
        return outputs.popleft() if outputs else super().dequeue()

    @property
    def pending(self):
        from dlss5tool.host_reconfigure import retained_outputs
        return len(retained_outputs(self))+super().pending


def make_live(**settings):
    return ProcessLive(8, 6, dict(host_backend='v2', **settings), _live_factory=ReconfigurableLive)


def test_process_proxy_reuses_worker_buffers_and_combines_configuration():
    live = make_live()
    try:
        session = live._session
        pid = session._process.pid
        names = session._input_memory.name, session._output_memory.name
        live.resize(7, 5)
        assert live._session is session and session._process.pid == pid
        assert (session._input_memory.name, session._output_memory.name) == names
        np.testing.assert_array_equal(live.process(np.full((5, 7, 4), 3, np.uint8)), 5)
        live.resize(13, 9, settings={'frame_format': 'rgba16f', 'color_profile': 'hdr10_hlg', 'host_tiled_mode': True})
        assert live._session is session and session._process.pid == pid and live.tiled
        assert session._input_memory.name != names[0]
        np.testing.assert_allclose(live.process(np.full((9, 13, 4), .25, np.float16)), .27, atol=.001)
        live.update({'frame_format': 'rgba8', 'color_profile': 'srgb', 'host_tiled_mode': False, 'guidance_mode': 1})
        assert live._session is session
        np.testing.assert_array_equal(live.process(np.zeros((9, 13, 4), np.uint8)), 2)
    finally:
        live.close()


def test_inflight_outputs_survive_size_and_async_mode_change_in_order():
    live = make_live()
    try:
        assert live.enqueue(np.full((6, 8, 4), 4, np.uint8))
        assert live.enqueue(np.full((6, 8, 4), 9, np.uint8))
        session = live._session
        live.resize(11, 7, settings={'host_tiled_mode': True})
        assert live._session is session and not live.supports_async
        assert live.pending == 2
        np.testing.assert_array_equal(live.dequeue(), np.full((6, 8, 4), 6, np.uint8))
        np.testing.assert_array_equal(live.dequeue(), np.full((6, 8, 4), 11, np.uint8))
        assert live.pending == 0
        assert live.process(np.zeros((7, 11, 4), np.uint8)).shape == (7, 11, 4)
    finally:
        live.close()


def test_feature_failure_recovers_after_retiring_original_worker():
    live = make_live()
    try:
        original = live._session
        live.resize(11, 7, settings={'fixture_native_failure': True})
        assert original._closed and original._process is None
        assert live._session is not original
        np.testing.assert_array_equal(live.process(np.zeros((7, 11, 4), np.uint8)), 2)
    finally:
        live.close()


def test_capacity_reuse_keeps_names_and_changes_only_views():
    owner = FramePair(16, 10, np.uint8)
    small = peer = None
    try:
        names = owner.input_memory.name, owner.output_memory.name
        small = FramePair(15, 9, np.uint8, borrow=(owner.input_memory, owner.output_memory))
        peer = FramePair(15, 9, np.uint8, names=names)
        small.input_frame.fill(17)
        np.testing.assert_array_equal(peer.input_frame, 17)
        peer.output_frame.fill(23)
        np.testing.assert_array_equal(small.output_frame, 23)
        small.close()
        assert owner.input_memory.name == names[0]
        assert owner.input_frame.shape == (10, 16, 4)
    finally:
        if peer: peer.close()
        if small: small.close()
        owner.close()
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


@pytest.mark.parametrize('failure', ['second_memory', 'first_view', 'second_view'])
def test_partial_candidate_creation_releases_all_owned_buffers(failure):
    import dlss5tool.host_reconfigure as module
    actual_memory, actual_array = shared_memory.SharedMemory, np.ndarray
    segments, counts = [], {'views': 0}
    def allocate(**kwargs):
        if len(segments) == 1 and failure == 'second_memory':
            raise MemoryError('candidate allocation')
        memory = actual_memory(**kwargs)
        segments.append(memory)
        return memory
    def view(*args, **kwargs):
        counts['views'] += 1
        if counts['views'] == (1 if failure == 'first_view' else 2):
            raise MemoryError('candidate view')
        return actual_array(*args, **kwargs)
    with mock.patch.object(module.shared_memory, 'SharedMemory', side_effect=allocate), \
            mock.patch.object(module.np, 'ndarray', side_effect=view), pytest.raises(MemoryError):
        FramePair(8, 6, np.uint8)
    assert segments
    for segment in segments:
        assert segment._mmap is None
        with pytest.raises(FileNotFoundError):
            actual_memory(name=segment.name)


def test_worker_preparation_rejects_pending_and_stale_generation_without_mutation():
    live = SimpleNamespace(pending=1, reconfigure=mock.Mock())
    message = dict(width=8, height=6, generation=2, settings={})
    with pytest.raises(ValueError, match='generation'):
        worker_candidate(live, message, 0)
    message['generation'] = 1
    with pytest.raises(ValueError, match='Drain'):
        worker_candidate(live, message, 0)
    live.reconfigure.assert_not_called()


def test_worker_candidate_maps_both_views_before_native_mutation():
    owner = FramePair(12, 8, np.float16)
    live = SimpleNamespace(pending=0, reconfigure=mock.Mock())
    peer = None
    try:
        message = dict(width=10, height=7, generation=1,
            settings={'frame_format': 'rgba16f', 'color_profile': 'hdr10_hlg'},
            input_name=owner.input_memory.name, output_name=owner.output_memory.name)
        peer = worker_candidate(live, message, 0)
        assert peer.input_frame.shape == (7, 10, 4) and peer.output_frame.dtype == np.float16
        live.reconfigure.assert_called_once()
        peer.close()
        # Peer close must never unlink the parent's buffers.
        check = shared_memory.SharedMemory(name=owner.input_memory.name)
        check.close()
    finally:
        if peer: peer.close()
        owner.close()


def native_fixture():
    live = engine.Live.__new__(engine.Live)
    live._w, live._h = 8, 6
    live.settings = {'host_backend': 'v2', 'preset': 1, 'guidance_mode': 0}
    live._config = engine._host_config(live.settings)
    live.backend, live._preference = 'v2', 'v2'
    live.runtime_path = 'fixture.dll'
    live.adapter_info = {'id': 'fixture-gpu'}
    live._guidance = live._gpu_flow = None
    live._lib = SimpleNamespace(dlssnr_pending=lambda: 0, dlssnr_resize=mock.Mock(return_value=1))
    live._allocate_buffers = mock.Mock()
    live._refresh_capabilities = mock.Mock()
    return live


def test_combined_dimensions_format_preset_and_configuration_rebuild_only_once():
    live = native_fixture()
    with mock.patch.object(engine, '_configure_host'), mock.patch.object(engine, '_set_options'):
        live.reconfigure(15, 9, {'preset': 2, 'frame_format': 'rgba16f',
            'color_profile': 'hdr10_hlg', 'host_in_flight': 1, 'host_tiled_mode': True})
    live._lib.dlssnr_resize.assert_called_once_with(15, 9, 2)
    live._allocate_buffers.assert_called_once()
    assert (live._w, live._h) == (15, 9) and live._reset_next


def test_appearance_only_update_keeps_feature_and_buffers():
    live = native_fixture()
    with mock.patch.object(engine, '_configure_host'):
        live.update({'intensity': .7, 'style': 2})
    live._lib.dlssnr_resize.assert_not_called()
    live._allocate_buffers.assert_not_called()


def test_guidance_budget_update_keeps_native_core_and_only_retires_component():
    live = native_fixture()
    component = live._guidance = mock.Mock()
    live._lib.dlssnr_capabilities = lambda: 0
    with mock.patch.object(engine, '_configure_host'):
        live.update({'guidance_cache_mb': 512})
    component.close.assert_called_once()
    live._lib.dlssnr_resize.assert_not_called()


class ProtocolLive:
    pending = 0
    def reconfigure(self, width, height, settings, **kwargs):
        if settings.get('fixture_native_failure'):
            raise RuntimeError('native feature was already released')


def protocol_worker(connection):
    import os
    pair = None
    try:
        message = connection.recv()
        assert message['operation'] == 'initialize'
        pair = FramePair(message['width'], message['height'], np.uint8, names=message['names'])
        connection.send({'ok': True})
        generation = 0
        while True:
            message = connection.recv()
            if message['operation'] == 'stop':
                break
            if message['operation'] == 'process':
                np.copyto(pair.output_frame, pair.input_frame+1)
                connection.send({'ok': True})
                continue
            candidate = worker_candidate(ProtocolLive(), message, generation)
            pair.close()
            pair = candidate
            generation += 1
            if message['settings'].get('fixture_lost_ack'):
                os._exit(7)
            connection.send({'ok': True, 'generation': generation})
    except Exception as error:
        connection.send({'ok': False, 'error': str(error)})
    finally:
        if pair is not None:
            pair.close()
        connection.close()


class ProtocolOwner:
    def __init__(self):
        import multiprocessing
        from dlss5tool.process_job import create_process_job
        pair = FramePair(8, 6, np.uint8)
        self._input_memory, self._output_memory, self._input_frame, self._output_frame = pair.detach()
        self.width, self.height, self.dtype = 8, 6, np.uint8
        self.configuration_generation = 0
        self.closed = False
        self.close_observations = []
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=protocol_worker, args=(child,))
        self.process.start()
        self.job = create_process_job(self.process)
        child.close()
        self.request('initialize', width=8, height=6,
                     names=(self._input_memory.name, self._output_memory.name))

    def request(self, operation, **payload):
        self.connection.send(dict(operation=operation, **payload))
        if not self.connection.poll(5):
            raise TimeoutError('fixture reply')
        response = self.connection.recv()
        if not response['ok']:
            raise RuntimeError(response['error'])
        return response

    def close(self):
        if self.closed:
            return
        self.job.close()
        self.process.join(5)
        assert not self.process.is_alive()
        self.close_observations.append('process_retired')
        self.connection.close()
        self._input_frame = self._output_frame = None
        for memory in (self._input_memory, self._output_memory):
            memory.close(); memory.unlink()
        for pair in getattr(self, '_reconfigure_frames', []):
            pair.close()
        self._reconfigure_frames = []
        self.process.close()
        self.closed = True


def test_real_spawn_handoff_reuses_capacity_grows_and_changes_pixel_format():
    owner = ProtocolOwner()
    names = owner._input_memory.name, owner._output_memory.name
    try:
        for index, (width, height, layout) in enumerate([(7, 5, 'rgba8'), (13, 9, 'rgba16f'), (8, 6, 'rgba8')]):
            reconfigure_session(owner, width, height, {'frame_format': layout, 'color_profile': 'hdr10_hlg' if layout == 'rgba16f' else 'srgb'})
            owner._input_frame.fill(index+1)
            owner.request('process')
            np.testing.assert_array_equal(owner._output_frame, index+2)
            assert owner.configuration_generation == index+1
            if index == 0:
                assert (owner._input_memory.name, owner._output_memory.name) == names
            elif index == 1:
                assert owner._input_memory.name != names[0]
                with pytest.raises(FileNotFoundError):
                    shared_memory.SharedMemory(name=names[0])
        assert not owner._reconfigure_frames
    finally:
        owner.close()


@pytest.mark.parametrize('setting', ['fixture_native_failure', 'fixture_lost_ack'])
def test_dispatched_failure_retires_worker_before_releasing_candidate(setting):
    owner = ProtocolOwner()
    names = []
    try:
        with pytest.raises((RuntimeError, EOFError)):
            reconfigure_session(owner, 13, 9, {setting: True})
        assert owner._reconfigure_failed
        assert (owner.width, owner.height) == (8, 6)
        assert owner.closed
        assert owner.close_observations == ['process_retired']
        assert not owner._reconfigure_frames
    finally:
        owner.close()
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


def test_old_map_close_failure_keeps_committed_geometry_and_retains_cleanup_owner():
    owner = ProtocolOwner()
    previous = owner._input_memory, owner._output_memory
    try:
        with mock.patch.object(previous[0], 'close', side_effect=OSError('old map still exported')):
            reconfigure_session(owner, 13, 9, {})
        assert (owner.width, owner.height) == (13, 9)
        assert len(owner._reconfigure_frames) == 1
        owner._input_frame.fill(3)
        owner.request('process')
        np.testing.assert_array_equal(owner._output_frame, 4)
    finally:
        owner.close()
        for memory in previous:
            memory.close()
            memory.unlink()
