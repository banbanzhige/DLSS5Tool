"""Boundary and ownership gates for the calibrated policy, without real OOM."""
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from dlss5tool import memory_budget as mb
from dlss5tool.shared_cache_budget import SharedCacheBudget
from dlss5tool.super_resolution import estimate_resources

GIB=1024**3
MIB=1024**2


@pytest.mark.parametrize('total,reserve',[(8,2),(16,2),(32,4),(64,4),(128,4)])
@pytest.mark.parametrize('limiting',['physical','commit'])
def test_calibrated_admission_thresholds(total,reserve,limiting):
    state=dict(total_physical_bytes=total*GIB,free_physical_bytes=32*GIB,free_commit_bytes=32*GIB)
    key='free_physical_bytes' if limiting=='physical' else 'free_commit_bytes'
    state[key]=reserve*GIB+256*MIB
    assert mb.reserve_bytes(state)==reserve*GIB
    assert mb.memory_details(256*MIB,memory=state)['available_bytes']==256*MIB
    state[key]-=1
    details=mb.memory_details(256*MIB,memory=state)
    assert details['available_bytes']==256*MIB-1
    assert details['reason']==limiting and details['blocked_by_reserve']


def test_hdr_without_vsr_and_decoding_have_separate_costs():
    plain=estimate_resources(3840,2160,1,True,in_flight=1,use_super_resolution=False,decode_buffer=1)
    with_vsr=estimate_resources(3840,2160,1,True,in_flight=1,decode_buffer=1)
    assert plain['vsr_ram_bytes']==0
    assert plain['decode_ram_bytes']==3840*2160*48
    assert with_vsr['known_ram_bytes']-plain['known_ram_bytes']==3840*2160*24
    assert plain['recommended_ram_bytes']>plain['known_ram_bytes']


def test_fg_estimate_uses_actual_resized_output_dimensions():
    plan=estimate_resources(640,360,1,in_flight=1,use_super_resolution=False,
        use_enhancement=False,frame_generation_multiplier=4,frame_generation_size=(1920,1080))
    assert plan['frame_generation_ram_bytes']==1920*1080*4*4


def test_processing_allowance_prevents_background_cache_refill():
    import numpy as np
    from dlss5tool.guidance_cache import RawGuidanceCache
    owner=SharedCacheBudget(limit_bytes=1024)
    cache=RawGuidanceCache(0,SharedCacheBudget(owner.name))
    try:
        with owner.processing(4096):
            cache.put('denied',np.ones(128,np.float32))
            assert cache.bytes==0
        cache.put('accepted',np.ones(128,np.float32))
        assert cache.bytes==512 and owner.limit_bytes==1024
    finally:cache.close();owner.close()


def test_current_frame_overtakes_waiting_background_prefetch():
    owner=SharedCacheBudget(limit_bytes=GIB)
    state=dict(total_physical_bytes=16*GIB,free_physical_bytes=2*GIB+128*MIB,free_commit_bytes=16*GIB)
    order=[];errors=[]
    def work(label,priority):
        mb.set_processing_priority(priority)
        try:
            with owner.processing(100*MIB,timeout=1):
                order.append(label)
                time.sleep(.02)
        except Exception as error:errors.append(error)
    try:
        with mock.patch.object(mb,'system_memory',return_value=state):
            with owner.processing(100*MIB):
                background=threading.Thread(target=work,args=('prefetch',False));background.start()
                time.sleep(.04)
                current=threading.Thread(target=work,args=('current',True));current.start()
                deadline=time.monotonic()+.5
                while owner.snapshot()['waiting_processing_bytes']<200*MIB and time.monotonic()<deadline:time.sleep(.01)
            background.join(2);current.join(2)
        assert not errors and order==['current','prefetch']
    finally:owner.close()


def test_prefetch_recovery_requires_delay_and_higher_headroom(monkeypatch):
    import struct
    owner=SharedCacheBudget(limit_bytes=512*MIB)
    state=dict(total_physical_bytes=16*GIB,free_physical_bytes=2*GIB+200*MIB,free_commit_bytes=16*GIB)
    monkeypatch.setattr(mb,'system_memory',lambda:state)
    try:
        owner.request_reclaim(256*MIB)
        assert owner.under_pressure
        until=struct.unpack_from('<Q',owner.mapping,40)[0]
        monkeypatch.setattr('dlss5tool.shared_cache_budget.time.monotonic_ns',lambda:until+1)
        assert owner.under_pressure  # time alone must not restart prefetch
        state['free_physical_bytes']=2*GIB+256*MIB
        assert not owner.under_pressure
        assert owner.limit_bytes==512*MIB
    finally:owner.close()


def test_failed_owner_keeps_reservation_until_actual_retirement():
    owner=SharedCacheBudget(limit_bytes=1024)
    alive=threading.Event();alive.set()
    resource=SimpleNamespace(_process=SimpleNamespace(is_alive=alive.is_set),close=mock.Mock())
    try:
        with owner.processing(1024) as ticket:
            ticket.defer_until_retired(resource)
        assert owner.snapshot()['reserved_processing_bytes']==1024
        alive.clear()
        deadline=time.monotonic()+2
        while owner.snapshot()['reserved_processing_bytes'] and time.monotonic()<deadline:time.sleep(.02)
        assert owner.snapshot()['reserved_processing_bytes']==0
        resource.close.assert_called_once()
    finally:alive.clear();owner.close()


def test_pressure_diagnostic_preserves_failure_time_values(monkeypatch):
    state=dict(total_physical_bytes=16*GIB,free_physical_bytes=2*GIB,free_commit_bytes=16*GIB)
    monkeypatch.setattr(mb,'system_memory',lambda:state)
    owner=SharedCacheBudget(limit_bytes=1024)
    try:
        with pytest.raises(mb.MemoryPressureError) as error:
            mb.require_memory_headroom(256*MIB,pool=owner,stage='test',recommended_bytes=768*MIB)
        state['free_physical_bytes']=8*GIB
        assert error.value.details['memory']['free_physical_bytes']==2*GIB
        assert error.value.details['recommended_bytes']==768*MIB
        assert error.value.details['stage']=='test'
    finally:owner.close()


def test_gui_hdr_plan_matches_actual_slots_and_disabled_vsr():
    from dlss5tool import gui
    app=gui.App.__new__(gui.App)
    app._cleanup_video_export=mock.Mock()
    with mock.patch.object(gui,'estimate_resources',wraps=estimate_resources) as estimate, \
            mock.patch.object(gui,'require_memory_headroom') as guard, \
            mock.patch.object(gui,'query_gpu_memory',return_value=None), \
            mock.patch.object(gui,'select_in_flight',return_value=3), \
            mock.patch.object(gui,'find_ffmpeg',return_value='unused'), \
            mock.patch.object(gui,'FFmpegHDRVideoReader',side_effect=RuntimeError('reader reached')):
        with pytest.raises(RuntimeError,match='reader reached'):
            app._export_hdr_video('source',dict(is_hdr=True,profile='hdr10_pq'),'out',8,24,
                1920,1080,{},dict(super_resolution_scale=1),0,1)
    assert estimate.call_args.kwargs['in_flight']==3
    assert estimate.call_args.kwargs['use_super_resolution'] is False
    plan=estimate_resources(1920,1080,1,True,in_flight=3,use_super_resolution=False,decode_buffer=1)
    assert guard.call_args.args==(plan['known_ram_bytes'],)
    assert guard.call_args.kwargs['recommended_bytes']==plan['recommended_ram_bytes']


def test_vsr_reuses_existing_buffers_without_another_two_frame_admission(monkeypatch):
    state=dict(total_physical_bytes=16*GIB,free_physical_bytes=2*GIB,free_commit_bytes=16*GIB)
    monkeypatch.setattr(mb,'system_memory',lambda:state)
    connection=mock.Mock();connection.recv.return_value={'operation':'process'}
    assert mb.receive_worker_command(connection,128*MIB,allocation_bytes=0)['operation']=='process'
