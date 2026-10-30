"""Run with: python -m unittest discover -s tests -p test_startup_settings.py

Only stdlib imports. The registry is ALWAYS replaced with an in-memory fake;
bootstrap files live in TemporaryDirectory, never the real user's config dir.
"""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from src import startup_settings as settings


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self):
        self.values = {}
        self.fail_once = None
        self.fail_always = None
        self.closed = []
        self.opened = []

    def CreateKeyEx(self, hive, path, reserved, access):
        if hive != self.HKEY_CURRENT_USER:
            raise AssertionError("Only HKCU is allowed")
        self.opened.append(path)
        return path

    def QueryValueEx(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[key, name]

    def _fail(self, key, name):
        if self.fail_always == (key, name):
            raise PermissionError("persistent registry failure")
        if self.fail_once == (key, name):
            self.fail_once = None
            raise PermissionError("registry write denied")

    def SetValueEx(self, key, name, reserved, kind, value):
        self._fail(key, name)
        self.values[key, name] = (value, kind)

    def DeleteValue(self, key, name):
        self._fail(key, name)
        if (key, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[key, name]

    def CloseKey(self, key):
        self.closed.append(key)


class StartupSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "startup_preferences.json"
        self.registry = FakeRegistry()
        for patcher in (
            mock.patch.object(settings, "bootstrap_preferences_path", return_value=self.path),
            mock.patch.object(settings, "winreg", self.registry),
            mock.patch.object(settings, "_is_windows", return_value=True),
            mock.patch.object(sys, "frozen", False, create=True),
            mock.patch.object(sys, "executable", str(self.directory / "Python Space" / "python.exe")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def frozen(self):
        executable = str(self.directory / "App Space" / "SSHDriveMgr.exe")
        for patcher in (mock.patch.object(sys, "frozen", True),
                        mock.patch.object(sys, "executable", executable)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return str(Path(executable).resolve())

    def write_json(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_defaults_do_not_require_admin(self):
        self.assertEqual(settings.read_bootstrap_preferences(), {"require_admin": False})

    def test_legacy_fallback_only_until_new_file_saved(self):
        legacy = self.directory / "config.json"
        self.write_json(legacy, {"settings": {"require_admin": True}, "password": "never copy"})
        self.assertTrue(settings.read_bootstrap_preferences()["require_admin"])
        settings.apply_startup_preferences(False, False)
        self.assertFalse(settings.read_bootstrap_preferences()["require_admin"])
        self.assertEqual(json.loads(self.path.read_text()), {"version": 1, "require_admin": False})

    def test_invalid_new_file_does_not_resurrect_legacy(self):
        self.write_json(self.directory / "config.json", {"settings": {"require_admin": True}})
        for payload in ("invalid JSON", '[]', '{"require_admin": "true"}', '{"require_admin": 1}'):
            self.path.write_text(payload, encoding="utf-8")
            self.assertFalse(settings.read_bootstrap_preferences()["require_admin"])

    def test_malformed_legacy_defaults_false(self):
        for data in ([], {"settings": []}, {"settings": {"require_admin": "false"}}):
            self.write_json(self.directory / "config.json", data)
            self.assertFalse(settings.read_bootstrap_preferences()["require_admin"])

    def test_source_autostart_includes_absolute_main_script(self):
        settings.apply_startup_preferences(True, True)
        command, kind = self.registry.values[settings.RUN_KEY, settings.RUN_VALUE]
        expected_script = str(Path(settings.__file__).resolve().parent.parent / "main.py")
        self.assertEqual(command, '"' + sys.executable + '" ' + subprocess.list2cmdline([expected_script]))
        self.assertEqual(kind, self.registry.REG_SZ)
        self.assertNotIn(settings.LAYERS_KEY, self.registry.opened)
        self.assertTrue(settings.read_bootstrap_preferences()["require_admin"])

    def test_frozen_command_quotes_executable_without_script(self):
        executable = self.frozen()
        settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values[settings.RUN_KEY, settings.RUN_VALUE],
                         ('"' + executable + '"', self.registry.REG_SZ))
        # A RUNASADMIN EXE can be suppressed by Windows HKCU Run startup.
        self.assertNotIn((settings.LAYERS_KEY, executable), self.registry.values)
        self.assertTrue(settings.read_bootstrap_preferences()["require_admin"])

    def test_disabling_autostart_deletes_only_owned_value(self):
        self.registry.values[settings.RUN_KEY, settings.RUN_VALUE] = ("old", 2)
        self.registry.values[settings.RUN_KEY, "OtherApp"] = ("untouched", 1)
        settings.apply_startup_preferences(False, False)
        self.assertNotIn((settings.RUN_KEY, settings.RUN_VALUE), self.registry.values)
        self.assertEqual(self.registry.values[settings.RUN_KEY, "OtherApp"], ("untouched", 1))

    def test_absent_values_can_be_disabled_repeatedly(self):
        self.frozen()
        settings.apply_startup_preferences(False, False)
        settings.apply_startup_preferences(False, False)
        self.assertEqual(self.registry.values, {})

    def test_legacy_admin_layer_removed_preserving_other_flags(self):
        executable = self.frozen()
        self.registry.values[settings.LAYERS_KEY, executable] = ("~ WIN8RTM HIGHDPIAWARE RUNASADMIN", 1)
        settings.apply_startup_preferences(False, True)
        self.assertEqual(self.registry.values[settings.LAYERS_KEY, executable],
                         ("~ WIN8RTM HIGHDPIAWARE", 1))
        settings.apply_startup_preferences(False, False)
        self.assertEqual(self.registry.values[settings.LAYERS_KEY, executable],
                         ("~ WIN8RTM HIGHDPIAWARE", 1))

    def test_remove_case_insensitive_admin_flag_and_empty_marker(self):
        executable = self.frozen()
        self.registry.values[settings.LAYERS_KEY, executable] = ("~ runasadmin RUNASADMIN", 1)
        settings.apply_startup_preferences(False, False)
        self.assertNotIn((settings.LAYERS_KEY, executable), self.registry.values)

    def test_shared_python_executable_never_gets_layers(self):
        self.frozen()
        with mock.patch.object(sys, "executable", str(self.directory / "pythonw.exe")):
            settings.apply_startup_preferences(False, True)
        self.assertNotIn(settings.LAYERS_KEY, self.registry.opened)

    def test_non_exe_frozen_launch_never_gets_layers(self):
        self.frozen()
        with mock.patch.object(sys, "executable", str(self.directory / "app.bin")):
            settings.apply_startup_preferences(False, True)
        self.assertNotIn(settings.LAYERS_KEY, self.registry.opened)

    def test_unknown_layer_type_not_overwritten(self):
        executable = self.frozen()
        self.registry.values[settings.LAYERS_KEY, executable] = (b"binary", 3)
        original = dict(self.registry.values)
        with self.assertRaisesRegex(settings.StartupPreferencesError, "refusing to overwrite"):
            settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values, original)
        self.assertFalse(self.path.exists())

    def test_second_registry_write_failure_rolls_back_first(self):
        executable = self.frozen()
        self.registry.values[settings.RUN_KEY, settings.RUN_VALUE] = ("old command", 2)
        self.registry.values[settings.LAYERS_KEY, executable] = ("~ WIN8RTM RUNASADMIN", 1)
        original = dict(self.registry.values)
        self.registry.fail_once = (settings.LAYERS_KEY, executable)
        with self.assertRaisesRegex(settings.StartupPreferencesError, "elevation flags"):
            settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values, original)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.registry.closed), 2)

    def test_first_registry_failure_does_not_save_bootstrap(self):
        self.registry.fail_once = (settings.RUN_KEY, settings.RUN_VALUE)
        with self.assertRaisesRegex(settings.StartupPreferencesError, "autostart"):
            settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values, {})
        self.assertFalse(self.path.exists())

    def test_file_failure_rolls_back_registry_and_keeps_old_json(self):
        self.frozen()
        self.write_json(self.path, {"version": 1, "require_admin": False})
        original = self.path.read_bytes()
        with mock.patch.object(settings.os, "replace", side_effect=PermissionError("replace denied")):
            with self.assertRaisesRegex(settings.StartupPreferencesError, "pre-login"):
                settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values, {})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_rollback_failure_is_visible_to_caller(self):
        self.frozen()
        self.registry.fail_always = (settings.RUN_KEY, settings.RUN_VALUE)
        with self.assertRaisesRegex(settings.StartupPreferencesError, "rollback also failed"):
            settings.apply_startup_preferences(True, True)

    def test_atomic_save_uses_same_directory_and_no_secrets(self):
        replace = settings.os.replace
        with mock.patch.object(settings.os, "replace", wraps=replace) as replacement:
            settings.apply_startup_preferences(False, True)
        source, target = replacement.call_args.args
        self.assertEqual(Path(source).parent, self.path.parent)
        self.assertEqual(target, self.path)
        self.assertEqual(set(json.loads(self.path.read_text())), {"version", "require_admin"})

    def test_non_windows_saves_file_without_registry(self):
        with mock.patch.object(settings, "_is_windows", return_value=False):
            settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.opened, [])
        self.assertTrue(settings.read_bootstrap_preferences()["require_admin"])

    def test_non_boolean_preferences_are_rejected(self):
        with self.assertRaises(TypeError):
            settings.apply_startup_preferences("false", False)
        self.assertFalse(self.path.exists())

    def test_unsafe_executable_path_is_rejected(self):
        with mock.patch.object(sys, "executable", 'bad"name.exe'):
            with self.assertRaises(settings.StartupPreferencesError):
                settings.apply_startup_preferences(True, False)
        self.assertFalse(self.path.exists())

    def test_caller_rollback_restores_exact_windows_user_snapshot(self):
        executable = self.frozen()
        original_bytes = b'{ "require_admin" : false, "version": 1 }\r\n'
        self.path.write_bytes(original_bytes)
        self.registry.values[settings.RUN_KEY, settings.RUN_VALUE] = ("%LOCALAPPDATA%\\old.exe", 2)
        self.registry.values[settings.LAYERS_KEY, executable] = ("~   WIN8RTM runasadmin", 1)
        self.registry.values[settings.RUN_KEY, "OtherApp"] = ("untouched", 1)
        original_registry = dict(self.registry.values)
        rollback = settings.apply_startup_preferences(True, True)
        self.assertTrue(callable(rollback))
        self.assertNotEqual(self.path.read_bytes(), original_bytes)
        self.assertNotEqual(self.registry.values, original_registry)
        closed_after_apply = len(self.registry.closed)
        rollback()
        self.assertEqual(self.registry.values, original_registry)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(len(self.registry.closed), closed_after_apply + 2)

    def test_caller_rollback_restores_absent_bootstrap_and_run_value(self):
        rollback = settings.apply_startup_preferences(True, True)
        self.assertTrue(self.path.exists())
        rollback()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.registry.values, {})

    def test_caller_rollback_uses_captured_paths_not_current_preferences(self):
        executable = self.frozen()
        self.registry.values[settings.LAYERS_KEY, executable] = ("~ RUNASADMIN", 1)
        rollback = settings.apply_startup_preferences(True, True)
        another_path = self.directory / "another-account.json"
        another_path.write_bytes(b"other account")
        with mock.patch.object(settings, "bootstrap_preferences_path", return_value=another_path), \
             mock.patch.object(sys, "executable", str(self.directory / "different.exe")):
            rollback()
        self.assertEqual(another_path.read_bytes(), b"other account")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.registry.values[settings.LAYERS_KEY, executable], ("~ RUNASADMIN", 1))

    def test_successful_caller_rollback_is_idempotent(self):
        rollback = settings.apply_startup_preferences(True, True)
        rollback()
        # An accidental second call must not undo a later successful save.
        self.path.write_bytes(b"later save")
        self.registry.values[settings.RUN_KEY, settings.RUN_VALUE] = ("later command", 1)
        rollback()
        self.assertEqual(self.path.read_bytes(), b"later save")
        self.assertEqual(self.registry.values[settings.RUN_KEY, settings.RUN_VALUE], ("later command", 1))

    def test_unreadable_bootstrap_snapshot_fails_before_registry_writes(self):
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("snapshot denied")):
            with self.assertRaisesRegex(settings.StartupPreferencesError, "snapshotting.*snapshot denied"):
                settings.apply_startup_preferences(True, True)
        self.assertEqual(self.registry.values, {})
        self.assertEqual(self.registry.opened, [])
        self.assertFalse(self.path.exists())

    def test_caller_rollback_registry_failure_is_reported_and_retryable(self):
        original_bytes = b"original bytes, even if not valid JSON"
        self.path.write_bytes(original_bytes)
        rollback = settings.apply_startup_preferences(True, True)
        self.registry.fail_once = (settings.RUN_KEY, settings.RUN_VALUE)
        with self.assertRaisesRegex(settings.StartupPreferencesError, "rollback failed.*registry write denied"):
            rollback()
        # Restore the file even when a registry restoration fails.
        self.assertEqual(self.path.read_bytes(), original_bytes)
        rollback()
        self.assertEqual(self.registry.values, {})
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_caller_rollback_file_failure_is_reported_and_retryable(self):
        self.path.write_bytes(b"original exact bytes")
        rollback = settings.apply_startup_preferences(True, True)
        with mock.patch.object(settings.os, "replace", side_effect=PermissionError("restore denied")):
            with self.assertRaisesRegex(settings.StartupPreferencesError, "bootstrap preference restoration"):
                rollback()
        self.assertEqual(self.registry.values, {})
        rollback()
        self.assertEqual(self.path.read_bytes(), b"original exact bytes")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_non_windows_caller_rollback_restores_original_bytes(self):
        self.path.write_bytes(b"original non-Windows preference")
        with mock.patch.object(settings, "_is_windows", return_value=False):
            rollback = settings.apply_startup_preferences(True, True)
        rollback()
        self.assertEqual(self.path.read_bytes(), b"original non-Windows preference")
        self.assertEqual(self.registry.opened, [])

    def test_elevation_source_arguments_include_script_and_loop_guard(self):
        args = ['--connect', 'a "quoted" value', 'C:\\folder with space\\', '--no-elevate']
        script = str(Path(settings.__file__).resolve().parent.parent / "main.py")
        self.assertEqual(settings.elevation_launch_arguments(args),
                         subprocess.list2cmdline([script] + args[:-1] + ['--no-elevate']))

    def test_elevation_frozen_arguments_do_not_include_script(self):
        self.frozen()
        self.assertEqual(settings.elevation_launch_arguments(['hello world']),
                         '"hello world" --no-elevate')


class MainBootstrapTests(unittest.TestCase):
    """Extract small bootstrap functions via AST, without Qt or app imports."""

    def setUp(self):
        path = Path(__file__).resolve().parent.parent / "main.py"
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = [node for node in self.tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("_is_admin", "_request_elevation")]
        self.shell = types.SimpleNamespace(IsUserAnAdmin=mock.Mock(return_value=0),
                                          ShellExecuteW=mock.Mock(return_value=42))
        self.namespace = {"sys": sys, "Path": Path, "__file__": str(path),
                          "ctypes": types.SimpleNamespace(windll=types.SimpleNamespace(shell32=self.shell))}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), self.namespace)

    def test_no_elevation_when_bootstrap_disabled(self):
        with mock.patch.object(sys, "argv", ["main.py"]), \
             mock.patch.object(settings, "read_bootstrap_preferences", return_value={"require_admin": False}):
            self.assertFalse(self.namespace["_request_elevation"]())
        self.shell.ShellExecuteW.assert_not_called()

    def test_source_elevation_passes_main_and_loop_guard(self):
        with mock.patch.object(sys, "argv", ["main.py", "--some-option"]), \
             mock.patch.object(sys, "frozen", False, create=True), \
             mock.patch.object(settings, "read_bootstrap_preferences", return_value={"require_admin": True}):
            self.assertTrue(self.namespace["_request_elevation"]())
        params = self.shell.ShellExecuteW.call_args.args[3]
        self.assertIn("main.py", params)
        self.assertIn("--some-option --no-elevate", params)

    def test_elevated_process_and_guard_do_not_relaunch(self):
        self.shell.IsUserAnAdmin.return_value = 1
        self.assertFalse(self.namespace["_request_elevation"]())
        self.shell.IsUserAnAdmin.return_value = 0
        with mock.patch.object(sys, "argv", ["main.py", "--no-elevate"]):
            self.assertFalse(self.namespace["_request_elevation"]())
        self.shell.ShellExecuteW.assert_not_called()

    def test_cancelled_uac_continues_non_admin(self):
        self.shell.ShellExecuteW.return_value = 5
        with mock.patch.object(sys, "argv", ["main.py"]), \
             mock.patch.object(settings, "read_bootstrap_preferences", return_value={"require_admin": True}):
            self.assertFalse(self.namespace["_request_elevation"]())

    def test_elevation_precedes_mutex_and_database(self):
        main = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = {node.func.id: node.lineno for node in ast.walk(main)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id in ("_request_elevation", "ensure_single_instance", "init_db")}
        self.assertLess(calls["_request_elevation"], calls["ensure_single_instance"])
        self.assertLess(calls["ensure_single_instance"], calls["init_db"])
        self.assertFalse(any(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                             and isinstance(node.value.func, ast.Name)
                             and node.value.func.id == "ensure_single_instance" for node in self.tree.body))

    def test_missing_mount_prerequisites_are_not_fatal(self):
        main = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        gate = next(node for node in main.body if isinstance(node, ast.If)
                    and any(isinstance(child, ast.Name) and child.id == "_prereq" for child in ast.walk(node.test)))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                             and isinstance(node.func.value, ast.Name) and node.func.value.id == "sys"
                             and node.func.attr == "exit" for node in ast.walk(gate)))
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "FramelessDialog" for node in ast.walk(gate)))


if __name__ == "__main__":
    unittest.main()
