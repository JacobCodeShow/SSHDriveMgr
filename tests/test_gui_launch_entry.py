"""Launcher/gate coverage without auth, live SSH, existing profiles or rebuilds."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from scripts import verify_gui_startup as gate

ROOT = Path(__file__).resolve().parents[1]
TITLE = "SSH test - Login"
LOGIN = {"pid": 101, "hwnd": 500, "title": TITLE, "visible": True, "class": "Qt691QWindowIcon"}


def test_entry_rejects_build_intermediate_and_incomplete_onedir(tmp_path):
    stub = tmp_path / "build/SSHDriveMgr-onedir/SSHDriveMgr.exe"
    stub.parent.mkdir(parents=True)
    stub.touch()
    with pytest.raises(gate.StartupError, match="Missing published"):
        gate.launch_entry(True, repository=tmp_path)
    entry = tmp_path / "dist/SSHDriveMgr-onedir/SSHDriveMgr.exe"
    entry.parent.mkdir(parents=True)
    entry.touch()
    with pytest.raises(gate.StartupError, match="Incomplete"):
        gate.launch_entry(True, repository=tmp_path)
    internal = entry.parent / "_internal"
    internal.mkdir()
    (internal / "base_library.zip").touch()
    (internal / "python3.dll").touch()
    with pytest.raises(gate.StartupError, match="Incomplete"):
        gate.launch_entry(True, repository=tmp_path)
    for name in ("python311.dll", "python312.dll", "python313.dll", "python314.dll"):
        dll = internal / name
        dll.touch()
        assert gate.launch_entry(True, repository=tmp_path) == entry
        dll.unlink()


def test_exact_translated_login_and_qt_native_display_name_variant_are_accepted():
    titles = gate.login_titles()
    labels = json.loads((ROOT / "src/translations/en.json").read_text(encoding="utf-8"))
    title = labels["login.title"]
    native_title = title + " - " + labels["app.title"]
    assert title in titles
    assert native_title in titles
    assert gate.is_login({**LOGIN, "title": native_title}, titles)
    assert not gate.is_login({**LOGIN, "title": title + " - ERROR"}, titles)


def test_owned_tree_kills_only_identity_references_from_private_child(monkeypatch):
    killed = []

    class Process:
        def __init__(self, pid, children=()):
            self.pid = pid
            self.descendants = children
            self.alive = True

        def create_time(self):
            return self.pid

        def is_running(self):
            return self.alive

        def children(self, recursive):
            assert recursive
            return list(self.descendants)

        def kill(self):
            killed.append(self.pid)
            self.alive = False

    child = Process(102)
    root = Process(101, [child])
    foreign = Process(999)
    monkeypatch.setattr(gate.psutil, "Process", lambda pid: root if pid == 101 else foreign)
    monkeypatch.setattr(gate.psutil, "wait_procs", lambda processes, timeout: (list(processes), []))
    tree = gate.OwnedTree(101)
    assert tree.refresh() == {101, 102}
    tree.kill_remaining()
    assert killed == [102, 101]
    assert foreign.alive


def test_default_entry_is_standard_dist_not_newest_intermediate(tmp_path):
    dist = tmp_path / "dist/SSHDriveMgr.exe"
    dist.parent.mkdir()
    dist.touch()
    assert gate.launch_entry(repository=tmp_path) == dist


def test_ascii_launcher_has_fixed_paths_no_python_or_argument_forwarding():
    text = (ROOT / "Start-SSHDriveMgr.cmd").read_bytes().decode("ascii")
    assert "setlocal DisableDelayedExpansion" in text
    assert 'set "APP=%~dp0dist\\SSHDriveMgr.exe"' in text
    assert 'set "APP=%~dp0dist\\SSHDriveMgr-onedir\\SSHDriveMgr.exe"' in text
    assert 'base_library.zip' in text and 'python3*.dll' in text
    assert 'python3[0-9][0-9][0-9]*[.]dll' in text
    assert "%*" not in text and "--pass-helper" not in text
    assert "build\\SSHDriveMgr" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cmd launcher")
@pytest.mark.parametrize("arguments,files,code,message", [
    ([], [], 2, "Missing published build"),
    (["onedir"], [], 2, "Missing published build"),
    (["onedir"], ["dist/SSHDriveMgr-onedir/SSHDriveMgr.exe"], 3, "Incomplete OneDir"),
    (["onedir"], ["dist/SSHDriveMgr-onedir/SSHDriveMgr.exe", "dist/SSHDriveMgr-onedir/_internal/base_library.zip"], 3, "Incomplete OneDir"),
    (["onedir"], ["dist/SSHDriveMgr-onedir/SSHDriveMgr.exe", "dist/SSHDriveMgr-onedir/_internal/base_library.zip", "dist/SSHDriveMgr-onedir/_internal/python3.dll"], 3, "Incomplete OneDir"),
    (["nonsense"], [], 2, "Usage:"),
    (["onedir", "extra"], [], 2, "Usage:"),
])
def test_missing_launcher_outputs_fail_without_executing_build(tmp_path, arguments, files, code, message):
    # Spaces/! exercise quoted paths with delayed expansion disabled.
    project = tmp_path / "private launch ! folder"
    project.mkdir()
    launcher = project / "Start-SSHDriveMgr.cmd"
    shutil.copyfile(ROOT / launcher.name, launcher)
    for name in ["build/SSHDriveMgr-onedir/SSHDriveMgr.exe", *files]:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not an executable; must never be invoked")
    # Pass a native command line: list2cmdline escaping is not cmd.exe quoting.
    command = 'cmd.exe /d /s /c ""' + str(launcher) + '" ' + " ".join(arguments) + '"'
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stderr
    assert not result.stdout


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def probe(windows, *, exited=False, timeout=3):
    clock = Clock()
    process = SimpleNamespace(poll=lambda: 7 if exited else None, returncode=7)
    native = SimpleNamespace(windows_for=lambda pids: windows)
    tree = SimpleNamespace(refresh=lambda: {101})
    return gate.wait_for_login(process, tree, native, {TITLE}, timeout, clock=clock, sleep=clock.sleep)


def test_normal_gate_requires_stable_visible_expected_qt_login():
    assert probe([LOGIN]) == [LOGIN]
    for window in ({**LOGIN, "visible": False}, {**LOGIN, "title": ""}):
        with pytest.raises(gate.StartupError, match="No stable visible Login"):
            probe([window])
    with pytest.raises(gate.StartupError, match="No stable visible Login"):
        probe([])


def test_normal_gate_rejects_error_popup_even_alongside_login_and_early_exit():
    popup = {**LOGIN, "hwnd": 501, "title": "Failed to execute script", "class": "#32770"}
    with pytest.raises(gate.StartupError, match="Unexpected/error"):
        probe([LOGIN, popup])
    with pytest.raises(gate.StartupError, match="exited early.*7"):
        probe([LOGIN], exited=True)
    with pytest.raises(gate.StartupError, match="Unexpected/error"):
        probe([{**LOGIN, "class": "#32770"}])


def test_transient_login_does_not_pass():
    clock = Clock()
    native = SimpleNamespace(windows_for=lambda pids: [LOGIN] if clock.now < .5 else [])
    with pytest.raises(gate.StartupError, match="No stable"):
        gate.wait_for_login(SimpleNamespace(poll=lambda: None), SimpleNamespace(refresh=lambda: {101}),
                            native, {TITLE}, 2, clock=clock, sleep=clock.sleep)


def test_private_env_forces_native_desktop_and_private_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("SSHNO_EARLY_WEBENGINE", "1")
    env = gate.private_environment(SimpleNamespace(root=tmp_path))
    assert env["APPDATA"] == str(tmp_path / "appdata")
    assert env["TMP"] == env["TEMP"] == str(tmp_path / "tmp")
    assert env["QT_QPA_PLATFORM"] == "windows"
    assert "SSHNO_EARLY_WEBENGINE" not in env


def test_close_login_rechecks_ownership_and_never_closes_error_or_foreign_window():
    sent = []
    native = object.__new__(gate.NativeWindows)
    native.api = SimpleNamespace(PostMessageW=lambda *args: sent.append(args))
    native.windows_for = lambda pids: [LOGIN, {**LOGIN, "hwnd": 501, "title": "Error"}]
    native.close_login([LOGIN, {**LOGIN, "hwnd": 501, "title": "Error"}], {101}, {TITLE})
    assert sent == [(500, 0x0010, 0, 0)]
    sent.clear()
    native.windows_for = lambda pids: []  # HWND no longer belongs to private tree
    native.close_login([LOGIN], {101}, {TITLE})
    assert sent == []


@pytest.mark.parametrize("failure", [False, True])
def test_verifier_normal_command_logs_isolation_and_owned_cleanup(monkeypatch, tmp_path, failure):
    calls = []
    profile = tmp_path / "private-profile"
    profile.mkdir()
    state = SimpleNamespace(root=profile, restore=lambda: calls.append("restore"),
                            cleanup=lambda: calls.append("cleanup") or True)
    process = SimpleNamespace(pid=101, wait=lambda timeout: calls.append("wait"), poll=lambda: None)
    tree = SimpleNamespace(refresh=lambda: {101}, kill_remaining=lambda: calls.append("kill-owned"))
    native = SimpleNamespace(close_login=lambda *args: calls.append("close-login"))
    entry = tmp_path / "dist/SSHDriveMgr-onedir/SSHDriveMgr.exe"
    monkeypatch.setattr(gate, "REPO", tmp_path)
    monkeypatch.setattr(gate.sys, "platform", "win32")
    monkeypatch.setattr(gate, "launch_entry", lambda onedir: entry)
    monkeypatch.setattr(gate, "login_titles", lambda: {TITLE})
    monkeypatch.setattr(gate, "NativeWindows", lambda: native)
    monkeypatch.setattr(gate.SessionIsolation, "create", lambda: state)
    monkeypatch.setattr(gate, "OwnedTree", lambda pid: tree)

    def popen(command, **kwargs):
        assert command == [str(entry)]
        assert kwargs["env"]["APPDATA"] == str(profile / "appdata")
        assert kwargs["stdout"].name.endswith("stdout.log")
        assert kwargs["stderr"].name.endswith("stderr.log")
        return process

    monkeypatch.setattr(gate.subprocess, "Popen", popen)

    def wait(*args):
        if failure:
            raise gate.StartupError("test early exit")
        return [LOGIN]

    monkeypatch.setattr(gate, "wait_for_login", wait)
    if failure:
        with pytest.raises(gate.StartupError, match="FAILED.*Logs:"):
            gate.verify_startup(True)
    else:
        assert gate.verify_startup(True)["success"]
    assert calls == ["close-login", "wait", "kill-owned", "restore", "cleanup"]
    logs = list((tmp_path / "build").glob("startup-check-*/result.json"))
    assert len(logs) == 1
    assert (logs[0].parent / "stdout.log").is_file()
    assert (logs[0].parent / "stderr.log").is_file()


def test_build_default_gate_explicit_skip_and_interpreter_are_visible():
    source = (ROOT / "build_dual.ps1").read_text(encoding="utf-8")
    assert "[switch]$SkipStartupCheck" in source
    assert "if ($SkipStartupCheck)" in source
    assert "scripts/verify_gui_startup.py" in source
    assert "if ($plan.Mode -eq 'onedir') { $startupArgs += '--onedir' }" in source
    assert "& $python @startupArgs" in source
    assert '$LASTEXITCODE -ne 0' in source
    assert "Get-Command $pyinstaller" in source
    assert "NOT runnable releases" in source
    assert "Start-SSHDriveMgr.cmd" in source
    assert source.index("NORMAL GUI startup check failed") < source.index('Write-Host "Build complete')
    assert "--pass-helper" not in source


def test_cli_returns_nonzero_and_never_claims_success_on_failure(monkeypatch, capsys):
    def fail(*args):
        raise gate.StartupError("No stable visible Login; Logs: private")
    monkeypatch.setattr(gate, "verify_startup", fail)
    assert gate.main(["--onedir"]) == 1
    captured = capsys.readouterr()
    assert "No stable visible Login" in captured.err
    assert "PASS" not in captured.out
