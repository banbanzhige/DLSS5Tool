"""Coordinated pressure tests use byte ledgers and simulated OS snapshots."""
import ctypes as C
import multiprocessing
import threading
import time
from unittest import mock

import pytest

from dlss5tool import memory_budget as mb
from dlss5tool.shared_cache_budget import SharedCacheBudget, CACHE_RECORD, CACHE_HEADER

GIB=1024**3
MIB=1024**2


def memory(free,commit=16*GIB,total=16*GIB):
    return dict(total_physical_bytes=total,free_physical_bytes=free,free_commit_bytes=commit)


def idle_cache_child(name,free,connection):
    pool=SharedCacheBudget(name)
    mb.system_memory=lambda:memory(free.value)
    with pool.locked():pool.publish_locked(4*GIB)
    def trim():
        with pool.locked():
            old=pool.read(pool.slot)[1]
            target=pool.allowance_locked()
            pool.publish_locked(min(old,target))
            with free.get_lock():free.value+=old-min(old,target)
        return True
    pool.set_reclaimer(trim)
    connection.send('ready')
    try:connection.recv()
    finally:pool.close();connection.close()


def legacy_cache_child(name,free,connection):
    import struct
    pool=SharedCacheBudget(name)
    with pool.locked():
        pool._clear_slot(pool.slot)
        CACHE_RECORD.pack_into(pool.cache_mapping,CACHE_HEADER+pool.slot*CACHE_RECORD.size,
                               __import__('os').getpid(),4*GIB,0)
    connection.send('ready')
    try:
        while not connection.poll(.05):
            with pool.locked():
                # Exactly the old worker's data layout; no processing metadata
                # or generation acknowledgement is published by this process.
                old=pool.read(pool.slot)[1]
                allowed=struct.unpack_from('<Q',pool.cache_mapping,8)[0]
                retained=min(old,allowed)
                CACHE_RECORD.pack_into(pool.cache_mapping,CACHE_HEADER+pool.slot*CACHE_RECORD.size,
                                       __import__('os').getpid(),retained,0)
                with free.get_lock():free.value+=old-retained
    finally:pool.close();connection.close()


def crashed_allocator_child(name,connection):
    import os
    pool=SharedCacheBudget(name)
    with pool.processing(1024*MIB):
        connection.send('reserved')
        connection.recv()
        os._exit(0)


def test_reclaim_makes_room_without_lowering_safety_reserve():
    owner=SharedCacheBudget(limit_bytes=8*GIB)
    cache=SharedCacheBudget(owner.name)
    state=memory(mb.reserve_bytes(memory(0)))
    with cache.locked():cache.publish_locked(4*GIB)
    def trim():
        with cache.locked():
            old=cache.read(cache.slot)[1]
            retained=min(old,cache.allowance_locked())
            cache.publish_locked(retained)
            state['free_physical_bytes']+=old-retained
        return True
    cache.set_reclaimer(trim)
    try:
        with mock.patch.object(mb,'system_memory',side_effect=lambda:dict(state)):
            with owner.processing(256*MIB,stage='test_reclaim') as ticket:
                assert mb.memory_headroom_bytes()>=256*MIB
                assert owner.snapshot()['reserved_processing_bytes']==256*MIB
                assert cache.read(cache.slot)[1]<4*GIB
                assert owner.under_pressure
                ticket.consume(256*MIB)
                assert owner.snapshot()['reserved_processing_bytes']==0
            assert owner.snapshot()['waiting_processing_bytes']==0
    finally:cache.close();owner.close()


def test_concurrent_processing_cannot_double_spend_and_waiter_recovers():
    owner=SharedCacheBudget(limit_bytes=GIB)
    state=memory(mb.reserve_bytes(memory(0))+128*MIB)
    acquired=threading.Event()
    errors=[]
    def contender():
        try:
            with owner.processing(100*MIB,timeout=1):acquired.set()
        except Exception as error:errors.append(error)
    try:
        with mock.patch.object(mb,'system_memory',return_value=state):
            with owner.processing(100*MIB):
                thread=threading.Thread(target=contender)
                thread.start()
                assert not acquired.wait(.08)
                assert owner.snapshot()['reserved_processing_bytes']==100*MIB
                with owner.processing(0,timeout=.03):pass  # reused worker buffers must not wait behind this waiter
            thread.join(2)
            assert acquired.is_set() and not errors
            assert not owner.snapshot()['reserved_processing_bytes']
    finally:owner.close()


@pytest.mark.parametrize('release_ledger',[False,True])
def test_acknowledgement_and_actual_free_memory_are_both_required(release_ledger):
    owner=SharedCacheBudget(limit_bytes=GIB)
    cache=SharedCacheBudget(owner.name)
    with cache.locked():cache.publish_locked(512*MIB)
    def trim():
        if release_ledger:
            with cache.locked():cache.publish_locked(0)
            return True  # another owner still holds the data: OS free unchanged
        return False
    cache.set_reclaimer(trim)
    state=memory(mb.reserve_bytes(memory(0)))
    try:
        with mock.patch.object(mb,'system_memory',return_value=state):
            with pytest.raises(mb.MemoryPressureError) as error:
                with owner.processing(128*MIB,timeout=.07):pytest.fail('admitted')
            if not release_ledger:assert error.value.details['failure']=='reclaim_timeout'
            assert owner.snapshot()['reserved_processing_bytes']==0
            assert owner.snapshot()['waiting_processing_bytes']==0
    finally:cache.close();owner.close()


def test_cancel_and_allocation_failure_return_reservations():
    owner=SharedCacheBudget(limit_bytes=GIB)
    cancelled=threading.Event();cancelled.set()
    try:
        with pytest.raises(InterruptedError):
            with owner.processing(1024,cancel=cancelled):pytest.fail('admitted')
        with pytest.raises(MemoryError,match='injected allocation failure'):
            with mb.memory_allocation(1024,pool=owner,stage='injected'):
                raise MemoryError('injected allocation failure')
        assert owner.snapshot()['waiting_processing_bytes']==0
        assert owner.snapshot()['reserved_processing_bytes']==0
        assert mb.memory_events()[-1]['event']=='allocation_failed'
    finally:owner.close()


def test_actual_snapshot_refresh_bypasses_quarter_second_cache(monkeypatch):
    old=memory(GIB)
    def read_status(pointer):
        status=C.cast(pointer,C.POINTER(mb._MemoryStatus)).contents
        status.total_physical=16*GIB;status.free_physical=8*GIB;status.free_commit=16*GIB
        return 1
    monkeypatch.setattr(mb,'_snapshot',old)
    monkeypatch.setattr(mb,'_snapshot_time',time.monotonic())
    monkeypatch.setattr(mb,'_status_function',lambda:read_status)
    assert mb.system_memory()==old
    assert mb.fresh_system_memory()['free_physical_bytes']==8*GIB


@pytest.mark.parametrize('child_target',[idle_cache_child,legacy_cache_child])
def test_paused_child_cache_acknowledges_parent_reclaim(child_target):
    owner=SharedCacheBudget(limit_bytes=8*GIB)
    ctx=multiprocessing.get_context('spawn')
    free=ctx.Value('Q',mb.reserve_bytes(memory(0)))
    parent,child=ctx.Pipe()
    process=ctx.Process(target=child_target,args=(owner.name,free,child))
    try:
        process.start();assert parent.poll(10);assert parent.recv()=='ready'
        with mock.patch.object(mb,'system_memory',side_effect=lambda:memory(free.value)):
            with owner.processing(256*MIB,timeout=2):
                assert owner.snapshot()['used_bytes']<4*GIB
        parent.send('stop');process.join(5)
        assert process.exitcode==0
    finally:
        if process.is_alive():process.terminate();process.join(5)
        process.close();parent.close();child.close();owner.close()


def test_reclaim_does_not_drop_render_target_or_temporal_state():
    import numpy as np
    from dlss5tool.render_cache import RenderSession
    owner=SharedCacheBudget(limit_bytes=1024)
    session=RenderSession('synthetic',{},1024,pool=SharedCacheBudget(owner.name))
    pixels=np.zeros((4,4,4),np.uint8)
    session.cache[0]=(pixels,pixels.copy());session.bytes=128
    session.produced=10
    try:
        owner.set_limit(0)
        assert session._reclaim()
        assert 0 in session.cache
        assert session.produced==10 and not session.restart
    finally:session.close();owner.close()


def test_schema_mismatch_is_rejected_without_overwriting_ledger():
    owner=SharedCacheBudget(limit_bytes=1234)
    try:
        owner.mapping[:8]=b'D5CACHE2'
        with pytest.raises(ValueError,match='not initialized'):SharedCacheBudget(owner.name)
        assert owner.limit_bytes==1234
    finally:owner.close()


def test_crashed_processing_owner_does_not_leave_a_reservation():
    owner=SharedCacheBudget(limit_bytes=GIB)
    ctx=multiprocessing.get_context('spawn');parent,child=ctx.Pipe()
    process=ctx.Process(target=crashed_allocator_child,args=(owner.name,child))
    try:
        process.start();assert parent.poll(10);assert parent.recv()=='reserved'
        assert owner.snapshot()['reserved_processing_bytes']==1024*MIB
        parent.send('crash');process.join(5)
        assert process.exitcode==0
        assert owner.snapshot()['reserved_processing_bytes']==0
    finally:
        if process.is_alive():process.terminate();process.join(5)
        process.close();parent.close();child.close();owner.close()


def test_idle_soft_pressure_can_accept_stop_without_retiring(monkeypatch):
    connection=mock.Mock()
    connection.poll.side_effect=[False,True]
    connection.recv.return_value={'operation':'stop'}
    monkeypatch.setattr(mb,'system_memory',lambda:memory(mb.reserve_bytes(memory(0))))
    assert mb.receive_worker_command(connection,1024)=={'operation':'stop'}


def test_idle_critical_pressure_still_retires_before_reading_command(monkeypatch):
    connection=mock.Mock();connection.poll.return_value=False
    monkeypatch.setattr(mb,'system_memory',lambda:memory(128*MIB))
    with pytest.raises(mb.MemoryPressureError):mb.receive_worker_command(connection,1024)
    connection.recv.assert_not_called()
