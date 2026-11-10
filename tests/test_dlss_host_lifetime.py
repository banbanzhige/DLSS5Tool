"""Exercise real Windows jobs, gated startup and orphan descendant retirement."""
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from dlss5tool import dlss_host_process as host
from dlss5tool import process_job
from dlss5tool import super_resolution as sr
from dlss5tool.process_job import private_commit
from tests.host_lifetime_fixture import DescendantLive, vsr_worker
from tests.test_dlss_host_process import FakeLive


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows job objects')


class OwnedProcesses:
    """Retain fixture identities for assertions and failure-only safe cleanup."""
    def __init__(self, pids):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('OpenProcess', [C.c_uint32, C.c_int, C.c_uint32], C.c_void_p),
            ('WaitForSingleObject', [C.c_void_p, C.c_uint32], C.c_uint32),
            ('TerminateProcess', [C.c_void_p, C.c_uint32], C.c_int),
            ('CloseHandle', [C.c_void_p], C.c_int),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.handles = []
        for pid in pids:
            handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
            if not handle:
                self.close()
                raise C.WinError(C.get_last_error())
            self.handles.append(handle)

    def assert_retired(self):
        for handle in self.handles:
            assert self.kernel.WaitForSingleObject(handle, 5000) == 0

    def close(self):
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 0) != 0:
                self.kernel.TerminateProcess(handle, 99)
                self.kernel.WaitForSingleObject(handle, 5000)
            self.kernel.CloseHandle(handle)
        self.handles = []


def make_live(**settings):
    return host.ProcessLive(8, 6, {'host_backend': 'v2', **settings},
                            _live_factory=DescendantLive)


def wait_record(path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            time.sleep(.02)
    raise AssertionError('Fixture did not start')


def test_stop_before_start_gate_never_maps_frames_or_initializes_ngx():
    connection = mock.Mock()
    connection.recv.return_value = {'operation': 'stop'}
    factory = mock.Mock()
    with mock.patch.object(host, '_open_shared_memory') as memory:
        host._host_worker_main(connection, 8, 6, {}, 'unused', 'unused',
                               'unused', factory)
    factory.assert_not_called()
    memory.assert_not_called()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize('failure', ['assignment', 'watcher'])
def test_lifetime_setup_failure_never_initializes_native_host(failure):
    owner = host._HostSession.__new__(host._HostSession)
    real_job = host._make_worker_job
    jobs = []

    def job(process):
        if failure == 'assignment':
            raise OSError('fixture job assignment denied')
        result = real_job(process)
        jobs.append(result)
        return result

    with mock.patch.object(host, '_make_worker_job', side_effect=job), \
            mock.patch.object(process_job.threading.Thread, 'start',
                              side_effect=RuntimeError('fixture watcher denied')):
        with pytest.raises((OSError, RuntimeError), match='fixture'):
            owner.__init__(8, 6, {'host_backend': 'v2'}, live_factory=FakeLive)
    assert owner._closed
    assert owner._process is owner._input_memory is owner._output_memory is None
    assert owner._job is None
    assert all(job._job._handle is None for job in jobs)


@pytest.mark.parametrize('hang_close', [False, True])
def test_close_retires_children_and_grandchildren_only(hang_close, monkeypatch):
    monkeypatch.setattr(host, '_STOP_TIMEOUT', .3)
    outsider = subprocess.Popen([sys.executable, '-B', '-c',
                                 'import time; time.sleep(120)'],
                                creationflags=subprocess.CREATE_NO_WINDOW)
    live = None
    processes = None
    try:
        live = make_live(fixture_hang_close=hang_close)
        session = live._session
        processes = OwnedProcesses(live.guidance_info['fixture_pids'])
        # Both real descendant processes have private committed allocations.
        for handle in processes.handles:
            assert private_commit(SimpleNamespace(_handle=handle)) >= 16*1024**2
        live.close()
        processes.assert_retired()
        assert outsider.poll() is None
        assert session._job is session._process is None
        live.close()
    finally:
        if live is not None:
            live.close()
        if processes is not None:
            processes.close()
        outsider.terminate()
        outsider.wait(timeout=5)


def test_worker_death_retires_descendants_without_another_api_call():
    live = make_live()
    processes = OwnedProcesses(live.guidance_info['fixture_pids'])
    try:
        live._session._process.terminate()
        processes.assert_retired()
        live._session._job._watcher.join(timeout=5)
        assert live._session._job._job._handle is None
    finally:
        live.close()
        processes.close()


@pytest.mark.parametrize('failure', ['initialize', 'timeout'])
def test_failed_initialization_retires_already_spawned_descendants(tmp_path, failure):
    record = tmp_path/'descendants.json'
    release = tmp_path/'release.txt'
    owner = host._HostSession.__new__(host._HostSession)
    original_receive = host._HostSession._receive
    tracked = []

    def receive(self, timeout, operation):
        tracked.append(OwnedProcesses(wait_record(record)))
        release.write_text('go', encoding='utf-8')
        return original_receive(self, .1 if failure == 'timeout' else timeout, operation)

    try:
        with mock.patch.object(host._HostSession, '_receive', receive):
            with pytest.raises(host.HostProcessError):
                owner.__init__(8, 6, dict(host_backend='v2', fixture_record=str(record),
                    fixture_release=str(release),
                    fixture_fail_init=failure == 'initialize',
                    fixture_hang_init=failure == 'timeout'), live_factory=DescendantLive)
        for processes in tracked:
            processes.assert_retired()
        assert owner._closed and owner._job is owner._process is None
    finally:
        owner.close()
        for processes in tracked:
            processes.close()


def test_repeated_replacement_retires_each_previous_tree():
    live = make_live()
    tracked = []
    try:
        for index in range(4):
            processes = OwnedProcesses(live.guidance_info['fixture_pids'])
            tracked.append(processes)
            live.resize(8 + index, 6)
            processes.assert_retired()
        processes = OwnedProcesses(live.guidance_info['fixture_pids'])
        tracked.append(processes)
        live.close()
        processes.assert_retired()
    finally:
        live.close()
        for processes in tracked:
            processes.close()


def test_failed_job_close_keeps_owner_and_frames_for_retry():
    session = host._HostSession.__new__(host._HostSession)
    session._closed = False
    session._process = mock.Mock(pid=123)
    session._process.is_alive.return_value = False
    session._job = mock.Mock()
    session._job.close.side_effect = OSError('fixture close failure')
    session._connection = mock.Mock()
    session._child_connection = None
    session._input_frame = session._output_frame = object()
    session._input_memory, session._output_memory = mock.Mock(), mock.Mock()
    session.log_path = 'not-created-lifetime-fixture.log'
    with pytest.raises(OSError, match='fixture close failure'):
        session.close()
    assert not session._closed
    assert session._input_frame is not None
    session._input_memory.close.assert_not_called()
    session._process.close.assert_not_called()
    session._job.close.side_effect = None
    session.close()
    assert session._closed and session._job is session._process is None


def test_abrupt_gui_owner_death_retires_worker_and_descendants():
    root = Path(__file__).resolve().parents[1]
    owner = subprocess.Popen(
        [sys.executable, '-B', '-m', 'tests.host_lifetime_fixture', '--owner'],
        cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    processes = None
    try:
        record = json.loads(owner.stdout.readline())
        processes = OwnedProcesses(record['pids'])
        owner.stdin.write('exit\n')
        owner.stdin.flush()
        assert owner.wait(timeout=10) == 88
        processes.assert_retired()
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=5)
        if processes is not None:
            processes.close()
        owner.stdin.close()
        owner.stdout.close()


@pytest.mark.parametrize('crash', [False, True])
def test_vsr_worker_uses_same_descendant_lifetime(monkeypatch, crash):
    monkeypatch.setattr(sr, '_worker_main', vsr_worker)
    live = sr.ProcessSuperResolution(8, 6, 2)
    processes = OwnedProcesses(live.adapter_info['fixture_pids'])
    try:
        if crash:
            live._process.terminate()
            processes.assert_retired()
        live.close()
        processes.assert_retired()
        assert live._job is live._process is None
    finally:
        live.close()
        processes.close()


def test_vsr_stop_at_start_gate_never_loads_ngx():
    connection = mock.Mock()
    connection.recv.return_value = {'operation': 'close'}
    with mock.patch.object(sr, '_load_host') as native, \
            mock.patch.object(sr.shared_memory, 'SharedMemory') as memory:
        sr._worker_main(connection, 8, 6, 2, False, 'unused', 'unused', '')
    native.assert_not_called()
    memory.assert_not_called()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize('crash', [False, True])
def test_popen_interpreter_tree_is_bound_without_venv_launcher_escape(crash):
    from dlss5tool.parallel_export import _worker_launch
    command, environment = _worker_launch()
    command[-1] = 'tests.host_lifetime_fixture'
    command.append('--controlled')
    process = subprocess.Popen(command, env=environment, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    lifetime = processes = None
    try:
        lifetime = process_job.create_process_job(process)
        process.stdin.write('start\n')
        process.stdin.flush()
        record = json.loads(process.stdout.readline())
        assert record['pids'][0] == process.pid
        processes = OwnedProcesses(record['pids'])
        if crash:
            process.terminate()
            processes.assert_retired()
        lifetime.close()
        processes.assert_retired()
        process.wait(timeout=5)
    finally:
        if lifetime is not None:
            lifetime.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if processes is not None:
            processes.close()
        process.stdin.close()
        process.stdout.close()
