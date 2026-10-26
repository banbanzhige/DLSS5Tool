"""Resident route eligibility and process/output failure boundaries, without GPU."""
from fractions import Fraction
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest import mock

import pytest

from dlss5tool import resident_sr as sr
from dlss5tool.frame_generation import Cancelled

SETTINGS = dict(super_resolution_scale=2, frame_generation_multiplier=1,
                nr_quality={'total':0}, hdr_mode=False, output_view=0)
ADAPTER = dict(cuda_index=0, luid_high=0, luid_low=123, vendor_id=4318, device_id=10115, id='gpu')
META = dict(is_hdr=False, pixel_format='yuv420p', codec_name='h264', color_space='bt709', color_range='tv')


@pytest.mark.parametrize('change', [dict(frame_generation_multiplier=2), dict(super_resolution_scale=1),
    dict(nr_quality={'total':1}), dict(hdr_mode=True), dict(output_view=1), dict(output_size=(3840,2160)),
    dict(nvenc_preset='p7'), dict(rate_control='bitrate'), dict(quality_profile='maximum'), dict(codec='hevc')])
def test_unsupported_settings_do_not_even_inspect_or_start_worker(change):
    manager = mock.Mock()
    with mock.patch('dlss5tool.resident_sr_runtime.load_components') as discover, mock.patch.object(sr, '_spawn') as spawn:
        assert sr.try_export(manager, 'source', 'output', SETTINGS|change,
                             cancel=threading.Event(), progress=mock.Mock()) is None
    discover.assert_not_called(); manager.inspect.assert_not_called(); spawn.assert_not_called()


@pytest.mark.parametrize('change', [dict(_is_vfr=True), dict(is_hdr=True), dict(pixel_format='yuv420p10le'),
    dict(codec_name='av1'), dict(color_space='bt2020nc'), dict(tags={'rotate':'90'}),
    dict(side_data_list=[{'rotation':180}]),dict(decode_backend='CPU 解码兜底（可能较慢）'),dict(field_order='tt')])
def test_unsupported_source_contract_keeps_existing_route(change):
    assert sr.make_plan((META|change, Fraction(30), 1920, 1080, 96), SETTINGS, ADAPTER) is None


def test_plan_preserves_rational_fps_and_rejects_missing_physical_identity():
    plan = sr.make_plan((META, Fraction(30000,1001), 1920,1080,96), SETTINGS, ADAPTER)
    assert plan['rate'] == '30000/1001' and plan['adapter']['luid_low'] == 123
    assert sr.make_plan((META, Fraction(30), 1920,1080,96), SETTINGS, ADAPTER|{'cuda_index':None}) is None


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    source, destination = tmp_path/'source.mp4', tmp_path/'out.mp4'
    source.write_bytes(b'source')
    plan = sr.make_plan((META, Fraction(30),64,48,8), SETTINGS, ADAPTER)
    state = dict(messages=[], retired=False)
    process = mock.Mock(pid=777)
    process.returncode = None
    process.poll.side_effect = lambda: process.returncode
    def wait(timeout):
        process.returncode = 0
        return 0
    process.wait.side_effect = wait
    process.kill.side_effect = lambda: setattr(process, 'returncode', -9)
    receiver, sender = mock.Mock(), mock.Mock()
    receiver.poll.return_value = True
    def recv(_):
        value = state['messages'].pop(0)
        return json.dumps(dict(version=1,nonce=state['nonce'],pid=process.pid,**value)).encode()
    receiver.recv_bytes.side_effect = recv
    def spawn(_, nonce, log):
        state['nonce'] = nonce
        state['staging'] = tmp_path/('.sr-'+nonce)/'video.mp4'
        state['staging'].write_bytes(b'verified-video')
        return process
    def retire(_):
        if not state['retired']:
            assert not destination.exists() or state.get('competing')
        state['retired'] = True
    job = mock.Mock()
    job.close.side_effect = lambda: setattr(process, 'returncode', -9) if process.returncode is None else None
    monkeypatch.setattr(sr, '_spawn', spawn)
    monkeypatch.setattr(sr.multiprocessing, 'Pipe', lambda **_: (receiver,sender))
    monkeypatch.setattr('dlss5tool.process_job.KillOnCloseJob', lambda _: job)
    monkeypatch.setattr('dlss5tool.process_job.confirm_retired', retire)
    monkeypatch.setattr('dlss5tool.process_job.private_commit', lambda _: 1024**3)
    monkeypatch.setattr('dlss5tool.memory_budget.current_pool', lambda **_: None)
    monkeypatch.setattr('dlss5tool.memory_budget.require_memory_headroom', lambda *_,**__: None)
    monkeypatch.setattr(sr.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100*1024**3))
    state.update(source=source, destination=destination, plan=plan, process=process,
        result=dict(status='complete',real_frames=8,output_rate='30',width=128,height=96))
    def export(**options):
        return sr.export_resident(source,destination,plan,{},cancel=state.setdefault('cancel',threading.Event()),
                                  log_root=tmp_path/'logs',**options)
    state['export'] = export
    return state


def test_publication_waits_for_native_retirement(transaction):
    t = transaction
    t['messages'] = [dict(kind='initialized',adapter_luid=123), dict(kind='complete',result=t['result'])]
    result = t['export']()
    assert t['retired'] and result['real_frames'] == 8
    assert t['destination'].read_bytes() == b'verified-video'
    assert not list(t['destination'].parent.glob('.sr-*'))


def test_cancel_retires_owner_and_cleans_only_its_transaction(transaction):
    t = transaction
    unrelated = t['source'].parent/'keep.mp4'; unrelated.write_bytes(b'user')
    t['messages'] = [dict(kind='progress',frames=1,total=8),dict(kind='complete',result=t['result'])]
    with pytest.raises(Cancelled):
        t['export'](frame_progress=lambda *_: t['cancel'].set())
    assert t['retired'] and not t['destination'].exists()
    assert unrelated.read_bytes() == b'user'
    assert not list(t['destination'].parent.glob('.sr-*'))


@pytest.mark.parametrize('message', [dict(kind='failed',error='native failure'),
    dict(kind='initialized',adapter_luid=999), dict(kind='progress',frames=9,total=8),
    dict(kind='complete',result=dict(status='complete',real_frames=7,output_rate='30',width=128,height=96))])
def test_invalid_or_failed_worker_cannot_publish(transaction,message):
    t = transaction; t['messages'] = [message]
    with pytest.raises(RuntimeError): t['export']()
    assert t['retired'] and not t['destination'].exists()
    assert not list(t['destination'].parent.glob('.sr-*'))


def test_source_mutation_after_preflight_cannot_publish(transaction,monkeypatch):
    t = transaction
    original = sr._spawn
    def mutate(*args):
        process = original(*args)
        t['source'].write_bytes(b'modified source')
        return process
    monkeypatch.setattr(sr,'_spawn',mutate)
    t['messages'] = [dict(kind='complete',result=t['result'])]
    with pytest.raises(RuntimeError,match='source changed'): t['export']()
    assert not t['destination'].exists()


def test_job_assignment_failure_sends_no_native_start(transaction,monkeypatch):
    t = transaction
    receiver,sender = sr.multiprocessing.Pipe()
    def fail(_): raise OSError('job assignment denied')
    monkeypatch.setattr('dlss5tool.process_job.KillOnCloseJob',fail)
    with pytest.raises(OSError,match='assignment'): t['export']()
    receiver.send_bytes.assert_not_called()
    assert t['retired'] and not t['destination'].exists()


def test_private_memory_guard_retires_worker_before_publication(transaction,monkeypatch):
    t = transaction
    monkeypatch.setattr('dlss5tool.process_job.private_commit',lambda _:5*1024**3)
    with pytest.raises(MemoryError,match='4 GiB'):t['export']()
    assert t['retired'] and not t['destination'].exists()
