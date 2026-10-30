# single_instance.py
# Ensures only one instance of application runs at a time.
#
# How it works:
#   1. On startup, try to create a named Windows Mutex.
#   2. If mutex already exists → another instance is running.
#   3. Find that instance's window via EnumWindows and bring it to front.
#   4. Then exit immediately.
#
# The mutex is automatically released by Windows when the process exits,
# even on crash – no cleanup needed.

import ctypes
import ctypes.wintypes
import sys

# Must match the app's main window title (or a unique prefix of it)
APP_WINDOW_TITLE = "SSH 磁盘管理器"

# The IPC pipe is version-independent and machine-wide. Use the same lifetime
# boundary for all versions/sessions of a Windows user, never one global lock
# shared by different users. Resolve the SID from the token, not environment
# variables or the (possibly Administrators-group) default token owner.
MUTEX_NAME = r"Global\SSHDriveMgr_SingleInstance_{A3F2B1C4-9E7D-4A8F-B3C2-1D5E6F7A8B9C}"
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", ctypes.wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.wintypes.LPVOID),
                ("bInheritHandle", ctypes.wintypes.BOOL)]


def _current_user_sid() -> str:
    import win32api
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 0x0008)  # TOKEN_QUERY
    try:
        sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()


def _mutex_apis():
    """Explicit pointer-sized HANDLE signatures, including on 64-bit Windows."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexExW.argtypes = [ctypes.POINTER(_SECURITY_ATTRIBUTES),
                                      ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                                      ctypes.wintypes.DWORD]
    kernel32.CreateMutexExW.restype = ctypes.wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                  ctypes.wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = ctypes.wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
    return kernel32


def _mutex_security(sid: str):
    """Only this user may synchronize; no write, privileges, or low-IL label.

    A medium mandatory label and SYNCHRONIZE-only opens work with either UAC
    token. Default elevated DACLs/CreateMutexW's MUTEX_ALL_ACCESS do not.
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD,
                        ctypes.POINTER(ctypes.wintypes.LPVOID),
                        ctypes.POINTER(ctypes.wintypes.ULONG)]
    convert.restype = ctypes.wintypes.BOOL
    descriptor = ctypes.wintypes.LPVOID()
    sddl = f"D:P(A;;0x00100000;;;{sid})S:(ML;;NW;;;ME)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    if not descriptor.value:
        raise OSError("Mutex security descriptor is NULL")
    attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), descriptor, False)
    return attributes, descriptor


def _find_and_focus_existing_window() -> bool:
    """
    Enumerate all top-level windows, find the one belonging to the
    already-running instance, restore it if minimized, and bring it
    to the foreground.
    Returns True if the window was found and focused.
    """
    user32 = ctypes.windll.user32

    found_hwnd = ctypes.wintypes.HWND(0)
    hidden_hwnd = ctypes.wintypes.HWND(0)

    # EnumWindows callback signature: BOOL CALLBACK(HWND hwnd, LPARAM lParam)
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _enum_callback(hwnd, lparam):
        # Get window title
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True  # continue enumeration

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        if APP_WINDOW_TITLE.lower() in title.lower():
            if user32.IsWindowVisible(hwnd):
                found_hwnd.value = hwnd
                return False  # stop enumeration
            if not hidden_hwnd.value:
                hidden_hwnd.value = hwnd
        return True  # continue

    callback = WNDENUMPROC(_enum_callback)
    user32.EnumWindows(callback, 0)

    hwnd = found_hwnd.value or hidden_hwnd.value

    if hwnd:
        SW_RESTORE = 9
        SW_SHOW = 5
        # Restore if minimized
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)
        # Bring to foreground (works even if window is behind others)
        user32.SetForegroundWindow(hwnd)
        # Flash taskbar button to draw attention
        user32.FlashWindow(hwnd, True)
        return True

    return False


def ensure_single_instance() -> None:
    """
    Call this at the very start of main(), before creating QApplication.

    If another instance is already running:
      - Focuses that instance's window
      - Exits this process immediately with code 0

    If this is the first instance:
      - Creates the mutex and returns normally
      - The mutex lives for the entire process lifetime
    """
    global _mutex_handle
    if _mutex_handle:
        return
    kernel32 = _mutex_apis()
    sid = _current_user_sid()
    mutex_name = f"{MUTEX_NAME}_{sid}"
    attributes, descriptor = _mutex_security(sid)
    try:
        # Do not request ownership or MUTEX_ALL_ACCESS: existence, not waiting
        # or mutation, implements this lock. Both UAC tokens can open it.
        ctypes.set_last_error(0)
        mutex = kernel32.CreateMutexExW(ctypes.byref(attributes), mutex_name, 0, _SYNCHRONIZE)
        last_error = ctypes.get_last_error()
    finally:
        kernel32.LocalFree(descriptor)

    if not mutex:
        if last_error != _ERROR_ACCESS_DENIED:
            raise ctypes.WinError(last_error)
        # An elevated/legacy mutex can deny creation's requested access. A
        # read-only probe may prove existence; denial is NEVER proof of being
        # the first instance. If the probe fails, fail closed with its error.
        mutex = kernel32.OpenMutexW(_SYNCHRONIZE, False, mutex_name)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        last_error = _ERROR_ALREADY_EXISTS

    if last_error == _ERROR_ALREADY_EXISTS:
        if not kernel32.CloseHandle(mutex):
            raise ctypes.WinError(ctypes.get_last_error())
        # Across sessions Windows cannot focus a window on another desktop;
        # still exit safely rather than start another shared IPC listener.
        _find_and_focus_existing_window()
        sys.exit(0)

    _mutex_handle = mutex

# Module-level handle – must stay alive for the entire process lifetime
_mutex_handle = None


def release_instance_lock() -> None:
    """Release the mutex so a new instance can start (e.g. for restart)."""
    global _mutex_handle
    if _mutex_handle:
        # No ReleaseMutex: the existence handle never owns the mutex.
        if not _mutex_apis().CloseHandle(_mutex_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        _mutex_handle = None
