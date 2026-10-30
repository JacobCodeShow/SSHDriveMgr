"""Windows NORMAL frozen-entry gate: fresh private profile, visible Login required.

CLI: python scripts/verify_gui_startup.py [--onedir] [--timeout 45]
API: verify_startup(onedir=False, timeout=45) -> result dict (raises on failure).
Requires source build dependencies psutil, pytest and pywin32, not GUI Python on
end-user machines. Never logs in, copies a database, or invokes helper modes.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import psutil
from tests.conftest import SessionIsolation


class StartupError(RuntimeError):
    pass


def launch_entry(onedir=False, *, repository=REPO):
    """Only fixed dist entries are allowed; never accept an intermediate stub."""
    entry = repository / "dist" / ("SSHDriveMgr-onedir/SSHDriveMgr.exe" if onedir else "SSHDriveMgr.exe")
    if not entry.is_file():
        mode = "onedir" if onedir else "gui"
        raise StartupError(f"Missing published executable: {entry}; run build_dual.ps1 {mode} first.")
    if onedir:
        runtime = entry.parent / "_internal"
        dlls = [p for p in runtime.glob("python3*.dll")
                if re.fullmatch(r"python3\d{2,}\.dll", p.name, re.IGNORECASE) and p.is_file()]
        if not (runtime / "base_library.zip").is_file() or not dlls:
            raise StartupError(f"Incomplete OneDir runtime: {runtime}; keep the entire dist folder.")
    return entry


def login_titles(repository=REPO):
    # Read labels only; do not import production initialization/auth/database code.
    titles = set()
    for path in (repository / "src/translations").glob("*.json"):
        labels = json.loads(path.read_text(encoding="utf-8"))
        titles.add(labels["login.title"])
        # Qt/Windows may append applicationDisplayName to the native caption.
        titles.add(f"{labels['login.title']} - {labels['app.title']}")
    return titles


class NativeWindows:
    """EnumWindows with pointer-sized native signatures and strict PID filtering."""

    def __init__(self):
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
            "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            "EnumWindows": ([self.callback_type, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def windows_for(self, pids):
        found = []

        def visit(hwnd, _):
            pid = wintypes.DWORD()
            self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                title = ctypes.create_unicode_buffer(self.api.GetWindowTextLengthW(hwnd) + 1)
                self.api.GetWindowTextW(hwnd, title, len(title))
                kind = ctypes.create_unicode_buffer(256)
                self.api.GetClassNameW(hwnd, kind, len(kind))
                found.append({"pid": pid.value, "hwnd": int(hwnd), "title": title.value,
                              "class": kind.value, "visible": bool(self.api.IsWindowVisible(hwnd))})
            return True

        callback = self.callback_type(visit)
        if not self.api.EnumWindows(callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return found

    def close_login(self, windows, pids, titles):
        # Re-enumerate to recheck PID/title; no broadcast or global app matching.
        handles = {w["hwnd"] for w in windows if is_login(w, titles)}
        for window in self.windows_for(pids):
            if window["hwnd"] in handles and is_login(window, titles):
                self.api.PostMessageW(window["hwnd"], 0x0010, 0, 0)  # WM_CLOSE


def is_login(window, titles):
    return (window["visible"] and window["title"] in titles
            and window.get("class", "").startswith("Qt"))


class OwnedTree:
    """Retain psutil identity-bearing references, never kill by executable name."""

    def __init__(self, pid):
        self.root = psutil.Process(pid)
        self.root.create_time()
        self.processes = {pid: self.root}

    def refresh(self):
        # Descendants may outlive their root (onefile bootloader); retain identities.
        for process in list(self.processes.values()):
            try:
                if process.is_running():
                    for child in process.children(recursive=True):
                        child.create_time()
                        self.processes.setdefault(child.pid, child)
            except psutil.Error:
                pass
        return {p.pid for p in self.processes.values() if p.is_running()}

    def kill_remaining(self):
        self.refresh()
        for process in reversed(list(self.processes.values())):
            try:
                if process.is_running():
                    process.kill()  # psutil checks identity/PID reuse before kill
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(list(self.processes.values()), timeout=5)
        if alive:
            raise StartupError(f"Private startup processes did not stop: {[p.pid for p in alive]}")


def wait_for_login(process, tree, native, titles, timeout, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    first_login = None
    observed = []
    while clock() < deadline:
        current = native.windows_for(tree.refresh())
        if current:
            observed = current
        if process.poll() is not None:
            raise StartupError(f"NORMAL entry exited early (exit {process.returncode}); windows={observed!r}")
        unexpected = [w for w in current if w["visible"] and w["title"] and not is_login(w, titles)]
        if unexpected:
            raise StartupError(f"Unexpected/error startup window: {unexpected!r}")
        if any(is_login(w, titles) for w in current):
            if first_login is None:
                first_login = clock()
            elif clock() - first_login >= 1.0:
                return current
        else:
            first_login = None
        sleep(0.25)
    raise StartupError(f"No stable visible Login within {timeout:g}s; windows={observed!r}")


def private_environment(state):
    env = os.environ.copy()
    env["APPDATA"] = str(state.root / "appdata")
    env["TEMP"] = env["TMP"] = str(state.root / "tmp")
    env["PYTHONIOENCODING"] = "utf-8"
    # Do not silently accept an offscreen/minimal Qt no-window launch as success.
    env["QT_QPA_PLATFORM"] = "windows"
    env.pop("SSHNO_EARLY_WEBENGINE", None)
    return env


def verify_startup(onedir=False, timeout=45):
    if sys.platform != "win32":
        raise StartupError("Native Windows desktop required; use build -SkipStartupCheck explicitly in headless CI.")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    folder = REPO / "build" / f"startup-check-{time.time_ns()}"
    folder.mkdir(parents=True)
    result = {"success": False, "mode": "onedir" if onedir else "onefile", "log_folder": str(folder)}
    state = process = tree = native = None
    titles = set()
    windows = []
    try:
        entry = launch_entry(onedir)
        titles = login_titles()
        native = NativeWindows()
        state = SessionIsolation.create()
        result["private_profile"] = str(state.root)
        result["command"] = [str(entry)]  # NORMAL path, deliberately no arguments
        with (folder / "stdout.log").open("wb") as out, (folder / "stderr.log").open("wb") as err:
            try:
                process = subprocess.Popen([str(entry)], cwd=REPO, env=private_environment(state),
                                           stdout=out, stderr=err)
                result["pid"] = process.pid
                tree = OwnedTree(process.pid)
                windows = wait_for_login(process, tree, native, titles, timeout)
                result["windows"] = windows
                result["success"] = True
            finally:
                try:
                    if tree is not None:
                        native.close_login(windows, tree.refresh(), titles)
                    if process is not None:
                        try:
                            process.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    # Even a failed WM_CLOSE/enumeration must not leak our child.
                    if tree is not None:
                        tree.kill_remaining()
                    elif process is not None and process.poll() is None:
                        process.kill()  # only this Popen-owned child, never a global process
                        process.wait(timeout=5)
    except Exception as error:
        result["success"] = False
        result["error"] = str(error)
        (folder / "verifier-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        if state is not None:
            state.restore()
            if not state.cleanup():
                result["success"] = False
                result["cleanup_error"] = state.cleanup_error
        (folder / "result.json").write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    if not result["success"]:
        raise StartupError(f"GUI startup check FAILED: {result.get('error', result.get('cleanup_error'))}. Logs: {folder}")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", action="store_true", help="Check the complete dist OneDir entry instead of onefile")
    parser.add_argument("--timeout", type=float, default=45, help="Visible Login deadline in seconds (default: 45)")
    args = parser.parse_args(argv)
    try:
        result = verify_startup(args.onedir, args.timeout)
    except (StartupError, ValueError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    print(f"NORMAL GUI startup PASS ({result['mode']}): visible private Login. Logs: {result['log_folder']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
