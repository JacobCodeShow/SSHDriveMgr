"""Offscreen editor-version shutdown races, with local files and fake transfers."""
import importlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest


def spin(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    assert predicate(), "Qt shutdown condition timed out"


def atomic_save(app, source, target, timeout=2):
    """Simulate an editor retrying a transient Windows share/delete lock.

    This fixture tests delivery of a late atomic save, not a contention-free
    MoveFileEx call. Never ignore a failed save or change file permissions.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            app.processEvents()
            time.sleep(0.02)


def test_atomic_save_retries_only_transient_windows_locks(monkeypatch):
    temporary_lock = PermissionError("temporary share lock")
    temporary_lock.winerror = 32
    replace = mock.Mock(side_effect=[temporary_lock, None])
    monkeypatch.setattr(os, "replace", replace)
    app = SimpleNamespace(processEvents=mock.Mock())
    atomic_save(app, "new", "old")
    assert replace.call_count == 2
    app.processEvents.assert_called_once()


def test_atomic_save_does_not_hide_permanent_or_expired_errors(monkeypatch):
    for winerror, timeout in ((None, 2), (5, 0), (32, 0), (33, 0)):
        error = PermissionError("must propagate")
        if winerror is not None:
            error.winerror = winerror
        replace = mock.Mock(side_effect=error)
        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(PermissionError, match="must propagate"):
            atomic_save(SimpleNamespace(processEvents=mock.Mock()), "new", "old", timeout=timeout)
        replace.assert_called_once()


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


class FakeClient:
    def __init__(self):
        self.uploads = []
        self.blocks = {}
        self.upload_entered = {}
        self.disconnect_entered = threading.Event()
        self.release_disconnect = threading.Event()
        self.release_disconnect.set()
        self.connect_entered = threading.Event()
        self.connect_threads = []
        self.disconnect_threads = []
        self.connected = True
        self.connect_error = None

    def upload(self, local, remote, progress_callback=None):
        assert self.connected, "upload raced disconnect/reconnect"
        data = Path(local).read_bytes()
        self.uploads.append((remote, data))
        count = sum(path == remote for path, _ in self.uploads)
        key = (remote, count)
        self.upload_entered.setdefault(key, threading.Event()).set()
        if key in self.blocks:
            assert self.blocks[key].wait(5), "test did not release upload"
        if progress_callback:
            progress_callback(len(data), len(data))

    def disconnect(self):
        self.disconnect_threads.append(threading.get_ident())
        self.disconnect_entered.set()
        assert self.release_disconnect.wait(5), "test did not release disconnect"
        self.connected = False

    def connect(self, conn, *, tofu_callback=None):
        self.connect_threads.append(threading.get_ident())
        self.connect_entered.set()
        if self.connect_error:
            raise RuntimeError(self.connect_error)
        self.connected = True

    def entered(self, remote, count=1):
        return self.upload_entered.get((remote, count), threading.Event()).is_set()


@pytest.fixture
def browser(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    bm = importlib.import_module("src.ui.sftp_browser")
    from src.config import Connection
    client = FakeClient()
    monkeypatch.setattr(bm, "SftpClient", lambda: client)
    monkeypatch.setattr(bm.SftpBrowserWindow, "_connect_and_load", lambda self: None)
    monkeypatch.setattr(bm.SftpBrowserWindow, "_on_refresh", lambda self: None)
    monkeypatch.setattr(bm.StyledMessageBox, "critical", mock.Mock())
    page = bm.SftpBrowserWindow(Connection("test", "never-connect.invalid", "u", password="fake"))
    failed = []
    page.shutdown_failed.connect(failed.append)
    qt_app.processEvents()
    yield page, client, bm, failed
    for release in client.blocks.values():
        release.set()
    client.release_disconnect.set()
    spin(qt_app, lambda: not any(worker.isRunning() for worker in page._workers))
    qt_app.processEvents()
    page._edit_map.clear()
    page._close_pending_edits.clear()
    for timer in page._edit_timers.values():
        timer.stop()
    page._edit_timers.clear()
    page._edit_reconnect_failed = False
    if not page._close_ready:
        # Do not let an intentionally cancelled close strand a Qt worker/window.
        page.request_shutdown()
        page._close_last_change_at = time.monotonic() - 1
        spin(qt_app, page.is_shutdown_complete)
    page.deleteLater()
    qt_app.processEvents()


def edit_file(page, tmp_path, name, data=b"v1", baseline=False, watch=True):
    path = tmp_path / name
    path.write_bytes(data)
    local = str(path)
    page._edit_map[local] = "/" + name
    if baseline:
        page._edit_uploaded_versions[local] = page._edit_version(local)
    if watch:
        page._watcher.addPath(local)
    return path


@pytest.mark.parametrize("watch_event", [True, False])
def test_save_a_while_slow_flush_b_is_not_lost(browser, qt_app, tmp_path, watch_event):
    page, client, _, failed = browser
    a = edit_file(page, tmp_path, "a.txt", b"a-v1")
    edit_file(page, tmp_path, "b.txt", b"b-v1")
    release_b = client.blocks[("/b.txt", 1)] = threading.Event()
    page.request_shutdown()
    spin(qt_app, lambda: client.entered("/b.txt"))
    assert str(a) in page._watcher.files(), "close must not remove edit watches"
    if not watch_event:
        page._watcher.removePath(str(a))
    if watch_event:
        replacement = tmp_path / "atomic-save.tmp"
        replacement.write_bytes(b"a-version-two")
        atomic_save(qt_app, replacement, a)
    else:
        a.write_bytes(b"a-version-two")
    if watch_event:
        spin(qt_app, lambda: page._edit_generations.get(str(a), 0) > 0)
    release_b.set()
    spin(qt_app, page.is_shutdown_complete)
    assert [data for remote, data in client.uploads if remote == "/a.txt"] == [b"a-v1", b"a-version-two"]
    assert [data for remote, data in client.uploads if remote == "/b.txt"] == [b"b-v1"]
    assert not failed


def test_success_records_start_version_not_save_during_upload(browser, qt_app, tmp_path):
    page, client, _, _ = browser
    local = edit_file(page, tmp_path, "a.txt", b"old")
    first_release = client.blocks[("/a.txt", 1)] = threading.Event()
    second_release = client.blocks[("/a.txt", 2)] = threading.Event()
    start_version = page._edit_version(str(local))
    page.request_shutdown()
    spin(qt_app, lambda: client.entered("/a.txt"))
    local.write_bytes(b"new-save-during-upload")
    page._on_watched_file_changed(str(local))
    first_release.set()
    spin(qt_app, lambda: client.entered("/a.txt", 2))
    assert page._edit_uploaded_versions[str(local)] == start_version
    assert page._edit_uploaded_versions[str(local)] != page._edit_version(str(local))
    second_release.set()
    spin(qt_app, page.is_shutdown_complete)
    assert client.uploads[-1] == ("/a.txt", b"new-save-during-upload")


def test_same_stat_save_still_reuploads_on_watcher_generation(browser, qt_app, tmp_path):
    page, client, _, _ = browser
    a = edit_file(page, tmp_path, "a.txt", b"aa")
    edit_file(page, tmp_path, "b.txt")
    release_b = client.blocks[("/b.txt", 1)] = threading.Event()
    page.request_shutdown()
    spin(qt_app, lambda: client.entered("/b.txt"))
    before = a.stat()
    a.write_bytes(b"bb")
    os.utime(a, ns=(before.st_atime_ns, before.st_mtime_ns))
    page._on_watched_file_changed(str(a))
    assert page._edit_version(str(a))[:3] == (before.st_mtime_ns, before.st_size, before.st_ino)
    release_b.set()
    spin(qt_app, page.is_shutdown_complete)
    assert client.uploads[-1] == ("/a.txt", b"bb")


@pytest.mark.parametrize("watch_event", [True, False])
def test_edit_while_disconnecting_waits_then_reconnects_and_saves(browser, qt_app, tmp_path, watch_event):
    page, client, _, failed = browser
    local = edit_file(page, tmp_path, "a.txt", b"original", baseline=True)
    client.release_disconnect.clear()
    main_thread = threading.get_ident()
    page.request_shutdown()
    spin(qt_app, client.disconnect_entered.is_set)
    assert not page.is_shutdown_complete()
    if not watch_event:
        page._watcher.removePath(str(local))
    local.write_bytes(b"saved-during-disconnect")
    spin(qt_app, lambda: page._shutdown_recovery_reason is not None)
    assert not failed, "must wait for actual disconnect before abort/reconnect"
    assert not client.connect_entered.is_set()
    client.release_disconnect.set()
    spin(qt_app, lambda: failed and client.connect_entered.is_set() and not page._edit_reconnecting)
    spin(qt_app, lambda: client.uploads and not page._workers)
    assert client.uploads[-1] == ("/a.txt", b"saved-during-disconnect")
    assert not page._close_ready and not page._close_requested
    assert page._edit_map[str(local)] == "/a.txt"
    assert page._fdlg_content.isEnabled()
    assert client.connect_threads and all(thread != main_thread for thread in client.connect_threads)
    assert all(thread != main_thread for thread in client.disconnect_threads)
    page.request_shutdown()
    spin(qt_app, page.is_shutdown_complete)


def test_cancel_global_shutdown_during_disconnect_recovers_session(browser, qt_app):
    page, client, _, failed = browser
    client.release_disconnect.clear()
    page.request_shutdown()
    spin(qt_app, client.disconnect_entered.is_set)
    page.cancel_shutdown()
    assert not client.connect_entered.is_set()
    assert not page.is_shutdown_complete()
    client.release_disconnect.set()
    spin(qt_app, lambda: failed and client.connect_entered.is_set() and not page._edit_reconnecting)
    assert client.connected
    assert not page._close_requested and not page._close_ready


def test_global_cancel_during_flush_resumes_queued_autosaves(browser, qt_app, tmp_path):
    page, client, _, failed = browser
    a = edit_file(page, tmp_path, "a.txt", b"a-v1")
    edit_file(page, tmp_path, "b.txt", b"b-v1")
    release_b = client.blocks[("/b.txt", 1)] = threading.Event()
    page.request_shutdown()
    spin(qt_app, lambda: client.entered("/b.txt"))
    a.write_bytes(b"a-save-while-closing")
    page._on_watched_file_changed(str(a))
    page.cancel_shutdown()
    release_b.set()
    spin(qt_app, lambda: ("/a.txt", b"a-save-while-closing") in client.uploads and not page._workers)
    assert not page._close_requested and not page._close_ready
    assert not client.disconnect_entered.is_set() and not failed


def test_missing_local_file_aborts_close_and_retains_recovery_path(browser, qt_app, tmp_path):
    page, client, bm, failed = browser
    local = edit_file(page, tmp_path, "missing.txt", baseline=True)
    local.unlink()
    page.request_shutdown()
    spin(qt_app, lambda: bool(failed))
    assert not page._close_requested and not page._close_ready
    assert page._edit_map[str(local)] == "/missing.txt"
    assert (str(local), "/missing.txt") in page._close_pending_edits
    assert not client.disconnect_entered.is_set()
    bm.StyledMessageBox.critical.assert_called()


def test_missing_during_disconnect_aborts_only_after_disconnect_finishes(browser, qt_app, tmp_path):
    page, client, _, failed = browser
    local = edit_file(page, tmp_path, "missing.txt", baseline=True)
    client.release_disconnect.clear()
    page.request_shutdown()
    spin(qt_app, client.disconnect_entered.is_set)
    local.unlink()
    spin(qt_app, lambda: page._shutdown_recovery_reason is not None)
    assert not failed
    client.release_disconnect.set()
    spin(qt_app, lambda: failed and not page._edit_reconnecting)
    assert not page._close_ready
    assert page._edit_map[str(local)] == "/missing.txt"
    assert (str(local), "/missing.txt") in page._close_pending_edits


def test_reconnect_failure_preserves_edit_and_prevents_false_shutdown_success(browser, qt_app, tmp_path):
    page, client, bm, failed = browser
    local = edit_file(page, tmp_path, "a.txt", baseline=True)
    client.release_disconnect.clear()
    client.connect_error = "fake reconnect failure"
    page.request_shutdown()
    spin(qt_app, client.disconnect_entered.is_set)
    local.write_bytes(b"must-survive")
    page._on_watched_file_changed(str(local))
    client.release_disconnect.set()
    spin(qt_app, lambda: page._edit_reconnect_failed)
    assert failed and not page.is_shutdown_complete()
    assert page._edit_map[str(local)] == "/a.txt"
    assert local.read_bytes() == b"must-survive"
    assert not client.uploads
    bm.StyledMessageBox.critical.assert_called()


def test_unknown_baseline_uploads_and_disconnect_waits_for_half_second_quiet(browser, qt_app, tmp_path, monkeypatch):
    page, client, bm, _ = browser
    edit_file(page, tmp_path, "a.txt", b"unknown-remote-baseline")
    now = [100.0]
    monkeypatch.setattr(bm, "time", SimpleNamespace(monotonic=lambda: now[0]))
    page.request_shutdown()
    spin(qt_app, lambda: client.uploads and not page._workers)
    page._finish_close_when_idle()
    assert not client.disconnect_entered.is_set()
    now[0] = 100.499
    page._finish_close_when_idle()
    assert not client.disconnect_entered.is_set()
    now[0] = 100.501
    page._finish_close_when_idle()
    spin(qt_app, page.is_shutdown_complete)
    assert client.uploads == [("/a.txt", b"unknown-remote-baseline")]
