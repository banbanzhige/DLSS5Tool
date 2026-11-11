"""Security boundaries of the MessageBus worker-token compatibility change."""
import os
import struct
from types import SimpleNamespace
from unittest import mock

import pytest

from dlss5tool import nvidia_token_compat as compat


def ace(sid, mask, kind=0, flags=0):
    return struct.pack('<BBHI', kind, flags, 8+len(sid), mask)+sid


def acl(*entries, padding=b''):
    return struct.pack('<BBHHH', 2, 0, 8+sum(map(len, entries))+len(padding), len(entries), 0)+b''.join(entries)+padding


LOGON = b'\x01\x03\x00\x00\x00\x00\x00\x05'+struct.pack('<III', 5, 17, 23)
OBSERVED = acl(ace(LOGON, compat.TOKEN_ALL_ACCESS), ace(compat._OWNER_RIGHTS, compat.READ_CONTROL),
               ace(compat._ADMINISTRATORS, compat.TOKEN_QUERY))
NORMAL = acl(ace(compat._SYSTEM, compat.TOKEN_ALL_ACCESS))
STATE = dict(id=200, type=1, restricted=True, app_container=False, integrity_rid=8192)


def test_only_read_control_added_and_all_existing_ace_bytes_preserved():
    original = OBSERVED+bytes(8)
    original = original[:2]+struct.pack('<H', len(original))+original[4:]
    candidate, reason = compat._plan(original, STATE)
    before, end = compat._aces(original)
    after, new_end = compat._aces(candidate)
    assert reason == 'restricted_messagebus_read_control'
    assert after[:-1] == before
    assert candidate[new_end:] == original[end:]
    assert after[-1][2] == ace(compat._SYSTEM, compat.READ_CONTROL)
    assert compat._plan(candidate, STATE) == (None, 'system_read_control_already_present')


@pytest.mark.parametrize('original', [NORMAL, None])
def test_existing_access_never_rewritten(original):
    assert compat._plan(original, STATE)[0] is None


@pytest.mark.parametrize('original', [
    acl(),
    acl(ace(compat._SYSTEM, compat.READ_CONTROL, kind=1), *[r[2] for r in compat._aces(OBSERVED)[0]]),
    acl(ace(LOGON, compat.TOKEN_ALL_ACCESS, flags=16), *[r[2] for r in compat._aces(OBSERVED)[0][1:]]),
    acl(ace(LOGON, compat.TOKEN_ALL_ACCESS), ace(compat._OWNER_RIGHTS, compat.READ_CONTROL),
        ace(compat._ADMINISTRATORS, compat.TOKEN_QUERY | 0x20)),
    acl(ace(LOGON, compat.TOKEN_ALL_ACCESS), ace(LOGON, compat.TOKEN_ALL_ACCESS),
        ace(compat._ADMINISTRATORS, compat.TOKEN_QUERY)),
    acl(ace(compat._SYSTEM, compat.TOKEN_QUERY), *[r[2] for r in compat._aces(OBSERVED)[0]]),
])
def test_unrecognized_policies_and_denies_left_untouched(original):
    assert compat._plan(original, STATE)[0] is None


@pytest.mark.parametrize('key,value', [('restricted', False), ('type', 2),
                                      ('app_container', True), ('integrity_rid', 4096),
                                      ('integrity_rid', 12288)])
def test_restricted_flag_alone_is_not_the_enable_switch(key, value):
    assert compat._plan(OBSERVED, dict(STATE, **{key:value}))[0] is None


@pytest.mark.parametrize('original', [b'', b'\x02'*7, b'\x02\0\x08\0\x01\0\0\0',
                                    OBSERVED[:2]+b'\xff\xff'+OBSERVED[4:]])
def test_malformed_acls_rejected_before_native_write(original):
    with pytest.raises(ValueError):
        compat._plan(original, STATE)


class Api:
    def __init__(self):
        self.k = SimpleNamespace(GetCurrentProcess=lambda: 11)
        self.parent = dict(STATE, id=100, restricted=False)
        self.child = dict(STATE)
        self.parent_dacl, self.child_dacl = NORMAL, OBSERVED
        self.opened, self.closed, self.writes = [], [], []
        self.pid_value, self.active = os.getpid()+10000, True
        self.deny_write = self.fail_verification = self.fail_restore = False

    def pid(self, handle): return self.pid_value
    def running(self, handle): return self.active

    def open_token(self, process, access):
        self.opened.append((process, access))
        if access & compat._WRITE_DAC and self.deny_write:
            raise OSError('access denied')
        return 101 if process == 11 else 102 if not access & compat._WRITE_DAC else 103

    def state(self, token): return dict(self.parent if token == 101 else self.child)

    def dacl(self, token):
        if token == 101: return self.parent_dacl
        if self.fail_verification and self.writes and self.child_dacl != OBSERVED:
            self.fail_verification = False
            return self.child_dacl+b'corrupt'
        return self.child_dacl

    def set_dacl(self, token, data):
        assert token == 103
        self.writes.append(data)
        if self.fail_restore and len(self.writes) > 1:
            raise OSError('restore denied')
        self.child_dacl = data

    def close(self, token): self.closed.append(token)


def test_owned_child_changed_parent_unchanged_and_all_handles_closed():
    api = Api()
    result = compat._prepare(api, 22)
    assert result['status'] == 'applied' and result['permission_mask'] == compat.READ_CONTROL
    assert result['parent_token_unchanged']
    assert api.parent_dacl == NORMAL and api.parent['id'] == 100
    assert api.writes == [compat._plan(OBSERVED, STATE)[0]]
    assert api.opened == [(11, 0x20008), (22, 0x20008), (22, 0x60008)]
    assert api.closed == [103, 102, 101]


@pytest.mark.parametrize('case', ['self', 'exited', 'shared', 'impersonation'])
def test_unsafe_owner_or_token_identity_refused(case):
    api = Api()
    if case == 'self': api.pid_value = os.getpid()
    if case == 'exited': api.active = False
    if case == 'shared': api.child['id'] = api.parent['id']
    if case == 'impersonation': api.child['type'] = 2
    assert compat._prepare(api, 22)['status'] == 'declined'
    assert not api.writes


def test_normal_child_never_requests_write_dac():
    api = Api()
    api.child_dacl = NORMAL
    api.child['restricted'] = False
    assert compat._prepare(api, 22)['status'] == 'unchanged'
    assert not api.writes and len(api.opened) == 2


def test_candidate_write_denied_aborts_startup_without_elevation_or_change():
    api = Api()
    api.deny_write = True
    with pytest.raises(compat.TokenCompatibilityError, match='startup refused'):
        compat._prepare(api, 22)
    assert not api.writes and api.child_dacl == OBSERVED
    assert api.closed == [102, 101]


def test_verification_failure_restores_exact_original_acl():
    api = Api()
    api.fail_verification = True
    with pytest.raises(compat.TokenCompatibilityError, match='startup refused'):
        compat._prepare(api, 22)
    assert len(api.writes) == 2 and api.child_dacl == OBSERVED
    assert api.closed == [103, 102, 101]


def test_rollback_failure_is_fatal_so_job_owner_can_stop_gated_child():
    api = Api()
    api.fail_verification = api.fail_restore = True
    with pytest.raises(compat.TokenCompatibilityError, match='restoration failed'):
        compat._prepare(api, 22)
    assert api.closed == [103, 102, 101]


def test_token_replaced_between_read_and_write_is_not_modified():
    api = Api()
    state = api.state
    api.state = lambda token: dict(state(token), id=999) if token == 103 else state(token)
    with pytest.raises(compat.TokenCompatibilityError, match='token changed'):
        compat._prepare(api, 22)
    assert not api.writes


def test_disabled_rollback_switch_never_opens_a_token(monkeypatch):
    monkeypatch.setenv('DLSS5TOOL_NVIDIA_TOKEN_COMPAT', 'off')
    with mock.patch.object(compat, '_Windows') as api:
        assert compat.prepare_worker_token(22)['status'] in ('disabled', 'unsupported')
    api.assert_not_called()


@pytest.mark.skipif(os.name != 'nt', reason='Windows job objects')
@pytest.mark.parametrize('fails', [False, True])
def test_compatibility_runs_after_job_assignment_and_failure_closes_job(fails):
    from dlss5tool import process_job as jobs
    kernel = mock.Mock()
    kernel.CreateJobObjectW.return_value = 123
    kernel.SetInformationJobObject.return_value = kernel.AssignProcessToJobObject.return_value = 1
    kernel.CloseHandle.return_value = 1
    def prepare(handle):
        kernel.AssignProcessToJobObject.assert_called_once_with(123, 987)
        if fails:
            raise compat.TokenCompatibilityError('startup refused')
        return dict(status='applied', permission_mask=compat.READ_CONTROL)
    with mock.patch.object(jobs.C, 'WinDLL', return_value=kernel), \
            mock.patch.object(compat, 'prepare_worker_token', side_effect=prepare):
        if fails:
            with pytest.raises(compat.TokenCompatibilityError):
                jobs.KillOnCloseJob.from_handle(987, nvidia_compat=True)
            kernel.CloseHandle.assert_called_once_with(123)
        else:
            job = jobs.KillOnCloseJob.from_handle(987, nvidia_compat=True)
            assert job.token_compatibility['status'] == 'applied'
            job.close()
