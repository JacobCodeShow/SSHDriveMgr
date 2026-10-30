"""Offline fourth-batch regressions: mock handovers; parse, never run, build/release code."""
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from src import update_security as security
from tests.test_update_security import (DIGEST, NAME, PAYLOAD, VERSION, WORKSPACE,
                                  official_fixture, updater_namespace)


def test_every_release_archive_ships_the_version_resource_it_validates():
    root = Path(__file__).resolve().parents[1]
    for spec_name in ("SSHDriveMgr.spec", "SSHDriveMgr-cli.spec"):
        tree = ast.parse((root / spec_name).read_text(encoding="utf-8"))
        analysis = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == "Analysis")
        data = ast.literal_eval(next(k.value for k in analysis.keywords if k.arg == "datas"))
        assert ("src/version.txt", "src") in data, spec_name


def pending_fixture(root):
    official = official_fixture()
    payload = security.staging_path(root, VERSION, NAME, "installer")
    payload.write_bytes(PAYLOAD)
    record = dict(official, schema=1, installer_path=str(payload),
                  install_on_next_start=True, changelog="")
    return official, record


def registry_fixture(location, hive=1, view=64):
    registry = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
                               KEY_READ=128, KEY_WOW64_64KEY=64, KEY_WOW64_32KEY=32)
    key = mock.MagicMock()
    key.__enter__.return_value = key

    def open_key(actual_hive, name, reserved, access):
        assert name.endswith("{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1")
        assert reserved == 0
        assert access in (128 | 64, 128 | 32)  # read-only, no registry writes
        if actual_hive != hive or access != 128 | view:
            raise FileNotFoundError("no matching registration")
        return key

    registry.OpenKey = mock.Mock(side_effect=open_key)
    registry.QueryValueEx = mock.Mock(return_value=(str(location), 1))
    return registry


@pytest.mark.parametrize("hive,view", [(1, 64), (1, 32), (2, 64), (2, 32)])
def test_actual_registration_recognizes_custom_and_per_user_install(tmp_path, hive, view):
    namespace = updater_namespace(tmp_path, installed=None)
    app_dir = tmp_path / "custom-per-user-install"
    app_dir.mkdir()
    namespace["sys"].executable = str(app_dir / "SSHDriveMgr.exe")
    registry = registry_fixture(app_dir, hive, view)
    with mock.patch.dict(sys.modules, {"winreg": registry}):
        assert namespace["is_installed_copy"]() is True
    registry.QueryValueEx.assert_called_with(mock.ANY, "InstallLocation")


def test_programfiles_prefix_and_other_install_registration_are_not_evidence(tmp_path):
    namespace = updater_namespace(tmp_path, installed=None)
    registry = registry_fixture(tmp_path / "different-install")
    with mock.patch.dict(sys.modules, {"winreg": registry}), mock.patch.dict(
            os.environ, {"ProgramFiles": str(tmp_path), "ProgramFiles(x86)": str(tmp_path)}):
        assert namespace["is_installed_copy"]() is False
    namespace["sys"].frozen = False
    assert namespace["is_installed_copy"]() is False


def test_local_inno_marker_requires_matching_exe_and_data(tmp_path):
    namespace = updater_namespace(tmp_path, installed=None)
    registry = registry_fixture(tmp_path / "different-install")
    (tmp_path / "unins000.exe").write_bytes(b"not executed")
    with mock.patch.dict(sys.modules, {"winreg": registry}):
        assert namespace["is_installed_copy"]() is False
        (tmp_path / "unins000.dat").write_bytes(b"installation marker")
        assert namespace["is_installed_copy"]() is True


@pytest.mark.parametrize("bundle", [".", "_internal", "custom-contents"])
def test_onedir_metadata_prevents_onefile_handover_and_asset_selection(tmp_path, bundle):
    namespace = updater_namespace(tmp_path)
    namespace["sys"]._MEIPASS = str(tmp_path / bundle)
    namespace["is_installed_copy"] = lambda: False
    official, record = pending_fixture(tmp_path)
    assert namespace["is_onedir_copy"]() is True
    with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
            security, "verify_for_apply", return_value=(Path(record["installer_path"]), official)):
        assert namespace["launch_pending_installer"](record) is False
    namespace["subprocess"].Popen.assert_not_called()
    assert "onedir" in namespace["get_update_handover_error"]()
    manager = namespace["UpdaterManager"]("1.0.0")
    release = {"tag_name": "v" + VERSION, "assets": [{"name": "SSHDriveMgr.exe", "browser_download_url": "not-selected"}]}
    with mock.patch.object(security, "fetch_release", return_value=release):
        manager.check_for_updates_async()
    assert manager.update_available.emit.call_args.args[-1] == "browser"
    for extraction in (tmp_path / "elsewhere" / "_MEI12345", tmp_path / "_MEI12345"):
        namespace["sys"]._MEIPASS = str(extraction)
        assert namespace["is_onedir_copy"]() is False


@pytest.mark.parametrize("entrypoint", ["launch_pending_installer", "maybe_install_pending_update"])
def test_disarm_write_failure_aborts_handover_and_retains_specific_message(tmp_path, entrypoint):
    namespace = updater_namespace(tmp_path)
    official, record = pending_fixture(tmp_path)
    security.atomic_json(tmp_path / "pending_update.json", record)
    namespace["os"] = SimpleNamespace(name="nt", path=os.path, environ=os.environ)
    with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
            security, "resolve_release", return_value=official), mock.patch.object(
            security, "atomic_json", side_effect=OSError("disarm access denied")):
        argument = record if entrypoint == "launch_pending_installer" else "1.0.0"
        assert namespace[entrypoint](argument) is False
    namespace["subprocess"].Popen.assert_not_called()
    assert "disarm access denied" in namespace["get_update_handover_error"]()
    assert json.loads((tmp_path / "pending_update.json").read_text())["install_on_next_start"] is True


def test_successful_handover_disarms_before_spawn_and_cancel_cannot_loop(tmp_path):
    namespace = updater_namespace(tmp_path)
    official, record = pending_fixture(tmp_path)
    security.atomic_json(tmp_path / "pending_update.json", record)
    namespace["os"] = SimpleNamespace(name="nt", path=os.path, environ=os.environ)

    def spawn(*args, **kwargs):
        marker = json.loads((tmp_path / "pending_update.json").read_text())
        assert marker["install_on_next_start"] is False
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    namespace["subprocess"].Popen.side_effect = spawn
    with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
            security, "resolve_release", return_value=official):
        assert namespace["launch_pending_installer"](record, "1.0.0") is True
        # Setup cancel leaves the old version running; restart must not re-arm.
        assert namespace["maybe_install_pending_update"]("1.0.0") is False
    namespace["subprocess"].Popen.assert_called_once()
    assert record["install_on_next_start"] is True  # caller's snapshot unmodified
    script = (tmp_path / "run_update.cmd").read_text(encoding="utf-8")
    assert "if %_tries% GEQ 60 goto abort" in script
    assert "GEQ 60 goto apply" not in script
    assert script.index("set PYINSTALLER_RESET_ENVIRONMENT=1") < script.index('start "" "' + namespace["sys"].executable)
    abort = script.split(":abort\n", 1)[1]
    commands = [line.strip() for line in abort.splitlines() if line.strip() and not line.startswith("rem ")]
    assert commands == ["exit /b 1"]  # no hash, cleanup, copy, delete or restart after timeout
    assert script.index("Get-FileHash") < script.index('start "" /wait')
    assert DIGEST in script


def test_production_worker_result_is_queued_to_bound_qt_slot(tmp_path, monkeypatch):
    # Compile only updater/class definitions: importing app logger would write
    # AppData, and constructing an actual dialog is unnecessary for Qt affinity.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    namespace = updater_namespace(tmp_path)
    tree = ast.parse((WORKSPACE / "src/updater.py").read_text(encoding="utf-8"))
    manager_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "UpdaterManager")
    namespace.update(QObject=QObject, pyqtSignal=pyqtSignal, threading=threading)
    exec(compile(ast.Module(body=[manager_node], type_ignores=[]), "worker-test", "exec"), namespace)
    dialog_tree = ast.parse((WORKSPACE / "src/ui/dialogs/update_dialog.py").read_text(encoding="utf-8"))
    slot = next(node for node in ast.walk(dialog_tree) if isinstance(node, ast.FunctionDef) and node.name == "on_handover_finished")
    receiver_node = ast.ClassDef(name="Receiver", bases=[ast.Name(id="QObject", ctx=ast.Load())], keywords=[], body=[slot], decorator_list=[])
    slot_ns = {"QObject": QObject, "pyqtSlot": pyqtSlot}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[receiver_node], type_ignores=[])), "receiver-test", "exec"), slot_ns)
    receiver = slot_ns["Receiver"]()
    manager = namespace["UpdaterManager"]("1.0.0")
    manager.pending_update = lambda: {"version": VERSION}
    results, worker_threads = [], []

    def apply(record, from_version):
        worker_threads.append(QThread.currentThread())
        return False

    namespace["launch_pending_installer"] = apply
    namespace["get_update_handover_error"] = lambda: "specific worker verification failure"

    def completed(success, message):
        results.append((success, message, QThread.currentThread()))
        app.quit()

    receiver._handover_result_callback = completed
    manager.handover_finished.connect(receiver.on_handover_finished)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(app.quit)
    timer.start(5000)
    manager.launch_pending_update_async()
    app.exec()
    timer.stop()
    assert results == [(False, "specific worker verification failure", app.thread())]
    assert worker_threads and worker_threads[0] != app.thread()
    assert manager._is_handing_over is False


def test_dialog_uses_async_capability_and_disconnects_when_closed():
    from types import ModuleType
    from tests.test_update_dialog_policy import definition
    core, widgets = ModuleType("PyQt6.QtCore"), ModuleType("PyQt6.QtWidgets")
    core.QTimer = mock.Mock()
    widgets.QApplication = mock.Mock()
    policy = ModuleType("src.updater")
    policy.launch_pending_installer = mock.Mock()
    policy.set_install_on_next_start = mock.Mock(return_value=True)
    policy.get_update_handover_error = mock.Mock(return_value="must not replace worker message")
    messages = ModuleType("src.ui.dialogs.styled_message_box")
    messages.StyledMessageBox = mock.Mock()
    manager = mock.Mock(current_version="1.0.0", supports_async_handover=True)
    manager.pending_update.return_value = {"install_on_next_start": False}
    dialog = mock.Mock()
    with mock.patch.dict(sys.modules, {"PyQt6.QtCore": core, "PyQt6.QtWidgets": widgets,
                                     "src.updater": policy,
                                     "src.ui.dialogs.styled_message_box": messages}):
        definition("_wire")(dialog, manager)
        manager.handover_finished.connect.assert_called_once_with(dialog.on_handover_finished)
        dialog.install_now.connect.call_args.args[0]()
        manager.launch_pending_update_async.assert_called_once()
        policy.launch_pending_installer.assert_not_called()
        dialog.accept.assert_not_called()
        assert dialog._handover_busy is True
        dialog._handover_result_callback(False, "worker retained failure")
        assert dialog._handover_busy is False
        dialog.accept.assert_not_called()
        dialog.schedule_cb.setChecked.assert_called_with(False)
        assert messages.StyledMessageBox.warning.call_args.args[-1] == "worker retained failure"
        dialog.finished.connect.call_args.args[0](0)
        manager.handover_finished.disconnect.assert_called_once_with(dialog.on_handover_finished)
        assert dialog._handover_result_callback is None


def workflow_runs(path):
    """Extract the deliberately simple GHA run scalars for PowerShell parsing."""
    lines = path.read_text(encoding="utf-8").splitlines()
    runs = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("        run: |"):  # all blocks in these two workflows
            index += 1
            block = []
            while index < len(lines) and (lines[index].startswith("          ") or not lines[index]):
                block.append(lines[index][10:])
                index += 1
            runs.append("\n".join(block))
            continue
        if line.startswith("        run: "):
            runs.append(line.split("run: ", 1)[1])
        index += 1
    assert runs
    return runs


def test_powershell_scripts_and_every_workflow_run_parse_without_execution():
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell parser unavailable on this platform")
    scripts = [(WORKSPACE / name).read_text(encoding="utf-8") for name in (
        "build_dual.ps1", "installer/build_installer.ps1")]
    for name in ("manual-build.yml", "auto-release.yml"):
        scripts.extend(workflow_runs(WORKSPACE / ".github/workflows" / name))
    # ParseInput builds an AST only. No script, native build, installer, release
    # command or registry operation is executed by this test.
    parser = """$sources = [Console]::In.ReadToEnd() | ConvertFrom-Json
    foreach ($source in $sources) {
        $tokens = $null; $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseInput([string]$source, [ref]$tokens, [ref]$errors)
        if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }
    }
    """
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", parser],
                            input=json.dumps(scripts), text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    for script in scripts:
        for match in re.finditer(r"(?ms)^@'\n(.*?)\n'@ \| python -", script):
            ast.parse(match.group(1))  # embedded version-inspection Python, also never executed


def test_build_scripts_fail_on_native_exit_and_reject_stale_outputs():
    build = (WORKSPACE / "build_dual.ps1").read_text(encoding="utf-8")
    installer = (WORKSPACE / "installer/build_installer.ps1").read_text(encoding="utf-8")
    for text, invocation in ((build, "& $pyinstaller"), (installer, "& $iscc")):
        assert re.search(re.escape(invocation) + r"[^\n]*\n\s*if \(\$LASTEXITCODE -ne 0\) \{ throw", text)
        assert "LastWriteTimeUtc" in text
        assert "Length -le 0" in text
        assert "VersionInfo.ProductVersion" in text
        assert text.index("Remove-Item") < text.index(invocation)
    assert "Stop-Process" not in build
    assert "Stop-Process" not in installer


def test_manual_checkout_and_both_workflow_native_commands_are_checked():
    manual = (WORKSPACE / ".github/workflows/manual-build.yml").read_text(encoding="utf-8")
    assert "ref: refs/tags/${{ github.event.inputs.tag }}" in manual
    for name in ("manual-build.yml", "auto-release.yml"):
        path = WORKSPACE / ".github/workflows" / name
        text = path.read_text(encoding="utf-8")
        assert '$env:RELEASE_TAG -cne "v$version"' in text
        assert "CArchiveReader" in text and "archive.extract" in text
        assert "VersionInfo.ProductVersion" in text
        assert 'SSHDriveMgr-Setup-$version.exe' in text
        assert r'run: .\build_dual.ps1 all -SkipStartupCheck' in text
        assert r'run: .\installer\build_installer.ps1 -SkipBuild' in text
        assert text.index('- name: Install Inno Setup') < text.index('- name: Run tests')
        assert text.index('- name: Run tests') < text.index('- name: Build executables')
        for run in workflow_runs(path):
            lines = run.splitlines()
            for index, line in enumerate(lines):
                if re.match(r"\s*(?:python |choco |gh |\$\w+ = (?:git |gh ))", line) or "'@ | python -" in line:
                    assert "$LASTEXITCODE" in lines[index + 1], (name, line)


@pytest.mark.parametrize("parameters,valid", [
    ("param([string]$Mode, [switch]$SkipStartupCheck)", True),
    ("param([switch]$OneDir, [switch]$NoClean)", False),
    ("param([string]$Mode)", False),
    ("param([switch]$Mode, [switch]$SkipStartupCheck)", False),
])
def test_manual_tag_contract_is_checked_without_executing_old_builds(tmp_path, parameters, valid):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell parser unavailable")
    workflow = WORKSPACE / ".github/workflows/manual-build.yml"
    validate = workflow_runs(workflow)[0]
    contract = validate[validate.index("$interfaceError ="):]
    (tmp_path / "build_dual.ps1").write_text(parameters + '\nthrow "must not execute"', encoding="utf-8")
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer/build_installer.ps1").write_text(
        'param([switch]$SkipBuild)\nthrow "must not execute"', encoding="utf-8")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", contract],
                            cwd=tmp_path, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
    if valid:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
        assert "tag predates explicit-mode build interface" in result.stderr
    assert "must not execute" not in result.stdout + result.stderr


def test_auto_release_stays_draft_until_all_assets_and_checksums_are_verified():
    path = WORKSPACE / ".github/workflows/auto-release.yml"
    publish = workflow_runs(path)[-1]
    assert "--generate-notes --draft --verify-tag" in publish
    assert "Refusing to overwrite an already published release" in publish
    assert publish.count("gh release upload") == 2
    assert "throw \"Asset upload failed after retry:" in publish
    assert "'sha256sums.txt'" in publish
    assert publish.index("Remote asset hash mismatch") < publish.index("--draft=false")
    assert publish.index("gh release download") < publish.index("--draft=false")
    assert "throw 'Release publication failed'" in publish
    assert publish.count("--draft=false") == 1
