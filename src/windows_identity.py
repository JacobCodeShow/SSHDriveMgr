"""Qt/pywin32-free Windows identity and shared IPC naming.

TokenUser is stable for the same Windows account under UAC elevation and across
sessions. Never use environment usernames, token owner (which can instead be
the Administrators group), app version, or a shared fallback IPC endpoint.
"""
import ctypes
from ctypes import wintypes
import hashlib
import re


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


def _identity_apis():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    return kernel32, advapi32


def current_user_sid() -> str:
    """Return the canonical process TokenUser SID; fail closed on API errors.

    Opens only TOKEN_QUERY on the current process. No privileges are enabled,
    no credentials are accessed, and all allocated handles/buffers are freed.
    """
    kernel32, advapi32 = _identity_apis()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                    ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not token.value or token.value == wintypes.HANDLE(-1).value:
        raise OSError("OpenProcessToken returned an invalid token handle")
    try:
        length = wintypes.DWORD()
        # TokenUser = 1. This first call must report ERROR_INSUFFICIENT_BUFFER.
        ctypes.set_last_error(0)
        result = advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        error = ctypes.get_last_error()
        if result or error != 122:
            raise ctypes.WinError(error or 13)  # ERROR_INVALID_DATA
        if not ctypes.sizeof(_SID_AND_ATTRIBUTES) <= length.value <= 1024 * 1024:
            raise OSError("GetTokenInformation returned an invalid TokenUser size")
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not ctypes.sizeof(_SID_AND_ATTRIBUTES) <= length.value <= len(buffer):
            raise OSError("GetTokenInformation returned truncated TokenUser data")
        sid = _SID_AND_ATTRIBUTES.from_buffer(buffer).Sid
        if not sid or not advapi32.IsValidSid(sid):
            raise OSError("GetTokenInformation returned an invalid user SID")
        text_pointer = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text_pointer)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not text_pointer.value:
            raise OSError("ConvertSidToStringSidW returned NULL")
        try:
            text = ctypes.wstring_at(text_pointer.value)
            if not re.fullmatch(r"S-\d+(?:-\d+)+", text, flags=re.ASCII):
                raise OSError("ConvertSidToStringSidW returned an invalid SID string")
            return text
        finally:
            if kernel32.LocalFree(text_pointer):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if not kernel32.CloseHandle(token):
            raise ctypes.WinError(ctypes.get_last_error())


def ipc_pipe_name() -> str:
    """One version-independent IPC endpoint per Windows user, including UAC.

    SID hashing keeps the name compact and non-identifying; it is a namespace,
    not authentication. The server must still enforce its pipe DACL/protocol.
    Identity lookup failures propagate, never falling back to an all-user pipe.
    """
    suffix = hashlib.sha256(current_user_sid().encode("ascii")).hexdigest()
    return rf"\\.\pipe\SSHDriveMgr_IPC_v1_{suffix}"
