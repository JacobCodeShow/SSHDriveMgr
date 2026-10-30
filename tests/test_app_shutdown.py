"""Real offscreen Qt lifecycle tests; all clients/controllers/config are isolated.

No SSH connection, named pipe, mount, update, user database or registry is used.
"""
import importlib
import os
from pathlib import Path
import sys
import threading
import time
import types
from unittest import mock

import pytest


def spin(app, predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    assert predicate(), "Qt lifecycle condition did not complete before timeout"


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    # Auth module initialization is isolated before its first possible import.
    profile = tmp_path / "appdata"
    profile.mkdir()
    monkeypatch.setenv("APPDATA", str(profile))
    mw = importlib.import_module("src.ui.main_window")
    from src.config import AppSettings, Connection
    connections = {"A": Connection("A", "a.invalid", "u", id="A", password="test", drive_letter="X:"),
                   "B": Connection("B", "b.invalid", "u", id="B", password="test", drive_letter="Y:")}
    manager = mock.Mock()
    manager.get_settings.return_value = AppSettings(minimize_to_tray=False)
    manager.get_connections.side_effect = lambda: list(connections.values())
    manager.get_all.side_effect = lambda *a, **k: list(connections.values())
    manager.get_by_id.side_effect = connections.get
    manager.get_active_mounts.return_value = []
    manager.get_templates.return_value = []
    controller = mock.Mock()
    controller.get_mounted_drives.return_value = {}
    monkeypatch.setattr(mw, "UserConnectionManager", lambda user: manager)
    monkeypatch.setattr(mw.Session, "current", lambda: types.SimpleNamespace(id="test", username="test"))
    monkeypatch.setattr(mw.Session, "is_admin", lambda: False)
    monkeypatch.setattr(mw, "SSHFSController", lambda: controller)
    monkeypatch.setattr(mw.MainWindow, "_setup_ipc", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_start_terminal_bridge", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_auto_reconnect_mounts", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_check_prerequisites", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_setup_tray", lambda self: setattr(self, "_tray", mock.Mock()))
    quit_call = mock.Mock()
    monkeypatch.setattr(mw.QApplication, "quit", quit_call)
    page = mw.MainWindow()
    page._poll_timer.stop()
    qt_app.processEvents()
    yield page, mw, manager, controller, connections, quit_call
    page._shutdown_requested = False
    page._shutdown_timer.stop()
    page._poll_timer.stop()
    for worker in list(page._tracked_workers):
        worker.requestInterruption()
        assert worker.wait(5000), "test left a worker running"
    page._reap_finished_workers()
    for browser in list(page._sftp_browsers.values()):
        from PyQt6 import sip
        if sip.isdeleted(browser):
            continue
        browser._close_pending_edits.clear()
        for timer in browser._edit_timers.values():
            timer.stop()
        browser._edit_timers.clear()
        browser._edit_map.clear()
        browser._client = mock.Mock()
        browser.request_shutdown()
        spin(qt_app, browser.is_shutdown_complete)
        browser.deleteLater()
    page._explicit_quit = True
    page.close()
    page.deleteLater()
    qt_app.processEvents()


def fake_terminal_module(monkeypatch):
    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import QWidget
    class Panel(QWidget):
        reconnect_requested = pyqtSignal(str)
        def __init__(self, bridge, key, conn, theme, parent=None):
            super().__init__(parent)
            self.key = key
            self.token = None
        def load_session(self, token):
            self.token = token
        def close_session(self):
            pass
    module = types.ModuleType("src.terminal.terminal_panel")
    module.TerminalPanel = Panel
    monkeypatch.setitem(sys.modules, "src.terminal.terminal_panel", module)
    return Panel


def test_late_handshake_keeps_visible_host_b(window, monkeypatch):
    page, mw, manager, controller, connections, quit_call = window
    Panel = fake_terminal_module(monkeypatch)
    current = Panel(None, "B#1", connections["B"], "dark", page)
    page._terminal_stack.addWidget(current)
    page._terminal_panels["B#1"] = current
    page._terminal_conn_tabs["B"] = ["B#1"]
    page._terminal_active_tab["B"] = "B#1"
    page._panel_mode = mw._PANEL_TERMINAL
    page._panel_conn_id = "B"
    page._rebuild_tab_bar("B")
    page._terminal_stack.setCurrentWidget(current)
    page._terminal_pending_conn["A#1"] = connections["A"]
    page._on_terminal_connected("A#1", "token-A")
    assert page._terminal_stack.currentWidget() is current
    assert page._panel_conn_id == "B"
    assert page._terminal_panels["A#1"].token == "token-A"
    for index in range(page._terminal_tab_bar_layout.count()):
        button = page._terminal_tab_bar_layout.itemAt(index).widget()
        if button:
            assert button.property("session_key").startswith("B#")


def test_opening_pending_host_never_shows_old_host(window, monkeypatch):
    page, mw, _, _, connections, _ = window
    Panel = fake_terminal_module(monkeypatch)
    old = Panel(None, "A#1", connections["A"], "dark", page)
    page._terminal_stack.addWidget(old)
    page._terminal_stack.setCurrentWidget(old)
    page._terminal_panels["A#1"] = old
    page._terminal_conn_tabs["A"] = ["A#1"]
    page._panel_mode = mw._PANEL_TERMINAL
    page._panel_conn_id = "A"
    page._terminal_connecting.add("B")
    page._open_terminal_panel("B")
    assert page._panel_conn_id == "B"
    assert page._terminal_stack.currentWidget() is page._terminal_placeholder


def test_stale_tab_click_cannot_switch_current_host(window, monkeypatch):
    page, mw, _, _, connections, _ = window
    Panel = fake_terminal_module(monkeypatch)
    current = Panel(None, "B#1", connections["B"], "dark", page)
    other = Panel(None, "A#1", connections["A"], "dark", page)
    for widget in (current, other):
        page._terminal_stack.addWidget(widget)
    page._terminal_panels.update({"B#1": current, "A#1": other})
    page._panel_mode = mw._PANEL_TERMINAL
    page._panel_conn_id = "B"
    page._terminal_stack.setCurrentWidget(current)
    page._switch_terminal_tab("A", "A#1")
    assert page._terminal_stack.currentWidget() is current


def test_result_signal_does_not_destroy_running_qthread(window, qt_app):
    page, mw, _, controller, connections, _ = window
    from PyQt6 import sip
    from src.ui.worker import MountWorker
    from src.sshfs_controller import MountResult
    emitted = threading.Event()
    release = threading.Event()
    class Worker(MountWorker):
        def run(self):
            self.result_ready.emit(self.conn.id, MountResult(True, "ok"))
            emitted.set()
            release.wait(5)
    worker = Worker(connections["A"], controller)
    worker.result_ready.connect(page._on_mount_finished)
    page._workers["A"] = worker
    page._track_background_worker(worker)
    worker.start()
    try:
        spin(qt_app, lambda: emitted.is_set() and "A" not in page._workers)
        assert worker in page._tracked_workers
        assert worker.isRunning()
        assert not sip.isdeleted(worker)
    finally:
        release.set()
        spin(qt_app, lambda: worker not in page._tracked_workers)


def make_browser(page, monkeypatch, qt_app, client):
    browser_module = importlib.import_module("src.ui.sftp_browser")
    monkeypatch.setattr(browser_module, "SftpClient", lambda: client)
    monkeypatch.setattr(browser_module.SftpBrowserWindow, "_connect_and_load", lambda self: None)
    browser = browser_module.SftpBrowserWindow(page._mgr.get_by_id("A"), parent=page)
    browser.shutdown_failed.connect(page._cancel_shutdown)
    page._sftp_browsers["A"] = browser
    qt_app.processEvents()
    return browser, browser_module


def test_global_shutdown_flushes_edits_and_waits_for_disconnect(window, qt_app, monkeypatch, tmp_path):
    page, _, _, _, _, quit_call = window
    from PyQt6.QtCore import QTimer
    from src.ui.sftp_worker import SftpUploadWorker
    uploading = threading.Event()
    finish_upload = threading.Event()
    disconnecting = threading.Event()
    finish_disconnect = threading.Event()
    client = mock.Mock()
    def disconnect():
        disconnecting.set()
        finish_disconnect.wait(5)
    client.disconnect.side_effect = disconnect
    browser, bm = make_browser(page, monkeypatch, qt_app, client)
    class Upload(SftpUploadWorker):
        def run(self):
            uploading.set()
            finish_upload.wait(5)
            self.finished.emit(self._remote_path)
    monkeypatch.setattr(bm, "SftpUploadWorker", Upload)
    local = tmp_path / "edited.txt"
    local.write_text("saved edit", encoding="utf-8")
    browser._edit_map[str(local)] = "/edited.txt"
    pending = QTimer(browser)
    pending.setSingleShot(True)
    pending.start(10000)
    browser._edit_timers[str(local)] = pending
    page._bridge_server = mock.Mock()
    page._bridge_server.stop.return_value = True
    assert page.quit_app(unmount=False)
    try:
        spin(qt_app, uploading.is_set)
        quit_call.assert_not_called()
        finish_upload.set()
        spin(qt_app, disconnecting.is_set)
        quit_call.assert_not_called()
        finish_disconnect.set()
        spin(qt_app, lambda: quit_call.called)
        assert browser.is_shutdown_complete()
        assert page._explicit_quit
    finally:
        finish_upload.set()
        finish_disconnect.set()


def test_failed_flush_cancels_global_quit_and_retains_edit(window, qt_app, monkeypatch, tmp_path):
    page, _, _, _, _, quit_call = window
    from PyQt6.QtCore import QTimer
    from src.ui.sftp_worker import SftpUploadWorker
    client = mock.Mock()
    browser, bm = make_browser(page, monkeypatch, qt_app, client)
    monkeypatch.setattr(bm.StyledMessageBox, "critical", mock.Mock())
    class Upload(SftpUploadWorker):
        def run(self):
            self.error.emit("network unavailable")
    monkeypatch.setattr(bm, "SftpUploadWorker", Upload)
    local = tmp_path / "edited.txt"
    local.write_text("save me", encoding="utf-8")
    browser._edit_map[str(local)] = "/edited.txt"
    timer = QTimer(browser)
    timer.start(10000)
    browser._edit_timers[str(local)] = timer
    assert page.quit_app(unmount=False)
    spin(qt_app, lambda: not page._shutdown_requested)
    quit_call.assert_not_called()
    assert browser._edit_map[str(local)] == "/edited.txt"
    assert (str(local), "/edited.txt") in browser._close_pending_edits
    assert browser._fdlg_content.isEnabled()
    client.disconnect.assert_not_called()
    spin(qt_app, lambda: not browser._workers)


def test_shutdown_unmounts_only_recorded_account_mounts(window, qt_app):
    page, _, _, controller, _, quit_call = window
    from src.sshfs_controller import MountResult
    controller.unmount.return_value = MountResult(True, "ok")
    page._active_mounts = {"A"}
    page._bridge_server = mock.Mock()
    page._bridge_server.stop.return_value = True
    assert page.quit_app(unmount=True)
    spin(qt_app, lambda: quit_call.called)
    controller.unmount.assert_called_once_with("X:")


def test_tray_quit_action_reaches_actual_unmount_gate(window, qt_app):
    page, _, _, controller, _, quit_call = window
    from src.sshfs_controller import MountResult
    from src.ui.system_tray import SystemTray
    controller.unmount.return_value = MountResult(True, "ok")
    page._active_mounts = {"A"}
    page._bridge_server = mock.Mock()
    page._bridge_server.stop.return_value = True
    tray = SystemTray(page)
    try:
        tray.contextMenu().actions()[-1].trigger()  # emits checked=False
        spin(qt_app, lambda: quit_call.called)
        assert page._shutdown_unmount is True
        controller.unmount.assert_called_once_with("X:")
    finally:
        tray.contextMenu().deleteLater()
        tray.deleteLater()


def test_explicit_keep_mounts_exit_still_preserves_requested_behavior(window, qt_app):
    page, _, _, controller, _, quit_call = window
    page._active_mounts = {"A"}
    page._bridge_server = mock.Mock()
    page._bridge_server.stop.return_value = True
    assert page.quit_app(unmount=False)
    spin(qt_app, lambda: quit_call.called)
    controller.unmount.assert_not_called()


def test_failed_shutdown_keeps_sysinfo_thread_valid_for_second_quit(window, qt_app, monkeypatch):
    page, mw, _, _, connections, quit_call = window
    from PyQt6 import sip
    from PyQt6.QtCore import QThread, pyqtSignal
    from PyQt6.QtWidgets import QWidget
    module = importlib.import_module("src.ui.system_info_panel")
    release = threading.Event()
    class InfoThread(QThread):
        info_ready = pyqtSignal(dict)
        error = pyqtSignal(str, str)
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.stopped = False
        def run(self):
            release.wait(5)
        def stop(self):
            self.stopped = True
        def resume(self):
            self.stopped = False
    class Gate(QWidget):
        ready = False
        def request_shutdown(self):
            pass
        def is_shutdown_complete(self):
            return self.ready
        def cancel_shutdown(self):
            pass
    monkeypatch.setattr(module, "SSHSystemInfoThread", InfoThread)
    panel = module.SystemInfoPanel(connections["A"], parent=page._rp_content)
    page._rp_layout.addWidget(panel)
    page._current_info_panel = panel
    gate = Gate(page)
    page._sftp_browsers["A"] = gate
    worker = panel._info_thread
    page._bridge_server = mock.Mock()
    page._bridge_server.stop.return_value = True
    try:
        assert page.quit_app(unmount=False)
        release.set()
        spin(qt_app, lambda: worker not in page._tracked_workers)
        assert not sip.isdeleted(worker)
        page._cancel_shutdown("save failed")
        assert not panel._background_stopped
        assert panel._refresh_timer.isActive()
        gate.ready = True
        assert page.quit_app(unmount=False)
        spin(qt_app, lambda: quit_call.called)
        assert page._explicit_quit
    finally:
        release.set()
        if panel._info_thread is not None and not sip.isdeleted(panel._info_thread):
            panel._info_thread.wait(5000)
        page._sftp_browsers.clear()
        gate.deleteLater()


def test_cancelled_sysinfo_waits_for_native_end_then_fetches_again(window, qt_app, monkeypatch):
    page, _, _, _, connections, quit_call = window
    from PyQt6.QtCore import QThread, pyqtSignal
    from PyQt6.QtWidgets import QWidget
    module = importlib.import_module("src.ui.system_info_panel")
    checkpoint = threading.Event()
    suppressed = threading.Event()
    finish_native = threading.Event()
    created = []
    class InfoThread(QThread):
        info_ready = pyqtSignal(dict)
        error = pyqtSignal(str, str)
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.stopped = False
            self.first = not created
            created.append(self)
        def run(self):
            if self.first:
                checkpoint.wait(5)
                if self.stopped:
                    suppressed.set()
                finish_native.wait(5)
            else:
                self.info_ready.emit({"connected": False, "error": "test-only result"})
        def stop(self):
            self.stopped = True
    class Gate(QWidget):
        def request_shutdown(self):
            pass
        def is_shutdown_complete(self):
            return False
        def cancel_shutdown(self):
            pass
    monkeypatch.setattr(module, "SSHSystemInfoThread", InfoThread)
    panel = module.SystemInfoPanel(connections["A"], parent=page._rp_content)
    page._rp_layout.addWidget(panel)
    page._current_info_panel = panel
    gate = Gate(page)
    page._sftp_browsers["A"] = gate
    try:
        assert page.quit_app(unmount=False)
        checkpoint.set()
        spin(qt_app, suppressed.is_set)
        page._cancel_shutdown("test cancellation")
        assert panel._resume_timer.isActive()
        assert len(created) == 1
        finish_native.set()
        spin(qt_app, lambda: len(created) == 2 and panel._refresh_btn.isEnabled())
        assert not panel._resume_timer.isActive()
        quit_call.assert_not_called()
    finally:
        checkpoint.set()
        finish_native.set()
        for worker in created:
            from PyQt6 import sip
            if not sip.isdeleted(worker):
                worker.wait(5000)
        page._sftp_browsers.clear()
        gate.deleteLater()


def test_shutdown_rejects_late_terminal_result_without_creating_panel(window):
    page, _, _, _, connections, _ = window
    page._shutdown_requested = True
    page._bridge_server = mock.Mock()
    page._terminal_pending_conn["A#1"] = connections["A"]
    page._on_terminal_connected("A#1", "token-A")
    page._bridge_server.close_session.assert_called_once_with("A#1")
    assert "A#1" not in page._terminal_panels
