"""Narrow MessageBus compatibility for a gated, job-owned NVIDIA worker.

Only the observed restricted-token DACL is eligible. Add LocalSystem
READ_CONTROL to that token object, preserving every existing ACE byte/order.
No TokenDefaultDacl, identity, privilege, process DACL or machine setting changes.
The caller owns the process handle and must keep native startup gated until this
returns. A changed token object lives only with that disposable worker.
"""
from contextlib import ExitStack
import ctypes as C
import hashlib
import os
import struct


READ_CONTROL = 0x20000
TOKEN_QUERY = 8
TOKEN_ALL_ACCESS = 0xF01FF
_TOKEN_READ = READ_CONTROL | TOKEN_QUERY
_WRITE_DAC = 0x40000
_SYSTEM = b'\x01\x01\x00\x00\x00\x00\x00\x05' + struct.pack('<I', 18)
_OWNER_RIGHTS = b'\x01\x01\x00\x00\x00\x00\x00\x03' + struct.pack('<I', 4)
_ADMINISTRATORS = b'\x01\x02\x00\x00\x00\x00\x00\x05' + struct.pack('<II', 32, 544)


class TokenCompatibilityError(RuntimeError):
    """An eligible worker must not initialize GPU code after a failed change."""


def _aces(acl):
    if len(acl) < 8:
        raise ValueError('Truncated token ACL')
    revision, zero, size, count, reserved = struct.unpack_from('<BBHHH', acl)
    if revision not in (2, 4) or size != len(acl) or zero or reserved:
        raise ValueError('Unsupported token ACL header')
    offset, rows = 8, []
    for _ in range(count):
        if offset + 4 > size:
            raise ValueError('Truncated token ACE')
        kind, flags, length = struct.unpack_from('<BBH', acl, offset)
        if length < 4 or length % 4 or offset + length > size:
            raise ValueError('Invalid token ACE length')
        data = acl[offset:offset+length]
        rows.append((kind, flags, data))
        offset += length
    return rows, offset


def _sid(ace):
    if len(ace) < 16 or ace[8] != 1 or ace[9] > 15 or len(ace) != 16+4*ace[9]:
        raise ValueError('Unsupported simple token ACE')
    return ace[8:]


def _plan(acl, state):
    """Return an exact ACL candidate or a reason for leaving it unchanged."""
    if acl is None:
        return None, 'null_dacl_already_allows_access'
    rows, end = _aces(acl)
    # Unknown ACEs, deny ACEs and inherited/special flags are not rewritten.
    if not rows or any(kind != 0 or flags for kind, flags, _ in rows):
        return None, 'unrecognized_dacl'
    entries = [(_sid(raw), struct.unpack_from('<I', raw, 4)[0]) for _, _, raw in rows]
    if any(sid == _SYSTEM and mask & READ_CONTROL for sid, mask in entries):
        return None, 'system_read_control_already_present'
    if (not state['restricted'] or state['app_container'] or state['type'] != 1
            or state['integrity_rid'] != 8192):
        return None, 'unrecognized_token_context'
    if len(entries) != 3:
        return None, 'unrecognized_dacl'
    roles = []
    for sid, mask in entries:
        if sid == _OWNER_RIGHTS and mask == READ_CONTROL:
            roles.append('owner')
        elif sid == _ADMINISTRATORS and mask == TOKEN_QUERY:
            roles.append('administrators')
        elif (len(sid) == 20 and sid[:8] == b'\x01\x03\x00\x00\x00\x00\x00\x05'
              and struct.unpack_from('<I', sid, 8)[0] == 5 and mask == TOKEN_ALL_ACCESS):
            roles.append('logon')
        else:
            return None, 'unrecognized_dacl'
    if sorted(roles) != ['administrators', 'logon', 'owner']:
        return None, 'unrecognized_dacl'
    addition = struct.pack('<BBHI', 0, 0, 8+len(_SYSTEM), READ_CONTROL) + _SYSTEM
    if len(acl)+len(addition) > 65535:
        return None, 'acl_capacity'
    candidate = bytearray(acl[:end]+addition+acl[end:])
    struct.pack_into('<HH', candidate, 2, len(candidate), len(rows)+1)
    # Preserve old ACEs and unused tail bytes exactly; all eligible ACEs allow
    # access, so appending an explicit allow cannot jump ahead of a deny.
    after, new_end = _aces(candidate)
    if after[:-1] != rows or bytes(candidate[new_end:]) != acl[end:]:
        raise ValueError('Unexpected token ACL change')
    return bytes(candidate), 'restricted_messagebus_read_control'


class _Windows:
    def __init__(self):
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.a = C.WinDLL('advapi32', use_last_error=True)
        for owner, name, args, result in (
            (self.k, 'GetCurrentProcess', [], C.c_void_p),
            (self.k, 'GetProcessId', [C.c_void_p], C.c_uint32),
            (self.k, 'GetExitCodeProcess', [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int),
            (self.k, 'CloseHandle', [C.c_void_p], C.c_int),
            (self.k, 'LocalFree', [C.c_void_p], C.c_void_p),
            (self.a, 'OpenProcessToken', [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)], C.c_int),
            (self.a, 'GetTokenInformation', [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32,
                                           C.POINTER(C.c_uint32)], C.c_int),
            (self.a, 'IsTokenRestricted', [C.c_void_p], C.c_int),
            (self.a, 'IsValidAcl', [C.c_void_p], C.c_int),
            (self.a, 'GetLengthSid', [C.c_void_p], C.c_uint32),
            (self.a, 'GetSecurityInfo', [C.c_void_p, C.c_int, C.c_uint32, C.c_void_p,
                C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)], C.c_uint32),
            (self.a, 'SetSecurityInfo', [C.c_void_p, C.c_int, C.c_uint32, C.c_void_p,
                C.c_void_p, C.c_void_p, C.c_void_p], C.c_uint32),
        ):
            function = getattr(owner, name)
            function.argtypes, function.restype = args, result

    def pid(self, process):
        value = self.k.GetProcessId(process)
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def running(self, process):
        value = C.c_uint32()
        if not self.k.GetExitCodeProcess(process, C.byref(value)):
            raise C.WinError(C.get_last_error())
        return value.value == 259

    def open_token(self, process, access):
        value = C.c_void_p()
        if not self.a.OpenProcessToken(process, access, C.byref(value)):
            raise C.WinError(C.get_last_error())
        return value

    def info(self, token, kind):
        required = C.c_uint32()
        self.a.GetTokenInformation(token, kind, None, 0, C.byref(required))
        if not 0 < required.value <= 1024*1024:
            raise OSError('Unexpected token information size')
        buffer = C.create_string_buffer(required.value)
        if not self.a.GetTokenInformation(token, kind, buffer, len(buffer), C.byref(required)):
            raise C.WinError(C.get_last_error())
        return buffer

    def state(self, token):
        statistics = self.info(token, 10)
        label = self.info(token, 25)
        sid = C.cast(label, C.POINTER(C.c_void_p))[0]
        length = self.a.GetLengthSid(sid)
        if not 12 <= length <= 68:
            raise OSError('Unexpected integrity SID')
        integrity = C.string_at(sid, length)
        return dict(id=C.c_uint64.from_buffer(statistics).value,
            type=C.c_uint32.from_buffer(statistics, 24).value,
            restricted=bool(self.a.IsTokenRestricted(token)),
            app_container=bool(C.c_uint32.from_buffer(self.info(token, 29)).value),
            integrity_rid=struct.unpack_from('<I', integrity, length-4)[0])

    def dacl(self, token):
        acl, descriptor = C.c_void_p(), C.c_void_p()
        code = self.a.GetSecurityInfo(token, 6, 4, None, None, C.byref(acl), None, C.byref(descriptor))
        try:
            if code:
                raise C.WinError(code)
            if not acl:
                return None
            if not self.a.IsValidAcl(acl):
                raise OSError('Invalid token ACL')
            size = C.c_ushort.from_address(acl.value+2).value
            if not 8 <= size <= 65535:
                raise OSError('Unexpected token ACL size')
            return C.string_at(acl, size)
        finally:
            if descriptor:
                self.k.LocalFree(descriptor)

    def set_dacl(self, token, data):
        _aces(data)  # Never pass a null/invalid ACL to SetSecurityInfo.
        buffer = C.create_string_buffer(data)
        code = self.a.SetSecurityInfo(token, 6, 4, None, None, buffer, None)
        if code:
            raise C.WinError(code)

    def close(self, token):
        self.k.CloseHandle(token)


def _prepare(api, process_handle):
    result = dict(status='unchanged', scope='owned_worker_primary_token', permission_mask=0)
    digest = lambda value: hashlib.sha256(value).hexdigest() if value is not None else None
    with ExitStack() as cleanup:
        child_pid = api.pid(process_handle)
        result['worker_pid'] = child_pid
        if child_pid == os.getpid() or not api.running(process_handle):
            return dict(result, status='declined', reason='not_a_live_child')
        parent = api.open_token(api.k.GetCurrentProcess(), _TOKEN_READ)
        cleanup.callback(api.close, parent)
        child = api.open_token(process_handle, _TOKEN_READ)
        cleanup.callback(api.close, child)
        parent_state, child_state = api.state(parent), api.state(child)
        result.update(parent_token_id=parent_state['id'], worker_token_id=child_state['id'])
        if parent_state['id'] == child_state['id'] or child_state['type'] != 1:
            return dict(result, status='declined', reason='shared_or_non_primary_token')
        original = api.dacl(child)
        candidate, reason = _plan(original, child_state)
        result['reason'] = reason
        if candidate is None:
            return dict(result, status='unchanged' if reason.startswith(('system_', 'null_')) else 'declined')
        parent_dacl = api.dacl(parent)
        result['old_dacl_sha256'] = digest(original)
        try:
            writable = api.open_token(process_handle, _TOKEN_READ | _WRITE_DAC)
            cleanup.callback(api.close, writable)
            if api.state(writable) != child_state or api.dacl(writable) != original:
                raise TokenCompatibilityError('Worker token changed during compatibility preparation')
            try:
                api.set_dacl(writable, candidate)
                if api.dacl(writable) != candidate or api.state(writable) != child_state:
                    raise OSError('Worker token compatibility verification failed')
                if api.dacl(parent) != parent_dacl or api.state(parent) != parent_state:
                    raise OSError('Parent token changed during compatibility preparation')
            except Exception as failure:
                try:
                    api.set_dacl(writable, original)
                    if api.dacl(writable) != original:
                        raise OSError('Token ACL restoration verification failed')
                except Exception as restore:
                    raise TokenCompatibilityError('Worker token compatibility restoration failed; '
                                                  'GPU startup refused') from restore
                raise TokenCompatibilityError('Worker token compatibility failed; GPU startup refused') from failure
        except TokenCompatibilityError:
            raise
        except OSError as failure:
            raise TokenCompatibilityError('Worker token compatibility unavailable; GPU startup refused') from failure
        return dict(result, status='applied', permission_mask=READ_CONTROL,
                    new_dacl_sha256=digest(candidate), parent_token_unchanged=True)


def prepare_worker_token(process_handle):
    """Called once after Job assignment and before releasing native startup.

    Unrecognized security policies are left intact. A failed change on the
    recognized affected token aborts startup so a partially prepared worker
    cannot initialize GPU code. No token handle survives this function.
    """
    if os.name != 'nt':
        return dict(status='unsupported', reason='windows_only')
    if os.environ.get('DLSS5TOOL_NVIDIA_TOKEN_COMPAT', '').lower() in ('0', 'off'):
        return dict(status='disabled', reason='explicit_environment_override')
    try:
        return _prepare(_Windows(), process_handle)
    except TokenCompatibilityError:
        raise
    except (OSError, ValueError) as failure:
        # Metadata could not establish the narrowly supported case; no change
        # has been attempted here. Keep existing launch/pressure behavior.
        return dict(status='unavailable', reason=str(failure)[:240])
