"""Queue recovery preserves task identity and blocks allocations until retirement."""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import gc
import time
import weakref

import numpy as np
import pytest

from dlss5tool import export_queue, gui, memory_budget as mb
from dlss5tool import queue_memory_recovery as recovery
from dlss5tool.deferred_error import ErrorSnapshot
from dlss5tool.dlss_host_process import _HostSession, HostProcessError, ProcessLive
from dlss5tool.super_resolution import ProcessSuperResolution, SuperResolutionError

MIB, GIB = 1024**2, 1024**3


def memory(free):
    return dict(total_physical_bytes=32*GIB, free_physical_bytes=free, free_commit_bytes=free)


def pressure():
    return mb.memory_details(MIB, memory=memory(1), stage='worker_idle')


class Root:
    def __init__(self):
        self.callbacks = {}
        self.next_id = 0

    def after(self, delay, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id

    def after_cancel(self, identity):
        self.callbacks.pop(identity, None)

    def tick(self):
        key = next(iter(self.callbacks))
        self.callbacks.pop(key)()


@pytest.fixture
def app(tmp_path, monkeypatch):
    a = gui.App.__new__(gui.App)
    a.root = Root()
    a._queue_running = True
    a._queue_pause_requested = False
    a._queue_active_job_id = None
    a._exporting = False
    a._queue_jobs = [export_queue.ExportJob.create(str(tmp_path/f'{name}.png'),
        str(tmp_path/f'{name}-out.png'), dict(style=2), {}) for name in ('done','current','later')]
    a._queue_jobs[0].state = 'completed'
    Path(a._queue_jobs[0].output_path).write_bytes(b'completed-output')
    a.queue_tree = mock.Mock()
    for name in ('logln','set_status','_refresh_queue_tree','_update_queue_action_states',
                 '_update_action_labels','_update_host_control_states','_freeze_preview_cache'):
        setattr(a, name, mock.Mock())
    a._wait_play_dlss = mock.Mock(return_value=True)
    a._close_live = mock.Mock()
    a._close_super_resolution = mock.Mock()
    a._normalize_queue_output_path = lambda job: job.output_path
    a._save_queue_state = lambda: export_queue.save(a._queue_jobs, str(tmp_path/'queue.json'))
    a.clock = [1.0]
    a.available = [12*GIB]
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(monotonic=lambda:a.clock[0], time=time.time))
    monkeypatch.setattr(mb, 'fresh_system_memory', lambda: memory(a.available[0]))
    monkeypatch.setattr(gui.messagebox, 'showinfo', mock.Mock())
    monkeypatch.setattr(gui.traceback, 'print_exc', mock.Mock())
    a._export_image_source = mock.Mock()
    a._export_video_source = a._export_image_source
    return a


def success(**kwargs):
    return dict(success=True,cancelled=False,error='',output_path=kwargs['out_path'],frames=1)


def drain(app, bound=30):
    for _ in range(bound):
        if not app.root.callbacks:
            return
        app.clock[0] += .25
        app.root.tick()
    raise AssertionError('Queue did not finish in bounded callbacks')


@pytest.mark.parametrize('kind', ['image','video'])
@pytest.mark.parametrize('transport', ['exception','result'])
def test_retry_same_item_after_recovery_preserves_completed_and_later(app, kind, transport):
    current = app._queue_jobs[1]
    current.media_kind = kind
    original = current.settings.copy()
    calls = []
    def export(source, **kwargs):
        calls.append(source)
        if len(calls)==1:
            if transport=='exception':
                raise mb.MemoryPressureError(pressure())
            return dict(success=False,cancelled=False,error='pressure',output_path=kwargs['out_path'],
                        memory_pressure=pressure())
        return success(**kwargs)
    app._export_image_source.side_effect = export
    app._run_next_queue_job()
    assert current.state=='running' and app._queue_memory_recovery is not None
    app.available[0] = 1
    for _ in range(3):
        app.root.tick()
    assert len(calls)==1
    app.available[0] = 12*GIB
    drain(app)
    assert calls == [current.source_path,current.source_path,app._queue_jobs[2].source_path]
    assert all(j.state=='completed' for j in app._queue_jobs)
    assert current.settings==original
    assert Path(app._queue_jobs[0].output_path).read_bytes()==b'completed-output'
    app._close_live.assert_called_once()
    app._close_super_resolution.assert_called_once()


def test_ordinary_error_does_not_auto_retry(app):
    app._export_image_source.side_effect = [ValueError('bad input'),success(out_path='later.png')]
    app._run_next_queue_job();drain(app)
    assert app._queue_jobs[1].state=='failed'
    assert app._queue_jobs[2].state=='completed'
    assert app._export_image_source.call_count==2
    app._close_live.assert_not_called()


def test_retry_exhaustion_pauses_before_later_items(app):
    app._export_image_source.side_effect = lambda *a,**k: (_ for _ in ()).throw(mb.MemoryPressureError(pressure()))
    app._run_next_queue_job();drain(app)
    assert app._export_image_source.call_count==3
    assert app._queue_jobs[1].state=='failed'
    assert app._queue_jobs[2].state=='pending'
    assert not app._queue_running and app._queue_last_summary=='paused'


@pytest.mark.parametrize('cancelled', [True,False])
def test_wait_can_cancel_or_pause_without_restarting(app, cancelled):
    app._export_image_source.side_effect=mb.MemoryPressureError(pressure())
    app._run_next_queue_job()
    if cancelled:
        app.cancel_current_queue_job()
    else:
        app.pause_export_queue_after_current()
    drain(app)
    assert app._queue_jobs[1].state==('cancelled' if cancelled else 'pending')
    assert app._queue_jobs[2].state=='pending'
    assert app._export_image_source.call_count==1
    assert not app._queue_running


def test_failed_close_keeps_owner_and_blocks_retry_until_it_retires(app):
    app._export_image_source.side_effect=[mb.MemoryPressureError(pressure()),success(out_path='current.png'),success(out_path='later.png')]
    app._close_live.side_effect=RuntimeError('still retiring')
    app._run_next_queue_job()
    for _ in range(3):app.root.tick()
    assert app._export_image_source.call_count==1 and app._queue_cleanup_pending
    assert len(app._pending_export_cleanup)==1
    app._close_live.side_effect=None
    drain(app)
    assert all(j.state=='completed' for j in app._queue_jobs)


def test_timeout_while_retiring_pauses_and_retains_cleanup(app):
    app._export_image_source.side_effect=mb.MemoryPressureError(pressure())
    app._close_live.side_effect=RuntimeError('still retiring')
    app._run_next_queue_job();app.root.tick()
    app.clock[0]+=recovery.WAIT_TIMEOUT
    app.root.tick()
    assert not app._queue_running and app._queue_jobs[1].state=='failed'
    assert app._queue_jobs[2].state=='pending' and app._queue_cleanup_pending
    assert len(app._pending_export_cleanup)==1


def test_pending_queue_retirement_blocks_other_gpu_work_and_allows_cleanup_retry(app):
    app._queue_running=False;app._queue_cleanup_pending=True
    assert app._gpu_conflict('preview').owner=='release'
    assert app._gpu_conflict('export').owner=='release'
    app._retire_queue_memory_owners=mock.Mock(return_value=False)
    app._gpu_admit=mock.Mock()
    app.start_export_queue()
    app._retire_queue_memory_owners.assert_called_once()
    app._gpu_admit.assert_not_called()


def test_unknown_or_fluctuating_headroom_does_not_restart():
    state=recovery.Recovery('job',pressure(),1,1,retired=True)
    assert state.poll(None,2)=='wait'
    assert state.poll(memory(12*GIB),3)=='wait'
    assert state.poll(memory(1),3.4)=='wait'
    assert state.poll(memory(12*GIB),4)=='wait'
    assert state.poll(memory(12*GIB),4.6)=='ready'
    assert state.poll(memory(12*GIB),121)=='timeout'


def test_timeout_under_real_headroom_gate_keeps_later_items_pending(app):
    app._export_image_source.side_effect=mb.MemoryPressureError(pressure())
    app.available[0]=1
    app._run_next_queue_job();app.root.tick()
    app.clock[0]+=recovery.WAIT_TIMEOUT
    app.root.tick()
    assert app._export_image_source.call_count==1
    assert app._queue_jobs[1].state=='failed' and app._queue_jobs[2].state=='pending'
    assert not app._queue_running


def test_image_export_result_keeps_structured_pressure(app, tmp_path, monkeypatch):
    app._collect_settings=lambda:dict(super_resolution_scale=1,output_view=0,output_mix=1)
    app._ensure_shared_cache_pool=lambda:SimpleNamespace(name='fixture')
    app._confirm_super_resolution_export=mock.Mock(return_value=True)
    app._begin_export_ui=mock.Mock();app._end_export_ui=mock.Mock()
    app._remember_export=mock.Mock();app._export_t0=time.perf_counter()
    app._process_still_image=mock.Mock(side_effect=mb.MemoryPressureError(pressure()))
    monkeypatch.setattr(gui,'read_still',lambda _: (np.zeros((4,4,3),np.uint8),None))
    result=gui.App._export_image_source(app,str(tmp_path/'input.png'),{},str(tmp_path/'out.png'),False)
    assert not result['success'] and result['memory_pressure']['stage']=='worker_idle'


def test_gpu_writer_error_keeps_structured_pressure():
    from dlss5tool.gpu_export_process import ProcessGpuVideoWriter
    writer=ProcessGpuVideoWriter.__new__(ProcessGpuVideoWriter)
    writer._timeout=1;writer._cancel=None;writer._connection=mock.Mock()
    writer._connection.recv.return_value=dict(ok=False,error='pressure',memory_pressure=pressure())
    writer._terminate=mock.Mock()
    with pytest.raises(RuntimeError) as error:writer._receive()
    assert mb.pressure_details(error.value) is not None
    writer._terminate.assert_called_once()


def test_error_snapshot_preserves_only_scalar_pressure_and_releases_arrays():
    array=np.zeros((128,128));reference=weakref.ref(array)
    error=mb.MemoryPressureError(pressure())
    error.details['frame']=array
    error.details['memory']['frame']=array
    error.custom=array
    snapshot=ErrorSnapshot(error)
    del array,error;gc.collect()
    assert reference() is None
    restored=snapshot.exception()
    assert mb.pressure_details(restored)['stage']=='worker_idle'
    assert 'frame' not in restored.memory_pressure


def test_host_pressure_survives_multiple_reads_after_worker_exit():
    session=_HostSession.__new__(_HostSession)
    session._closed=False;session._process=SimpleNamespace(is_alive=lambda:False)
    with pytest.raises(HostProcessError):
        session._check_response(dict(ok=False,error='pressure',memory_pressure=pressure()))
    for _ in range(2):
        with pytest.raises(HostProcessError) as failure:session.request('update')
        assert mb.pressure_details(failure.value) is not None


def test_worker_admission_pressure_does_not_allocate_fallback_backend(monkeypatch):
    from dlss5tool import dlss_host_process as host
    owner=ProcessLive.__new__(ProcessLive)
    owner._live_factory=object()
    owner._auto_candidates=lambda _:['v2','legacy']
    failure=HostProcessError('worker admission pressure')
    failure.memory_pressure=pressure()
    constructor=mock.Mock(side_effect=failure)
    monkeypatch.setattr(host,'_HostSession',constructor)
    with pytest.raises(HostProcessError):owner._start_session(64,64,dict(host_backend='auto'))
    assert constructor.call_count==1


def test_vsr_reply_preserves_pressure_for_later_consumers():
    live=ProcessSuperResolution.__new__(ProcessSuperResolution)
    live._process=SimpleNamespace(is_alive=lambda:False)
    live._connection=mock.Mock()
    live._connection.poll.return_value=True
    live._connection.recv.return_value=dict(ok=False,error='pressure',memory_pressure=pressure())
    live._log_tail=lambda:''
    with pytest.raises(SuperResolutionError) as failure:live._receive('process')
    assert mb.pressure_details(failure.value) is not None
    live._connection.poll.return_value=False
    with pytest.raises(SuperResolutionError) as repeated:live._receive('process')
    assert mb.pressure_details(repeated.value) is not None


@pytest.mark.parametrize('free', [0,4*GIB])
def test_zero_allocation_worker_still_retires_when_idle_headroom_exhausted(monkeypatch,free):
    connection=mock.Mock();connection.poll.return_value=False
    monkeypatch.setattr(mb,'system_memory',lambda:memory(free))
    clock=iter((1.,7.))
    monkeypatch.setattr(mb,'time',SimpleNamespace(monotonic=lambda:next(clock),time=time.time))
    monkeypatch.setattr(mb,'current_pool',lambda **_:None)
    with pytest.raises(mb.MemoryPressureError):
        mb.receive_worker_command(connection,1024,allocation_bytes=0)
    connection.recv.assert_not_called()


@pytest.mark.parametrize('bad', [-1, float('nan'), float('inf'), True, np.zeros(1)])
def test_invalid_pressure_sizes_cannot_drive_recovery(bad):
    assert mb.copy_pressure_details(dict(required_bytes=bad)) is None
