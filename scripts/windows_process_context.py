"""Read-only Windows context for interpreting native stability diagnostics.

Never changes tokens, ACLs, privileges or launch behavior. SID bytes are hashed;
credentials, group names and token contents are not emitted. Explicit ACE masks
are observations, not a complete effective-access calculation for a service.
"""
import ctypes as C
import hashlib
import os


def current_context():
    if os.name != 'nt':
        return dict(supported=False, reason='Windows diagnostic context only')
    kernel = C.WinDLL('kernel32', use_last_error=True)
    security = C.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = C.c_void_p
    kernel.CloseHandle.argtypes = [C.c_void_p]
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.ProcessIdToSessionId.argtypes = [C.c_uint32, C.POINTER(C.c_uint32)]
    kernel.GetProcessTimes.argtypes = [C.c_void_p] + [C.POINTER(C.c_uint64)] * 4
    security.OpenProcessToken.argtypes = [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)]
    security.GetTokenInformation.argtypes = [C.c_void_p, C.c_int, C.c_void_p,
        C.c_uint32, C.POINTER(C.c_uint32)]
    security.IsTokenRestricted.argtypes = [C.c_void_p]
    security.GetLengthSid.argtypes = [C.c_void_p]
    security.GetLengthSid.restype = C.c_uint32
    security.GetSidSubAuthorityCount.argtypes = [C.c_void_p]
    security.GetSidSubAuthorityCount.restype = C.POINTER(C.c_ubyte)
    security.GetSidSubAuthority.argtypes = [C.c_void_p, C.c_uint32]
    security.GetSidSubAuthority.restype = C.POINTER(C.c_uint32)
    security.GetSecurityInfo.argtypes = [C.c_void_p, C.c_int, C.c_uint32,
        C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)]
    security.GetAclInformation.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_int]
    security.GetAce.argtypes = [C.c_void_p, C.c_uint32, C.POINTER(C.c_void_p)]
    security.ConvertStringSidToSidW.argtypes = [C.c_wchar_p, C.POINTER(C.c_void_p)]
    security.EqualSid.argtypes = [C.c_void_p, C.c_void_p]
    process = kernel.GetCurrentProcess()
    token, system_sid = C.c_void_p(), C.c_void_p()
    result = dict(supported=True, pid=os.getpid(), read_only=True)

    def error():
        return C.WinError(C.get_last_error())

    def token_info(kind):
        required = C.c_uint32()
        security.GetTokenInformation(token, kind, None, 0, C.byref(required))
        if not 0 < required.value <= 1024 * 1024:
            raise RuntimeError('Unexpected token metadata length')
        buffer = C.create_string_buffer(required.value)
        if not security.GetTokenInformation(token, kind, buffer, len(buffer), C.byref(required)):
            raise error()
        return buffer

    def acl_info(handle):
        acl, descriptor = C.c_void_p(), C.c_void_p()
        code = security.GetSecurityInfo(handle, 6, 4, None, None, C.byref(acl), None, C.byref(descriptor))
        row = dict(query_error=code, explicit_system_allow_mask=0, explicit_system_deny_mask=0,
                   effective_access_calculated=False)
        try:
            if code or not acl:
                row['null_dacl'] = not code and not acl
                return row
            size = (C.c_uint32 * 3)()
            if not security.GetAclInformation(acl, size, C.sizeof(size), 2):
                row['query_error'] = C.get_last_error()
                return row
            row['ace_count'] = size[0]
            for index in range(size[0]):
                ace = C.c_void_p()
                if not security.GetAce(acl, index, C.byref(ace)):
                    row['query_error'] = C.get_last_error()
                    return row
                kind = C.c_ubyte.from_address(ace.value).value
                flags = C.c_ubyte.from_address(ace.value + 1).value
                if kind not in (0, 1) or flags & 8:  # Ignore inherit-only and non-simple ACEs.
                    continue
                if security.EqualSid(ace.value + 8, system_sid):
                    key = 'explicit_system_allow_mask' if kind == 0 else 'explicit_system_deny_mask'
                    row[key] |= C.c_uint32.from_address(ace.value + 4).value
            return row
        finally:
            if descriptor:
                kernel.LocalFree(descriptor)

    try:
        # TOKEN_QUERY plus READ_CONTROL; no token duplication/impersonation rights.
        if not security.OpenProcessToken(process, 0x20008, C.byref(token)):
            raise error()
        if not security.ConvertStringSidToSidW('S-1-5-18', C.byref(system_sid)):
            raise error()
        user = token_info(1)
        sid = C.cast(user, C.POINTER(C.c_void_p))[0]
        length = security.GetLengthSid(sid)
        if not 8 <= length <= 1024:
            raise RuntimeError('Unexpected SID metadata length')
        result['user_sid_sha256'] = hashlib.sha256(C.string_at(sid, length)).hexdigest()
        result['token_restricted'] = bool(security.IsTokenRestricted(token))
        result['primary_token_id'] = C.c_uint64.from_buffer(token_info(10)).value
        result['elevated'] = bool(C.c_uint32.from_buffer(token_info(20)).value)
        result['app_container'] = bool(C.c_uint32.from_buffer(token_info(29)).value)
        label = token_info(25)
        integrity_sid = C.cast(label, C.POINTER(C.c_void_p))[0]
        count = security.GetSidSubAuthorityCount(integrity_sid)[0]
        result['integrity_rid'] = security.GetSidSubAuthority(integrity_sid, count - 1)[0]
        session = C.c_uint32()
        if not kernel.ProcessIdToSessionId(os.getpid(), C.byref(session)):
            raise error()
        result['session_id'] = session.value
        created, exited, system_time, user_time = (C.c_uint64() for _ in range(4))
        if not kernel.GetProcessTimes(process, C.byref(created), C.byref(exited),
                                      C.byref(system_time), C.byref(user_time)):
            raise error()
        result['created_filetime'] = created.value
        result['process_dacl'] = acl_info(process)
        result['primary_token_dacl'] = acl_info(token)
        result['query_complete'] = not (result['process_dacl']['query_error'] or
                                         result['primary_token_dacl']['query_error'])
    except (OSError, RuntimeError) as failure:
        result.update(query_complete=False, error=str(failure))
    finally:
        if system_sid:
            kernel.LocalFree(system_sid)
        if token:
            kernel.CloseHandle(token)
    return result
