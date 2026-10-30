"""Dependency-free UI regression tests.

Methods are compiled directly from their real source with fake Qt collaborators.
These verify controller behavior, not rendering or the native Qt event loop.
Run: python -B -m unittest discover -s tests -p test_ui_consistency.py -v
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from src.config import AppSettings, Connection

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "src/ui/main_window.py"
BROWSER = ROOT / "src/ui/sftp_browser.py"
MODES = {"_PANEL_NONE": "none", "_PANEL_SETTINGS": "settings", "_PANEL_USERS": "users",
         "_PANEL_PROFILE": "profile", "_PANEL_ADD": "add", "_PANEL_EDIT": "edit",
         "_PANEL_INFO": "info", "_PANEL_SYSINFO": "sysinfo"}


def methods(path, class_name, names, base=object, **extra):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    selected = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in selected:
        node.decorator_list = []
    cls = ast.ClassDef(name="Subject", bases=[ast.Name(id="Base", ctx=ast.Load())],
                       keywords=[], body=selected, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    namespace = {**MODES, "Base": base, "tr": lambda key, **kw: key,
                 "AppSettings": AppSettings, "Connection": Connection,
                 "PROTOCOL_SFTP": "sftp", "PROTOCOL_FTP": "ftp", "PROTOCOL_FTPS": "ftps",
                 "QApplication": SimpleNamespace(instance=lambda: None), **extra}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["Subject"]


class Field:
    def __init__(self, value):
        self.value = value
    def text(self):
        return self.value


class PasswordAndHiddenConfigTests(unittest.TestCase):
    def test_password_text_preserves_spaces_and_tabs(self):
        cls = methods(MAIN, "MainWindow", {"_safe_lineedit_text"})
        page = cls()
        page._ef_pw = Field(" \tvalid secret\t ")
        self.assertEqual(page._safe_lineedit_text("_ef_pw", strip=False), " \tvalid secret\t ")
        self.assertEqual(page._safe_lineedit_text("_ef_pw"), "valid secret")
        self.assertEqual(page._safe_lineedit_text("_missing", strip=False), "")

    def test_all_password_save_and_snapshot_calls_request_raw_value(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "_safe_lineedit_text"
                 and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "_ef_pw"]
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            self.assertTrue(any(k.arg == "strip" and isinstance(k.value, ast.Constant)
                                and k.value.value is False for k in call.keywords))

    def test_ppk_field_is_bound_before_backend_visibility_branch(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_build_edit_form")
        bound = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Attribute) and t.attr == "_ef_putty_key" for t in n.targets))
        conditional = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.If)
                           and isinstance(n.test, ast.Attribute) and n.test.attr == "use_putty")
        self.assertLess(bound, conditional)

    def test_ssh_collector_preserves_ppk_and_ftp_clears_ssh_fields(self):
        cls = methods(MAIN, "MainWindow", {"_ef_collect_protocol_fields"})
        page = cls()
        page._ef_current_protocol = lambda: "sftp"
        page._safe_lineedit_text = lambda name: {"_ef_putty_key": "C:/keys/private.ppk", "_ef_key": ""}.get(name, "")
        page._safe_bool_checked = lambda name, default=False: default
        page._safe_current_data = lambda name, default=None: default
        self.assertEqual(page._ef_collect_protocol_fields()["putty_key_path"], "C:/keys/private.ppk")
        page._ef_current_protocol = lambda: "ftp"
        fields = page._ef_collect_protocol_fields()
        self.assertEqual(fields["putty_key_path"], "")
        self.assertFalse(fields["cli_access_enabled"])


class NavigationTests(unittest.TestCase):
    def test_active_settings_click_does_not_run_leave_guard(self):
        cls = methods(MAIN, "MainWindow", {"_open_settings_panel"})
        page = mock.Mock()
        page._panel_mode = "settings"
        cls._open_settings_panel(page)
        page._guard_leave_form.assert_not_called()
        page._build_settings_form.assert_not_called()

    def test_delete_while_mount_in_flight_is_rejected(self):
        cls = methods(MAIN, "MainWindow", {"_delete_connection"})
        page = mock.Mock()
        page._workers = {"id": object()}
        cls._delete_connection(page, "id")
        page._mgr.delete.assert_not_called()
        page._set_status.assert_called_once_with("action.busy")

    def test_delete_rechecks_after_modal_and_event_processing(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_confirm_delete_connection")
        worker_checks = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                         and isinstance(n.left, ast.Name) and n.left.id == "conn_id"
                         and any(isinstance(c, ast.Attribute) and c.attr == "_workers" for c in n.comparators)]
        self.assertGreaterEqual(len(worker_checks), 2)

    def test_add_from_fullscreen_reveals_connections_page(self):
        cls = methods(MAIN, "MainWindow", {"_open_add_panel"})
        page = mock.Mock()
        page._guard_leave_form.return_value = True
        page._panel_conn_id = None
        page._selected_id = None
        cls._open_add_panel(page)
        page._main_stack.setCurrentIndex.assert_called_once_with(0)
        self.assertEqual(page._panel_mode, "add")
        page._build_edit_form.assert_called_once_with(None)

    def test_delete_inhibits_mounts_until_confirmation_returns(self):
        cls = methods(MAIN, "MainWindow", {"_delete_connection"})
        page = cls()
        page._workers = {}
        page._deleting_connections = set()
        def confirm(conn_id):
            self.assertIn(conn_id, page._deleting_connections)
            raise RuntimeError("confirmation interrupted")
        page._confirm_delete_connection = confirm
        with self.assertRaises(RuntimeError):
            page._delete_connection("id")
        self.assertEqual(page._deleting_connections, set())

    def test_users_refresh_does_not_toggle_home(self):
        cls = methods(MAIN, "MainWindow", {"_open_users_panel"})
        page = mock.Mock()
        page._guard_leave_form.return_value = True
        page._panel_mode = "users"
        page._panel_conn_id = None
        cls._open_users_panel(page, refresh=True)
        page._nav_home.assert_not_called()
        page._build_users_form.assert_called_once()
        page._main_stack.setCurrentIndex.assert_called_once_with(1)

    def test_users_sidebar_still_toggles_home(self):
        cls = methods(MAIN, "MainWindow", {"_open_users_panel"})
        page = mock.Mock()
        page._guard_leave_form.return_value = True
        page._panel_mode = "users"
        cls._open_users_panel(page)
        page._nav_home.assert_called_once()
        page._build_users_form.assert_not_called()

    def test_discard_settings_restores_saved_theme(self):
        cls = methods(MAIN, "MainWindow", {"_guard_leave_form"})
        page = mock.Mock()
        page._leave_guard_active = False
        page._shutdown_requested = False
        page._panel_mode = "settings"
        page._settings_form_is_dirty.return_value = True
        page._ask_settings_dirty_action.return_value = "discard"
        page._mgr.get_settings.return_value = AppSettings(theme="ocean")
        manager = mock.Mock()
        with mock.patch("src.theme_manager.get_theme_manager", return_value=manager):
            self.assertTrue(cls._guard_leave_form(page))
        manager.apply_theme.assert_called_once_with(None, "ocean")
        page._save_settings_form.assert_not_called()
        self.assertFalse(page._leave_guard_active)


class DialogBase:
    def reject(self):
        self.base_rejected = True
    def done(self, result):
        self.base_result = result
    def closeEvent(self, event):
        event.accept()


class CloseLifecycleTests(unittest.TestCase):
    def test_idle_close_flushes_one_edit_before_final_close(self):
        cls = methods(BROWSER, "SftpBrowserWindow", {"_finish_close_when_idle"})
        page = cls()
        page._workers = []
        page._close_requested = True
        page._pending_transfer_results = set()
        page._disconnect_thread = None
        page._disconnect_done = mock.Mock()
        page._disconnect_done.is_set.return_value = True
        page._scan_close_edits = mock.Mock(return_value=None)
        page._shutdown_recovery_reason = None
        page._edit_reconnecting = False
        page._edit_reconnect_failed = False
        page._close_pending_edits = [("local", "/remote")]
        page._upload_edited_file = mock.Mock()
        page._close_wait_timer = mock.Mock()
        page.close = mock.Mock()
        page._finish_close_when_idle()
        page._upload_edited_file.assert_called_once_with("local", "/remote")
        page.close.assert_not_called()
        page._disconnect_thread = object()
        page._finish_close_when_idle()
        page.close.assert_called_once()

    def test_flush_failure_cancels_close_and_preserves_retry(self):
        worker = mock.Mock()
        fake_os = mock.Mock()
        fake_os.path.exists.return_value = True
        fake_os.path.basename.return_value = "local"
        messages = mock.Mock()
        cls = methods(BROWSER, "SftpBrowserWindow", {"_upload_edited_file", "_abort_shutdown", "cancel_shutdown"},
                      os=fake_os, SftpUploadWorker=mock.Mock(return_value=worker),
                      StyledMessageBox=messages)
        page = mock.Mock()
        page._close_requested = True
        page._close_ready = False
        page._disconnect_thread = None
        page._edit_reconnecting = False
        page._edit_reconnect_failed = False
        page._edit_uploads = set()
        page._edit_timers = {}
        page._edit_uploaded_versions = {}
        page._edit_version = mock.Mock(return_value=(1, 1, 1, 0))
        page._queue_edit = lambda lp, rp: page._close_pending_edits.append((lp, rp))
        page._abort_shutdown = lambda *args: cls._abort_shutdown(page, *args)
        page.cancel_shutdown = lambda: cls.cancel_shutdown(page)
        page._close_pending_edits = []
        page._edit_map = {"local": "/remote"}
        page._watcher.files.return_value = []
        cls._upload_edited_file(page, "local", "/remote")
        on_error = worker.error.connect.call_args.args[0]
        on_error("network failure")
        self.assertFalse(page._close_requested)
        self.assertEqual(page._close_pending_edits, [("local", "/remote")])
        self.assertEqual(page._edit_map, {"local": "/remote"})
        page._close_wait_timer.stop.assert_called_once()
        page._fdlg_content.setEnabled.assert_called_once_with(True)
        messages.critical.assert_called_once()

    def test_titlebar_reject_enters_close_gate(self):
        cls = methods(BROWSER, "SftpBrowserWindow", {"reject", "done"}, base=DialogBase)
        page = cls()
        page._close_ready = False
        page.close = mock.Mock()
        page.reject()
        page.done(0)
        self.assertEqual(page.close.call_count, 2)
        self.assertFalse(hasattr(page, "base_rejected"))
        page._close_ready = True
        page.reject()
        page.done(0)
        self.assertTrue(page.base_rejected)
        self.assertEqual(page.base_result, 0)

    def test_idle_close_waits_for_running_worker(self):
        cls = methods(BROWSER, "SftpBrowserWindow", {"_finish_close_when_idle"})
        page = cls()
        page.close = mock.Mock()
        page._close_wait_timer = mock.Mock()
        page._close_requested = True
        page._pending_transfer_results = set()
        page._disconnect_thread = object()
        page._disconnect_done = mock.Mock()
        page._disconnect_done.is_set.return_value = True
        page._scan_close_edits = mock.Mock(return_value=None)
        page._shutdown_recovery_reason = None
        worker = mock.Mock()
        worker.wait.return_value = True
        worker.isRunning.return_value = True
        page._workers = [worker]
        cls._finish_close_when_idle(page)
        page.close.assert_not_called()
        worker.isRunning.return_value = False
        cls._finish_close_when_idle(page)
        page.close.assert_called_once()
        self.assertTrue(page._close_ready)


class AutomaticRemountTests(unittest.TestCase):
    def make_page(self, conn):
        worker_class = mock.Mock()
        cls = methods(MAIN, "MainWindow", {"_start_automatic_remount"}, MountWorker=worker_class)
        page = cls()
        page._workers = {}
        page._lost_mounts = {"id": (0, 0.0)}
        page._automatic_remounts = set()
        page._controller = object()
        page._mgr = mock.Mock()
        page._mgr.get_by_id.return_value = conn
        page._mgr.get_settings.return_value = AppSettings()
        page._on_mount_finished = mock.Mock()
        page._track_background_worker = mock.Mock()
        return page, worker_class

    def test_remount_starts_once_with_backoff(self):
        page, workers = self.make_page(Connection("name", "host", "user", password="secret"))
        with mock.patch("time.monotonic", return_value=100.0):
            page._start_automatic_remount("id")
            page._start_automatic_remount("id")
        workers.assert_called_once()
        workers.return_value.start.assert_called_once()
        self.assertEqual(page._lost_mounts["id"], (1, 115.0))
        self.assertIn("id", page._automatic_remounts)

    def test_background_retry_is_deferred_during_same_connection_edit(self):
        page, workers = self.make_page(Connection("name", "host", "user", password="secret"))
        page._panel_mode = "edit"
        page._panel_conn_id = "id"
        page._start_automatic_remount("id")
        workers.assert_not_called()
        self.assertIn("id", page._lost_mounts)

    def test_remount_does_not_start_during_delete_confirmation(self):
        page, workers = self.make_page(Connection("name", "host", "user", password="secret"))
        page._deleting_connections = {"id"}
        page._start_automatic_remount("id")
        workers.assert_not_called()
        self.assertIn("id", page._lost_mounts)

    def test_future_due_does_not_start_worker(self):
        page, workers = self.make_page(Connection("name", "host", "user", password="secret"))
        page._lost_mounts["id"] = (2, 200.0)
        with mock.patch("time.monotonic", return_value=100.0):
            page._start_automatic_remount("id")
        workers.assert_not_called()

    def test_background_retry_does_not_prompt_for_missing_or_ask_auth(self):
        for conn in (None, Connection("n", "h", "u", auth_method="ask"), Connection("n", "h", "u")):
            with self.subTest(conn=conn):
                page, workers = self.make_page(conn)
                page._start_automatic_remount("id")
                workers.assert_not_called()
                self.assertNotIn("id", page._lost_mounts)


class SettingsIntegrationTests(unittest.TestCase):
    def page(self):
        cls = methods(MAIN, "MainWindow", {"_save_settings_form"}, current_language=lambda: "en")
        page = mock.Mock()
        page._sf_term_putty.isChecked.return_value = False
        page._sf_term_xterm.isChecked.return_value = True
        for name in ("_sf_start", "_sf_admin", "_sf_tray", "_sf_auto_reconnect", "_sf_auto_remount", "_sf_debug", "_sf_sshfs_disable_cache"):
            getattr(page, name).isChecked.return_value = False
        page._sf_lang.currentData.return_value = "en"
        page._sf_theme.currentData.return_value = "dark"
        page._sf_putty_path.text.return_value = ""
        page._sf_get_interval_seconds.return_value = 30
        page._mgr.get_settings.return_value = AppSettings()
        return cls, page

    def test_os_failure_does_not_persist_or_report_success(self):
        cls, page = self.page()
        with mock.patch("src.startup_settings.apply_startup_preferences", side_effect=RuntimeError("denied")):
            self.assertFalse(cls._save_settings_form(page))
        page._mgr.save_settings.assert_not_called()
        page._apply_settings_object.assert_not_called()
        page._err_popup.assert_called_once()

    def test_database_failure_rolls_back_os_preferences(self):
        cls, page = self.page()
        page._mgr.save_settings.side_effect = RuntimeError("database failure")
        with mock.patch("src.startup_settings.apply_startup_preferences") as apply:
            self.assertFalse(cls._save_settings_form(page))
        apply.assert_called_once()
        apply.return_value.assert_called_once_with()
        page._set_status.assert_not_called()


class TerminalBackendTests(unittest.TestCase):
    def launcher(self):
        path = ROOT / "src/ssh_launcher.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "launch_ssh_terminal")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
        ns = {"logger": mock.Mock(), "_launch_putty": mock.Mock(return_value=(True, "")),
              "_launch_native_ssh": mock.Mock(return_value=(True, ""))}
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
        return ns

    def test_putty_accepts_ppk_without_openssh_key(self):
        ns = self.launcher()
        conn = Connection("n", "h", "u", auth_method="key", putty_key_path="C:/key.ppk")
        result = ns["launch_ssh_terminal"](conn, AppSettings(use_putty=True, putty_path="putty.exe"))
        self.assertTrue(result[0])
        ns["_launch_putty"].assert_called_once()
        ns["_launch_native_ssh"].assert_not_called()

    def test_native_ssh_does_not_silently_drop_ppk_only_identity(self):
        ns = self.launcher()
        conn = Connection("n", "h", "u", auth_method="key", putty_key_path="C:/key.ppk")
        self.assertFalse(ns["launch_ssh_terminal"](conn, AppSettings(use_putty=False))[0])
        ns["_launch_native_ssh"].assert_not_called()

    def test_native_ssh_rejects_ppk_in_openssh_field(self):
        ns = self.launcher()
        conn = Connection("n", "h", "u", auth_method="key", key_path="C:/key.ppk")
        self.assertFalse(ns["launch_ssh_terminal"](conn, AppSettings(use_putty=False))[0])
        ns["_launch_native_ssh"].assert_not_called()


class TranslationAndDialogContractTests(unittest.TestCase):
    def test_new_labels_exist_in_every_translation(self):
        keys = {"window.minimize", "window.maximize", "window.restore", "window.close", "dialog.reset", "error.copy", "prereq.dialog.optional_text"}
        for path in (ROOT / "src/translations").glob("*.json"):
            with self.subTest(language=path.stem):
                values = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(keys <= values.keys())
                self.assertIn("{missing}", values["prereq.dialog.optional_text"])

    def test_destructive_style_is_explicit_not_language_heuristic(self):
        path = ROOT / "src/ui/dialogs/styled_message_box.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StyledMessageBox")
        question = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "question")
        self.assertIn("destructive", [n.arg for n in question.args.kwonlyargs])
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr == "lower" for n in ast.walk(cls)))


if __name__ == "__main__":
    unittest.main()
