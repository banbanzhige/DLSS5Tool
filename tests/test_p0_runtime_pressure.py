"""Runtime/idle pressure must stop work without losing the resource owner."""
import time
from unittest import mock

import numpy as np
import pytest

from dlss5tool import memory_budget as mb, super_resolution as sr
from dlss5tool.dlss_host_process import ProcessLive, _HostSession, HostProcessError
from tests.test_dlss_host_process import FakeLive

GIB = 1024**3
PRESSURE = dict(total_physical_bytes=32*GIB, free_physical_bytes=4*GIB,
                free_commit_bytes=20*GIB)


class PressureLive(FakeLive):
    def process(self, *args, **kwargs):
        result = super().process(*args, **kwargs)
        # Only this disposable child sees pressure; never consume real RAM.
        mb.system_memory = lambda: dict(PRESSURE)
        return result


@pytest.mark.parametrize('idle', [False, True])
def test_worker_pressure_rejects_active_and_idle_work(idle):
    connection = mock.Mock()
    connection.poll.return_value = not idle
    connection.recv.return_value = {'operation': 'process'}
    with mock.patch.object(mb, 'system_memory', return_value=PRESSURE):
        with pytest.raises(MemoryError):
            mb.receive_worker_command(connection, 256)
    if idle:
        connection.recv.assert_not_called()


@pytest.mark.parametrize('operation', ['stop', 'close'])
def test_stop_commands_are_accepted_under_pressure(operation):
    connection = mock.Mock()
    connection.recv.return_value = {'operation': operation}
    with mock.patch.object(mb, 'system_memory', return_value=PRESSURE):
        assert mb.receive_worker_command(connection, 256)['operation'] == operation


def test_unknown_memory_status_keeps_worker_operable():
    connection = mock.Mock()
    connection.poll.side_effect = [False, True]
    connection.recv.return_value = {'operation': 'process'}
    with mock.patch.object(mb, 'system_memory', return_value=None):
        assert mb.receive_worker_command(connection, 256) == {'operation': 'process'}


@pytest.mark.parametrize('kind', ['sr', 'nr'])
def test_pressure_rejects_parent_copy_and_send(kind):
    pixels = np.zeros((4, 4, 4), np.uint8)
    if kind == 'sr':
        owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
        owner._closed, owner.width, owner.height, owner.dtype = False, 4, 4, np.uint8
        owner._output_frame = pixels
        operation = owner.process
    else:
        owner = _HostSession.__new__(_HostSession)
        owner.dtype = np.uint8
        operation = owner.copy_input
    owner._input_frame = np.full_like(pixels, 255)
    owner._connection = mock.Mock()
    with mock.patch.object(mb, 'system_memory', return_value=PRESSURE):
        with pytest.raises(MemoryError):
            operation(pixels)
    assert np.all(owner._input_frame == 255)
    owner._connection.send.assert_not_called()


def test_real_spawned_idle_host_retires_after_simulated_pressure():
    owner = ProcessLive(8, 8, {'host_backend': 'v2'}, _live_factory=PressureLive)
    session = owner._session
    try:
        assert owner.process(np.zeros((8, 8, 4), np.uint8)) is not None
        deadline = time.monotonic()+8
        while session._process.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not session._process.is_alive()
        with pytest.raises(HostProcessError, match='MemoryPressureError'):
            session.request('pending')
    finally:
        owner.close()
    assert owner._session is None
    assert session._input_memory is session._output_memory is None


def test_dead_vsr_reports_idle_pressure_without_copy_or_send():
    owner = sr.ProcessSuperResolution.__new__(sr.ProcessSuperResolution)
    owner._closed, owner.width, owner.height, owner.dtype = False, 4, 4, np.uint8
    owner._process = mock.Mock()
    owner._process.is_alive.return_value = False
    owner._connection = mock.Mock()
    owner._connection.recv.return_value = {'ok': False, 'error': 'MemoryError: reserve exhausted'}
    owner._log_tail = lambda: ''
    owner.timeout = 60
    with pytest.raises(sr.SuperResolutionError, match='reserve exhausted'):
        owner.process(np.zeros((4, 4, 4), np.uint8))
    owner._connection.poll.assert_called_once_with(0)
    owner._connection.send.assert_not_called()


def test_vsr_pressure_does_not_enter_potentially_blocking_ngx_shutdown():
    from multiprocessing import shared_memory
    source = shared_memory.SharedMemory(create=True, size=8*8*4)
    target = shared_memory.SharedMemory(create=True, size=16*16*4)
    connection, library = mock.Mock(), mock.Mock()
    try:
        with mock.patch.object(sr, '_load_host', return_value=library), \
                mock.patch.object(sr, 'receive_worker_command', side_effect=MemoryError('pressure')):
            sr._worker_main(connection, 8, 8, 2, False, source.name, target.name, '')
        library.vsr_init.assert_called_once()
        library.vsr_shutdown.assert_not_called()
        assert connection.send.call_args_list[-1].args[0]['ok'] is False
        connection.close.assert_called_once_with()
    finally:
        source.close()
        target.close()
        source.unlink()
        target.unlink()
