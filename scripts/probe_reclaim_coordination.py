"""Small deterministic admission probes; no real RAM pressure or GPU work.

The candidate policy is installed in this process only. Product source files
are never changed. The optional pytest mode checks existing targeted tests
against the same in-memory candidate. Busy render-cache probes use its real
reclaimer; other probes deliberately control the byte ledger or scheduler.
"""
import argparse
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import subprocess
import sys
import textwrap
import threading
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dlss5tool import memory_budget as mb
from dlss5tool import shared_cache_budget as sb

GIB, MIB = 1024**3, 1024**2
REQUIRED = 14*MIB
HEALTHY = dict(total_physical_bytes=round(31.92*GIB),
               free_physical_bytes=round(18.48*GIB),
               free_commit_bytes=round(14.79*GIB))


def acknowledgements(ticket, epoch):
    if epoch is None:
        return True
    records = [(i, ticket._record(i)) for i in range(sb.SLOTS)]
    total = sum(record[1] for _, record in records)
    target = struct.unpack_from('<Q', ticket.mapping, 32)[0]
    return all(not r[0] or not r[1] or r[4] & sb.PROCESSING or
               (r[5] >= epoch if ticket._has_metadata(i) else total <= target)
               for i, r in records)


@contextmanager
def candidate_processing(self, required, *, stage='allocation', cancel=None,
                         timeout=None, recommended_bytes=None):
    """Proposal: fresh headroom and reservations decide admission, not ACKs.

    Queue ordering, cancellation, safe reserves, deferred owners, and the
    existing unknown-snapshot policy are preserved. Only the first waiter
    requests reclaim, only when its complete reservation would not fit.
    Every rejection follows a fresh decision, including after the last service.
    Callback scheduling itself is intentionally unchanged in this prototype.
    """
    required = max(0, int(required))
    ticket = sb.SharedCacheBudget(self.name, _processing=True)
    started = time.monotonic()
    deadline = started + (mb.RECLAIM_TIMEOUT if timeout is None else max(0, float(timeout)))
    epoch = None
    try:
        with ticket.locked():
            ticket._limit_locked()
            order = struct.unpack_from('<Q', ticket.mapping, 48)[0]+1
            struct.pack_into('<Q', ticket.mapping, 48, order)
            ticket._write(ticket.slot, pending=required, order=order,
                kind=sb.PROCESSING | (sb.BACKGROUND if not mb.processing_priority() else 0))
        while True:
            if cancel is not None:
                if callable(cancel):
                    cancel()
                elif cancel.is_set():
                    raise InterruptedError('Memory allocation cancelled')
            with ticket.locked():
                ticket.reap_locked()
                reserved, waiting = ticket._processing_locked()
                details = mb.memory_details(required, memory=mb.fresh_system_memory(), stage=stage)
                details['recommended_bytes'] = recommended_bytes
                available = details['available_bytes']
                turn = not required or not waiting or waiting[0][1] == ticket.slot
                fits = not required or available is None or required+reserved <= available
                ack = acknowledgements(ticket, epoch)
                details.update(reserved_processing_bytes=reserved, reclaim_epoch=epoch,
                    cache_bytes=sum(ticket.read(i)[1] for i in range(sb.SLOTS)),
                    reclaim_acknowledged=ack)
                # Bypass an absent ACK only on a known sufficient snapshot.
                # If sampling becomes unavailable after known pressure, retain
                # the old ACK requirement and the existing bounded wait.
                observable = not required or available is not None or ack
                if turn and fits and observable:
                    ticket._write(ticket.slot, kind=sb.PROCESSING | sb.ADMITTED)
                    break
                if turn and not fits and epoch is None:
                    epoch = ticket._request_locked(required+reserved)
                    details['reclaim_epoch'] = epoch
                    details['reclaim_acknowledged'] = acknowledgements(ticket, epoch)
                no_reclaim_possible = not details['cache_bytes'] and not reserved and turn
                reject = no_reclaim_possible or time.monotonic() >= deadline
            if reject:
                if not turn or (available is not None and required <= available < required+reserved):
                    details['failure'] = 'busy'
                elif not details['reclaim_acknowledged']:
                    details['failure'] = 'reclaim_timeout'
                mb.record_memory_event('rejected', **details)
                raise mb.MemoryPressureError(details)
            sb.service_reclaims()
            time.sleep(.025)
        if epoch is not None:
            event = 'reclaimed' if details['reclaim_acknowledged'] else 'admitted_after_wait'
            mb.record_memory_event(event, elapsed_seconds=time.monotonic()-started, **details)
        yield ticket
    finally:
        if not getattr(ticket, '_deferred_release', False):
            ticket.close()


@contextmanager
def policy(mode):
    original = sb.SharedCacheBudget.processing
    if mode == 'candidate':
        sb.SharedCacheBudget.processing = candidate_processing
    elif mode == 'baseline':
        # Keep the historical comparison reproducible after the fix lands.
        # Load only the admission method from the known local release tag.
        source = subprocess.run(['git','show','v3.1.3:dlss5tool/shared_cache_budget.py'],
            cwd=ROOT,check=True,capture_output=True,encoding='utf-8',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
        cls = next(node for node in ast.parse(source).body
                   if isinstance(node,ast.ClassDef) and node.name=='SharedCacheBudget')
        method = next(node for node in cls.body if isinstance(node,ast.FunctionDef)
                      and node.name=='processing')
        first = min([method.lineno]+[node.lineno for node in method.decorator_list])
        code = textwrap.dedent(''.join(source.splitlines(keepends=True)[first-1:method.end_lineno]))
        namespace = dict(sb.__dict__)
        namespace['service_reclaims'] = lambda:sb.service_reclaims()
        exec(compile(code,'<v3.1.3 admission>','exec'),namespace)
        sb.SharedCacheBudget.processing = namespace['processing']
    try:
        yield
    finally:
        sb.SharedCacheBudget.processing = original


def attempt(root, **kwargs):
    started = time.monotonic()
    try:
        with root.processing(REQUIRED, **kwargs):
            result = dict(outcome='admitted')
    except mb.MemoryPressureError as error:
        details = error.details
        result = dict(outcome='rejected', error=str(error), **{key: details.get(key) for key in (
            'failure', 'required_bytes', 'available_bytes', 'reserved_processing_bytes',
            'cache_bytes', 'reclaim_epoch', 'reclaim_acknowledged')})
    result['elapsed_seconds'] = round(time.monotonic()-started, 4)
    result['ledger_after'] = root.snapshot()
    return result


def busy_render(start_low):
    import numpy as np
    from dlss5tool.render_cache import RenderSession
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    session = RenderSession('synthetic-feedback', {}, 8*GIB,
                            pool=sb.SharedCacheBudget(root.name))
    pixels = np.zeros((4, 4, 4), np.uint8)
    session.cache[0] = (pixels, pixels.copy())
    session.bytes = 128
    session._publish()
    held, release = threading.Event(), threading.Event()
    def hold():
        with session.condition:
            held.set()
            release.wait(10)
    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(1)
    def memory():
        if start_low and not root.reclaim_epoch:
            return dict(HEALTHY, free_commit_bytes=mb.reserve_bytes(HEALTHY))
        return dict(HEALTHY)
    try:
        with mock.patch.object(mb, 'system_memory', side_effect=memory):
            result = attempt(root, stage='busy_render_cache')
            if result['outcome'] == 'rejected':
                result['retry_without_unlock_or_clear'] = attempt(root, stage='same_cache_retry')
            result['cache_lock_still_held'] = not release.is_set() and thread.is_alive()
            return result
    finally:
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        session.close()
        root.close()


def queue_only():
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    cache = sb.SharedCacheBudget(root.name)
    previous = sb.SharedCacheBudget(root.name, _processing=True)
    with root.locked():
        cache.publish_locked(128)
        previous._write(previous.slot, pending=REQUIRED, order=1, kind=sb.PROCESSING)
        struct.pack_into('<Q', root.mapping, 48, 1)
    cache.set_reclaimer(lambda: False)
    released = threading.Event()
    def finish_previous():
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if root.snapshot()['waiting_processing_bytes'] >= 2*REQUIRED:
                previous.close()
                released.set()
                return
            time.sleep(.001)
    thread = threading.Thread(target=finish_previous)
    thread.start()
    try:
        with mock.patch.object(mb, 'system_memory', return_value=HEALTHY):
            result = attempt(root, stage='queue_order_only', timeout=.12)
            result.update(predecessor_released=released.is_set(), reclaim_epoch=root.reclaim_epoch)
            return result
    finally:
        thread.join(3)
        assert not thread.is_alive()
        previous.close()
        cache.close()
        root.close()


def controlled_reclaim(ack, recover, *, legacy=False):
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    cache = sb.SharedCacheBudget(root.name)
    with cache.locked():
        if legacy:
            cache._clear_slot(cache.slot)
            sb.CACHE_RECORD.pack_into(cache.cache_mapping, sb.CACHE_HEADER+cache.slot*sb.CACHE_RECORD.size,
                                      __import__('os').getpid(), 512*MIB, 0)
        else:
            cache.publish_locked(128)
    calls = [0]
    state = dict(HEALTHY, free_commit_bytes=mb.reserve_bytes(HEALTHY))
    def reclaim():
        calls[0] += 1
        if recover:
            state.update(HEALTHY)
        return ack
    # Each deterministic scheduler call runs exactly one production callback.
    # No background thread is needed for this controlled interleaving.
    cache._reclaimer = reclaim
    try:
        with mock.patch.object(mb, 'system_memory', side_effect=lambda: dict(state)), \
                mock.patch.object(sb, 'service_reclaims', side_effect=cache.service_reclaim):
            result = attempt(root, stage='controlled_reclaim', timeout=.08)
            result.update(callback_calls=calls[0], legacy_record=legacy)
            return result
    finally:
        cache.close()
        root.close()


def reserved_credit():
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    try:
        with mock.patch.object(mb, 'system_memory', return_value=HEALTHY):
            available = mb.memory_headroom_bytes()
            with root.processing(available-REQUIRED+1, stage='existing_reservation'):
                return attempt(root, stage='cannot_double_spend', timeout=.08)
    finally:
        root.close()


def unknown_snapshot(start_low):
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    cache = sb.SharedCacheBudget(root.name)
    with cache.locked():
        cache.publish_locked(128)
    cache._reclaimer = lambda: False
    def memory():
        if start_low and not root.reclaim_epoch:
            return dict(HEALTHY, free_commit_bytes=mb.reserve_bytes(HEALTHY))
        return None
    try:
        with mock.patch.object(mb, 'system_memory', side_effect=memory), \
                mock.patch.object(sb, 'service_reclaims', side_effect=cache.service_reclaim):
            return attempt(root, stage='unknown_snapshot', timeout=.08)
    finally:
        cache.close()
        root.close()


def last_service():
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    cache = sb.SharedCacheBudget(root.name)
    with cache.locked():
        cache.publish_locked(128)
    state = dict(HEALTHY, free_commit_bytes=mb.reserve_bytes(HEALTHY))
    def reclaim():
        time.sleep(.04)
        state.update(HEALTHY)
        with cache.locked():
            cache.publish_locked(0)
    try:
        with mock.patch.object(mb, 'system_memory', side_effect=lambda: dict(state)), \
                mock.patch.object(sb, 'service_reclaims', side_effect=reclaim):
            result = attempt(root, stage='last_service_completed', timeout=.01)
            result['actual_available_after_service'] = mb.memory_headroom_bytes()
            return result
    finally:
        cache.close()
        root.close()


def cancelled():
    root = sb.SharedCacheBudget(limit_bytes=8*GIB)
    cancel = threading.Event()
    cancel.set()
    try:
        try:
            with root.processing(REQUIRED, cancel=cancel):
                raise AssertionError('Cancelled work was admitted')
        except InterruptedError:
            return dict(outcome='cancelled', ledger_after=root.snapshot())
    finally:
        root.close()


def run_probes(mode):
    cases = {
        'healthy_busy_cache': lambda: busy_render(False),
        'recovered_busy_cache': lambda: busy_render(True),
        'queue_order_only': queue_only,
        'no_ack_no_recovery': lambda: controlled_reclaim(False, False),
        'ack_without_os_recovery': lambda: controlled_reclaim(True, False),
        'ack_and_os_recovery': lambda: controlled_reclaim(True, True),
        'legacy_no_ack_with_os_recovery': lambda: controlled_reclaim(False, True, legacy=True),
        'reserved_credit': reserved_credit,
        'unknown_initial': lambda: unknown_snapshot(False),
        'known_pressure_then_unknown_no_ack': lambda: unknown_snapshot(True),
        'last_service_completed': last_service,
        'cancelled': cancelled,
    }
    expected = {
        'healthy_busy_cache': 'admitted',
        'recovered_busy_cache': 'rejected' if mode == 'baseline' else 'admitted',
        'queue_order_only': 'rejected' if mode == 'baseline' else 'admitted',
        'no_ack_no_recovery': 'rejected',
        'ack_without_os_recovery': 'rejected',
        'ack_and_os_recovery': 'admitted',
        'legacy_no_ack_with_os_recovery': 'rejected' if mode == 'baseline' else 'admitted',
        'reserved_credit': 'rejected',
        'unknown_initial': 'admitted',
        'known_pressure_then_unknown_no_ack': 'rejected',
        'last_service_completed': 'rejected' if mode == 'baseline' else 'admitted',
        'cancelled': 'cancelled',
    }
    results = {}
    with policy(mode):
        for name, probe in cases.items():
            results[name] = probe()
            assert results[name]['outcome'] == expected[name], (name, results[name])
            ledger = results[name].get('ledger_after')
            if ledger:
                assert ledger['waiting_processing_bytes'] == 0, (name, ledger)
                if name != 'reserved_credit':
                    assert ledger['reserved_processing_bytes'] == 0, (name, ledger)
    details = dict(required_bytes=REQUIRED, available_bytes=10*GIB, reserved_processing_bytes=0,
        cache_bytes=128, reclaim_epoch=3, reclaim_acknowledged=False,
        failure='reclaim_timeout', stage='nr_output')
    return dict(policy=mode, scope='simulated OS snapshots; tiny actual cache; no GPU',
        cases=results, diagnostics_transport=dict(before=details, after=mb.copy_pressure_details(details)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=('current', 'baseline', 'candidate'), default='current')
    parser.add_argument('--pytest', nargs=argparse.REMAINDER,
                        help='Run only explicitly supplied existing tests under this policy')
    args = parser.parse_args()
    if args.pytest is not None:
        if not args.pytest:
            parser.error('--pytest needs explicit test paths')
        import pytest
        with policy(args.policy):
            return pytest.main(args.pytest)
    print(json.dumps(run_probes(args.policy), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
