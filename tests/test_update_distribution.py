"""Offline distribution matching: never run installers or contact GitHub."""
from pathlib import Path
from unittest import mock

import pytest

from src import update_security as security
from tests.test_update_security import VERSION, NAME, updater_namespace, official_fixture
from tests.test_release_pipeline import pending_fixture
from tests.test_update_dialog_policy import definition

ZIP_NAME = f"SSHDriveMgr-onedir-{VERSION}.zip"


def release_fixture():
    tag = "v" + VERSION
    names = ["SSHDriveMgr-cli.exe", ZIP_NAME, NAME, "SSHDriveMgr.exe", "sha256sums.txt"]
    return {"tag_name": tag, "body": "fixture", "assets": [
        {"name": name, "browser_download_url": security.release_asset_url(tag, name)}
        for name in names
    ]}


def runtime(tmp_path, installed=False, onedir=False, frozen=True):
    namespace = updater_namespace(tmp_path, installed=installed)
    namespace["sys"].frozen = frozen
    namespace["sys"].executable = str(tmp_path / "renamed-gui.exe")
    namespace["sys"]._MEIPASS = str(tmp_path / "_internal" if onedir
                                   else tmp_path.parent / "_MEI12345")
    return namespace


@pytest.mark.parametrize("installed,onedir,frozen,kind,asset", [
    (True, False, True, "installer", NAME),
    (True, True, True, "installer", NAME),  # registration takes precedence
    (False, True, True, "onedir_zip", ZIP_NAME),
    (False, False, True, "portable_exe", "SSHDriveMgr.exe"),
    (False, False, False, "browser", ""),
])
def test_checker_selects_only_matching_distribution(tmp_path, installed, onedir, frozen, kind, asset):
    namespace = runtime(tmp_path, installed, onedir, frozen)
    manager = namespace["UpdaterManager"]("1.0.0")
    with mock.patch.object(security, "fetch_release", return_value=release_fixture()):
        manager.check_for_updates_async()
    manager.check_failed.emit.assert_not_called()
    args = manager.update_available.emit.call_args.args
    assert args[0] == VERSION and args[3] == kind
    assert manager._asset_name == asset
    expected_url = (security.release_asset_url("v" + VERSION, asset) if asset else
                    f"https://github.com/{security.REPOSITORY}/releases/tag/v{VERSION}")
    assert args[2] == expected_url


def test_onedir_zip_is_browser_only_and_never_armed_or_staged(tmp_path):
    namespace = runtime(tmp_path, onedir=True)
    manager = namespace["UpdaterManager"]("1.0.0")
    with mock.patch.object(security, "fetch_release", return_value=release_fixture()):
        manager.check_for_updates_async()
    with mock.patch.object(security, "resolve_release") as resolve:
        manager.download_update_async(security.release_asset_url("v" + VERSION, ZIP_NAME))
    resolve.assert_not_called()
    assert manager.download_finished.emit.call_args.args[0] is False
    assert not (tmp_path / "pending_update.json").exists()
    namespace["subprocess"].Popen.assert_not_called()
    with pytest.raises(security.UpdateSecurityError):
        security.checked_asset_name(VERSION, ZIP_NAME, "onedir_zip")


def test_missing_or_lookalike_zip_never_selects_exe_cli_or_setup(tmp_path):
    namespace = runtime(tmp_path, onedir=True)
    release = release_fixture()
    release["assets"] = [asset for asset in release["assets"] if asset["name"] != ZIP_NAME]
    release["assets"].append({"name": "SSHDriveMgr-onedir-0.0.1.zip", "browser_download_url": "wrong"})
    name, url, kind = namespace["matching_release_asset"](release, VERSION)
    assert name == "" and kind == "browser"
    assert url.endswith("/releases/tag/v" + VERSION)


@pytest.mark.parametrize("failure", ["duplicate", "foreign", "wrong_tag"])
def test_ambiguous_or_noncanonical_matching_asset_fails_closed(tmp_path, failure):
    namespace = runtime(tmp_path, onedir=True)
    release = release_fixture()
    asset = next(asset for asset in release["assets"] if asset["name"] == ZIP_NAME)
    if failure == "duplicate":
        release["assets"].append(dict(asset))
    elif failure == "foreign":
        asset["browser_download_url"] = "https://evil.example/" + ZIP_NAME
    else:
        release["tag_name"] = "v0.0.1"
    with pytest.raises(security.UpdateSecurityError):
        namespace["matching_release_asset"](release, VERSION)


@pytest.mark.parametrize("installed,onedir,record_kind", [
    (False, False, "installer"), (True, False, "portable_exe"),
    (False, True, "installer"), (False, True, "portable_exe"),
])
def test_foreign_pending_ignored_before_stale_cleanup(tmp_path, installed, onedir, record_kind):
    namespace = runtime(tmp_path, installed, onedir)
    record = {"asset_type": record_kind, "version": "0.1.0"}
    namespace["read_pending_update"] = mock.Mock(return_value=record)
    namespace["clear_pending_update"] = mock.Mock()
    assert namespace["get_pending_update"]("9.0.0") is None
    assert namespace["UpdaterManager"]("9.0.0").pending_update() is None
    namespace["clear_pending_update"].assert_not_called()


def test_foreign_pending_schedule_is_not_modified(tmp_path):
    namespace = runtime(tmp_path, installed=False)
    namespace["read_pending_update"] = mock.Mock(return_value={
        "asset_type": "installer", "install_on_next_start": True,
    })
    with mock.patch.object(security, "atomic_json") as write:
        assert namespace["set_install_on_next_start"](False) is False
    write.assert_not_called()
    assert "another distribution" in namespace["get_update_handover_error"]()


@pytest.mark.parametrize("installed,onedir,kind", [
    (False, False, "installer"), (True, False, "portable_exe"),
    (False, True, "installer"), (False, True, "portable_exe"),
    (False, True, "onedir_zip"), (True, False, "unknown"),
])
def test_final_handover_rejects_foreign_or_nonexecutable_type(tmp_path, installed, onedir, kind):
    namespace = runtime(tmp_path, installed, onedir)
    official = dict(official_fixture(), asset_type=kind)
    with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
            security, "verify_for_apply", return_value=(tmp_path / "payload", official)):
        assert namespace["launch_pending_installer"]({}) is False
    namespace["subprocess"].Popen.assert_not_called()
    assert not (tmp_path / "run_update.cmd").exists()


def test_installed_onedir_can_apply_matching_setup(tmp_path):
    namespace = runtime(tmp_path, installed=True, onedir=True)
    official, record = pending_fixture(tmp_path)
    with mock.patch.object(security, "is_elevated", return_value=False), mock.patch.object(
            security, "resolve_release", return_value=official):
        assert namespace["launch_pending_installer"](record, "1.0.0") is True
    namespace["subprocess"].Popen.assert_called_once()


@pytest.mark.parametrize("kind,name,expected", [
    ("installer", NAME, True), ("portable_exe", "SSHDriveMgr.exe", False),
    ("installer", "wrong.exe", False), ("onedir_zip", ZIP_NAME, False),
])
def test_dialog_ready_requires_matching_version_type_and_filename(kind, name, expected):
    function = definition("run_update_dialog")
    dialog = mock.Mock()
    wire = mock.Mock()
    function.__globals__.update(UpdateDialog=dialog, _wire=wire)
    manager = mock.Mock()
    manager.pending_update.return_value = {
        "version": VERSION, "asset_type": kind, "asset_name": name, "install_on_next_start": True,
    }
    function(None, manager, VERSION, "changes", "url", "installer")
    assert dialog.call_args.kwargs["already_downloaded"] is expected


def test_zip_dialog_has_manual_guidance_no_automatic_install_controls():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from src.ui.dialogs.update_dialog import UpdateDialog
    from src.i18n import current_language, set_language
    app = QApplication.instance() or QApplication([])
    previous = current_language()
    set_language("zh")
    dialog = UpdateDialog(version=VERSION, obj_type="onedir_zip", already_downloaded=True,
                          download_url=security.release_asset_url("v" + VERSION, ZIP_NAME))
    try:
        assert dialog._downloaded is False
        assert dialog.install_btn.isHidden() and dialog._opt_row.isHidden()
        assert "ZIP" in dialog.browser_btn.text()
        assert "_internal" in dialog.lead.text()
        with mock.patch("webbrowser.open") as opened:
            dialog._open_browser()
        opened.assert_called_once_with(security.release_asset_url("v" + VERSION, ZIP_NAME))
    finally:
        dialog.hide()
        dialog.deleteLater()
        app.processEvents()
        set_language(previous)
