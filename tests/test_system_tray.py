"""Real QAction regressions: Qt's checked bool must never control unmounting."""
import os
from unittest import mock

import pytest

from src.config import Connection

_APP = None


@pytest.fixture(scope="module")
def qt_app():
    global _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    _APP.setQuitOnLastWindowClosed(False)
    return _APP


@pytest.fixture
def tray(qt_app):
    from src.ui.system_tray import SystemTray
    window = mock.Mock()
    icon = SystemTray(window)
    yield icon, window
    menu = icon.contextMenu()
    icon.hide()
    if menu is not None:
        menu.deleteLater()
    icon.deleteLater()
    qt_app.processEvents()


def quit_action(icon):
    action = icon.contextMenu().actions()[-1]
    assert not action.isSeparator()
    return action


def test_initial_menu_quit_forces_unmount_despite_checked_false(tray):
    icon, window = tray
    action = quit_action(icon)
    assert not action.isCheckable()
    assert not action.isChecked()
    action.trigger()
    window.quit_app.assert_called_once_with(unmount=True)


def test_refreshed_empty_menu_quit_also_forces_unmount(tray):
    icon, window = tray
    icon.update_connections_menu([], set())
    quit_action(icon).trigger()
    window.quit_app.assert_called_once_with(unmount=True)


def test_refreshed_connection_menu_quit_forces_unmount_without_toggling_connection(tray):
    icon, window = tray
    conn = Connection("test", "example.invalid", "u", drive_letter="X:")
    icon.update_connections_menu([conn], {"X:"})
    quit_action(icon).trigger()
    window.quit_app.assert_called_once_with(unmount=True)
    window._on_mount.assert_not_called()
    window._on_unmount.assert_not_called()


@pytest.mark.parametrize("checked", [False, True])
def test_quit_slot_ignores_both_qaction_checked_values(tray, checked):
    icon, window = tray
    action = quit_action(icon)
    action.setCheckable(True)
    action.setChecked(not checked)
    action.trigger()
    assert action.isChecked() is checked
    window.quit_app.assert_called_once_with(unmount=True)


def test_repeated_menu_refresh_never_reintroduces_boolean_argument_binding(tray):
    icon, window = tray
    for _ in range(5):
        icon.update_connections_menu([], set())
        quit_action(icon).trigger()
    assert window.quit_app.call_args_list == [mock.call(unmount=True)] * 5
