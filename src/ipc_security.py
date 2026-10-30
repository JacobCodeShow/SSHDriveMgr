"""Fail-closed, current-process-user security attributes for Windows IPC.

Importing this module needs neither Windows nor pywin32. The native descriptor
is self-relative and copied into Python-owned storage; retain the returned
structure until the Windows API using it has returned.
"""

import ctypes
import os
import re


# Windows LONG/DWORD remain 32 bits even on hosts where C long is 64 bits.
_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_WORD = ctypes.c_uint16


class SECURITY_ATTRIBUTES(ctypes.Structure):
    """Windows SECURITY_ATTRIBUTES, including ownership of its descriptor."""

    _fields_ = [
        ("nLength", _DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", _BOOL),
    ]


def _current_user_sid():
    """Read TokenUser from the process token, never environment/user names."""
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid, _attributes = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )
        return win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()


def _validate_sid(sid):
    # Exclude SDDL aliases, injection, whitespace, unsupported revisions and
    # out-of-range authority/subauthority values before interpolating SDDL.
    if not isinstance(sid, str) or re.fullmatch(
        r"S-1-(?:[0-9]+|0x[0-9a-fA-F]+)(?:-[0-9]+){1,15}", sid
    ) is None:
        raise ValueError("Invalid current-process user SID")
    parts = sid.split("-")
    authority = parts[2]
    if int(authority, 16 if authority.startswith("0x") else 10) >= 1 << 48:
        raise ValueError("SID identifier authority is out of range")
    if any(int(part) >= 1 << 32 for part in parts[3:]):
        raise ValueError("SID subauthority is out of range")
    return sid


def _load_windows_libraries():
    if os.name != "nt":
        raise OSError("Windows pipe security is only available on Windows")
    return (
        ctypes.WinDLL("advapi32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )


def _api_error(operation):
    error = ctypes.get_last_error()
    return OSError(error, "{} failed: {}".format(operation, ctypes.FormatError(error)))


def _descriptor_from_sddl(sddl):
    """Convert SDDL, validate it, copy its bytes, and release LocalAlloc memory."""
    advapi32, kernel32 = _load_windows_libraries()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_wchar_p, _DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_DWORD),
    ]
    convert.restype = _BOOL
    is_valid = advapi32.IsValidSecurityDescriptor
    is_valid.argtypes = [ctypes.c_void_p]
    is_valid.restype = _BOOL
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WORD), ctypes.POINTER(_DWORD)]
    get_control.restype = _BOOL
    get_length = advapi32.GetSecurityDescriptorLength
    get_length.argtypes = [ctypes.c_void_p]
    get_length.restype = _DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    size = _DWORD()
    try:
        if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(size)):
            raise _api_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        if not descriptor.value or size.value < 20:
            raise ValueError("Windows returned an empty or truncated security descriptor")
        if not is_valid(descriptor):
            raise _api_error("IsValidSecurityDescriptor")
        control = _WORD()
        revision = _DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise _api_error("GetSecurityDescriptorControl")
        # SE_SELF_RELATIVE | SE_DACL_PROTECTED | SE_DACL_PRESENT
        required_control = 0x8000 | 0x1000 | 0x0004
        if revision.value != 1 or control.value & required_control != required_control:
            raise ValueError("Security descriptor must contain a protected self-relative DACL")
        if get_length(descriptor) != size.value:
            raise ValueError("Security descriptor length does not match allocation")
        return ctypes.string_at(descriptor, size.value)
    finally:
        if descriptor.value and local_free(descriptor):
            raise _api_error("LocalFree")


def make_pipe_security_attributes():
    """Return non-inheritable attributes granting only TokenUser generic R/W.

    Every identity, ACL, allocation, or native API failure raises. In particular,
    callers must NOT catch errors and create a pipe with default/NULL attributes.
    The returned object's ``_descriptor_buffer`` owns the descriptor for the
    entire structure lifetime; pass ``ctypes.byref(attributes)`` to native APIs.
    """
    sid = _validate_sid(_current_user_sid())
    descriptor = _descriptor_from_sddl("D:P(A;;GRGW;;;{})".format(sid))
    if not isinstance(descriptor, bytes) or len(descriptor) < 20:
        raise ValueError("Missing or truncated self-relative security descriptor")
    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    attributes._descriptor_buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
    attributes.lpSecurityDescriptor = ctypes.addressof(attributes._descriptor_buffer)
    attributes.bInheritHandle = False
    return attributes


__all__ = ["SECURITY_ATTRIBUTES", "make_pipe_security_attributes"]
