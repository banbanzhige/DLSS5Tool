"""Parallel workers cannot start native work before their lifetime is armed."""
from fractions import Fraction
import os
from pathlib import Path
import sys
import threading
from unittest import mock

import pytest

from dlss5tool import parallel_export as parent, parallel_export_worker as child
from dlss5tool.video_timeline import VideoTimeline


@pytest.fixture
def controller(monkeypatch):
    monkeypatch.setattr(parent, '_pending_retirements', [])
    cap = mock.Mock()
    cap.get.side_effect = [4, 128, 128, 24]
    process = mock.Mock()
    process.poll.return_value = process.wait.return_value = 0
    launch = mock.Mock(return_value=process)
    monkeypatch.setattr(parent, 'open_capture', lambda *a, **k: cap)
    monkeypatch.setattr(parent, 'inspect_video_timeline', lambda *a, **k: VideoTimeline(
        tuple(str(Fraction(i, 24)) for i in range(4)), Fraction(1, 6),
        Fraction(0), Fraction(24), False))
    monkeypatch.setattr(parent, 'find_ffmpeg', lambda: 'fixture')
    monkeypatch.setattr(parent, 'select_video_encoder', lambda *a, **k: ('h264', False))
    monkeypatch.setattr(parent.subprocess, 'Popen', launch)
    monkeypatch.setattr(parent, 'concat_video_segments', lambda *a: None)
    monkeypatch.setattr(parent, 'mux_source_audio', lambda *a, **k: 'none')
    return launch


@pytest.mark.parametrize('fail_assignment', [False, True])
def test_parent_publishes_start_only_after_job_assignment(controller, tmp_path,
                                                         monkeypatch, fail_assignment):
    lifetimes, gates = [], []

    def arm(process, *, nvidia_compat=False):
        assert nvidia_compat is True
        command = controller.call_args.args[0]
        gate = Path(command[command.index('--start-gate') + 1])
        assert not gate.exists()
        gates.append(gate)
        if fail_assignment and len(gates) == 2:
            raise OSError('fixture job assignment denied')
        lifetime = mock.Mock()
        lifetimes.append(lifetime)
        return lifetime

    def result(_):
        assert all(gate.read_text(encoding='ascii') == 'start' for gate in gates)
        return {'ok': True, 'kept_frames': 2}

    monkeypatch.setattr(parent, 'create_process_job', arm)
    monkeypatch.setattr(parent, '_read_json', result)
    if fail_assignment:
        with pytest.raises(OSError, match='fixture job assignment denied'):
            parent.export_parallel('fixture.mp4', str(tmp_path/'output.mp4'), {})
    else:
        assert parent.export_parallel('fixture.mp4', str(tmp_path/'output.mp4'), {})['frames'] == 4
    for lifetime in lifetimes:
        lifetime.close.assert_called_once_with()
    assert len(gates) == 2


def test_child_waits_for_parent_release_and_times_out_without_it(tmp_path):
    gate = tmp_path/'start'
    started = threading.Event()
    thread = threading.Thread(target=lambda: (child._wait_start_gate(str(gate), 3), started.set()))
    thread.start()
    try:
        assert not started.wait(.05)
        gate.write_text('start', encoding='ascii')
        thread.join(timeout=3)
        assert started.is_set()
    finally:
        thread.join(timeout=3)
    with pytest.raises(RuntimeError, match='启动许可'):
        child._wait_start_gate(str(tmp_path/'never-created'), .01)


def test_denied_start_never_decodes_or_initializes_native_host(tmp_path, monkeypatch):
    receipts = []
    monkeypatch.setattr(sys, 'argv', ['worker', '--input', 'fixture.mp4', '--output', 'unused.mp4',
        '--start', '0', '--end', '1', '--progress', str(tmp_path/'progress'),
        '--result', str(tmp_path/'result'), '--worker-id', '0', '--start-gate', 'denied'])
    monkeypatch.setattr(child, '_wait_start_gate', mock.Mock(side_effect=RuntimeError('denied')))
    monkeypatch.setattr(child, '_write_json', lambda _, payload, **kw: receipts.append(payload))
    native, capture = mock.Mock(), mock.Mock()
    monkeypatch.setattr(child.dlss_engine, 'Live', native)
    monkeypatch.setattr(child, 'open_capture', capture)
    assert child.main() == 1
    native.assert_not_called()
    capture.assert_not_called()
    assert not receipts[-1]['ok']


@pytest.mark.skipif(os.name != 'nt', reason='Windows venv redirector')
def test_source_launch_bypasses_venv_redirector():
    command, environment = parent._worker_launch()
    assert command[0] == sys._base_executable
    if sys.prefix != sys.base_prefix:
        assert environment['__PYVENV_LAUNCHER__'] == sys.executable


def test_failed_retirement_retains_handles_and_blocks_another_export(monkeypatch):
    process, lifetime, capture = mock.Mock(), mock.Mock(), mock.Mock()
    process.poll.return_value = 0
    lifetime.close.side_effect = OSError('fixture retirement denied')
    pending, errors = parent._retire_process_trees([(process, lifetime)])
    assert pending == [(process, lifetime)] and errors
    monkeypatch.setattr(parent, '_pending_retirements', pending)
    monkeypatch.setattr(parent, 'open_capture', capture)
    with pytest.raises(RuntimeError, match='前次并行导出进程树尚未回收'):
        parent.export_parallel('unused', 'unused', {})
    capture.assert_not_called()
    assert parent._pending_retirements == [(process, lifetime)]
    lifetime.close.side_effect = None
    parent._retry_pending_retirements()
    assert not parent._pending_retirements
