"""Per-Windows-user IPC namespace regressions; no real token/pipe/credentials.

Every native call is fake. Client production definitions are AST-loaded rather
than importing GUI/SSH application modules. No Qt or pywin32 is required.
"""
import ast
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_identity():
    spec = importlib.util.spec_from_file_location("isolated_windows_identity", ROOT / "src/windows_identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []
    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


class NamespaceTests(unittest.TestCase):
    def setUp(self):
        self.identity = load_identity()

    def test_same_user_and_elevated_token_use_same_version_independent_endpoint(self):
        sid = "S-1-5-21-111-222-333-1001"
        with mock.patch.object(self.identity, "current_user_sid", return_value=sid):
            medium = self.identity.ipc_pipe_name()
            with mock.patch.dict(os.environ, {"USERNAME": "Administrator", "APP_VERSION": "999", "USER": "other"}):
                elevated = self.identity.ipc_pipe_name()
        self.assertEqual(medium, elevated)
        self.assertEqual(medium, r"\\.\pipe\SSHDriveMgr_IPC_v1_" + hashlib.sha256(sid.encode("ascii")).hexdigest())
        self.assertNotIn(sid, medium)

    def test_other_windows_user_gets_different_pipe(self):
        with mock.patch.object(self.identity, "current_user_sid", side_effect=[
            "S-1-5-21-111-222-333-1001", "S-1-5-21-111-222-333-1002",
        ]):
            self.assertNotEqual(self.identity.ipc_pipe_name(), self.identity.ipc_pipe_name())

    def test_identity_failure_never_falls_back_to_fixed_or_env_pipe(self):
        with mock.patch.object(self.identity, "current_user_sid", side_effect=OSError("identity unavailable")):
            with self.assertRaisesRegex(OSError, "identity unavailable"):
                self.identity.ipc_pipe_name()

    def test_helper_has_no_qt_pywin32_or_app_imports(self):
        tree = ast.parse((ROOT / "src/windows_identity.py").read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module)
        self.assertEqual(set(modules), {"ctypes", "hashlib", "re"})

    def test_every_client_imports_shared_helper_and_has_no_fixed_pipe_literal(self):
        for relative in ("cli_main.py", "main.py", "src/ssh_launcher.py"):
            with self.subTest(path=relative):
                tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "src.windows_identity"
                                    and any(alias.name == "ipc_pipe_name" for alias in node.names)
                                    for node in ast.walk(tree)))
                self.assertFalse(any(isinstance(node, ast.Constant) and isinstance(node.value, str)
                                     and node.value.startswith(r"\\.\pipe\SSHDriveMgr_IPC")
                                     for node in ast.walk(tree)))

    @unittest.skipUnless(sys.platform == "win32", "Windows ctypes client contracts")
    def test_all_three_actual_clients_use_exact_same_per_user_pipe(self):
        with mock.patch.object(self.identity, "current_user_sid", return_value="S-1-5-21-111-222-333-1001"):
            expected = self.identity.ipc_pipe_name()
            kernel = SimpleNamespace()
            for name in ("WaitNamedPipeW", "CreateFileW", "SetNamedPipeHandleState", "WriteFile", "ReadFile", "CloseHandle"):
                setattr(kernel, name, FakeFunction(lambda *args: False))
            kernel.CreateFileW.callback = lambda *args: wintypes.HANDLE(-1).value
            with mock.patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel)), \
                    mock.patch.dict(sys.modules, {"src.windows_identity": self.identity}):
                for relative, function in (("cli_main.py", "_handle_cli_connect"),
                                           ("src/ssh_launcher.py", "_send_cli_log")):
                    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
                    namespace = {"ctypes": ctypes}
                    exec(compile(ast.Module(body=[node], type_ignores=[]), relative, "exec"), namespace)
                    with contextlib.redirect_stdout(io.StringIO()):
                        namespace[function]("bearer") if function == "_handle_cli_connect" else namespace[function]("bearer", {})
                tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
                branch = next(n for n in tree.body if isinstance(n, ast.If)
                              and any(isinstance(c, ast.Constant) and c.value == "--pass-helper" for c in ast.walk(n.test)))
                with mock.patch.dict(os.environ, {"SSH_ASKPASS_TOKEN": "one-time-token"}):
                    with self.assertRaises(SystemExit) as exc:
                        exec(compile(ast.Module(body=branch.body, type_ignores=[]), "main.py", "exec"), {"sys": sys, "os": os})
                self.assertEqual(exc.exception.code, 0)
            self.assertEqual([call[0] for call in kernel.WaitNamedPipeW.calls], [expected, expected])
            self.assertEqual([call[0] for call in kernel.CreateFileW.calls], [expected])


@unittest.skipUnless(sys.platform == "win32", "Windows native ctypes API contract")
class NativeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = m = load_identity()
        self.token_handle = 0x1234567887654321
        self.user_sid_text = "S-1-5-21-111-222-333-1001"
        self.attributes = 0
        self.returned_length = ctypes.sizeof(m._SID_AND_ATTRIBUTES) + 32
        self.text_buffer = None
        def open_token(process, access, output):
            ctypes.cast(output, ctypes.POINTER(wintypes.HANDLE))[0] = self.token_handle
            return True
        def token_info(token, info_class, buffer, length, output_length):
            ctypes.cast(output_length, ctypes.POINTER(wintypes.DWORD))[0] = self.returned_length
            if buffer is None:
                ctypes.set_last_error(122)
                return False
            user = m._SID_AND_ATTRIBUTES.from_buffer(buffer)
            user.Sid = 0x1234567890
            user.Attributes = self.attributes
            return True
        def convert(sid, output):
            self.text_buffer = ctypes.create_unicode_buffer(self.user_sid_text)
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.text_buffer)
            return True
        self.kernel = SimpleNamespace(
            GetCurrentProcess=FakeFunction(lambda: wintypes.HANDLE(-1).value),
            CloseHandle=FakeFunction(lambda token: True),
            LocalFree=FakeFunction(lambda pointer: None),
        )
        self.advapi = SimpleNamespace(
            OpenProcessToken=FakeFunction(open_token),
            GetTokenInformation=FakeFunction(token_info),
            IsValidSid=FakeFunction(lambda sid: True),
            ConvertSidToStringSidW=FakeFunction(convert),
        )
        patch = mock.patch.object(ctypes, "WinDLL", side_effect=lambda name, **kwargs: self.kernel if name == "kernel32" else self.advapi)
        patch.start()
        self.addCleanup(patch.stop)

    def test_pointer_sized_signatures_tokenuser_only_query_and_cleanup(self):
        self.assertEqual(self.identity.current_user_sid(), self.user_sid_text)
        self.assertIs(self.kernel.GetCurrentProcess.restype, wintypes.HANDLE)
        self.assertEqual(self.kernel.CloseHandle.argtypes, [wintypes.HANDLE])
        self.assertEqual(self.advapi.OpenProcessToken.calls[0][1], 0x0008)
        self.assertEqual([call[1] for call in self.advapi.GetTokenInformation.calls], [1, 1])
        self.assertEqual(self.kernel.CloseHandle.calls[0][0].value, self.token_handle)
        self.assertEqual(len(self.kernel.LocalFree.calls), 1)

    def test_same_tokenuser_different_elevation_attributes_keeps_endpoint(self):
        medium = self.identity.ipc_pipe_name()
        self.attributes = 0x20
        elevated = self.identity.ipc_pipe_name()
        self.assertEqual(medium, elevated)
        self.assertEqual(len(self.kernel.CloseHandle.calls), 2)

    def test_token_open_failure_no_fallback_or_invalid_close(self):
        self.advapi.OpenProcessToken.callback = lambda *args: False
        with self.assertRaises(OSError):
            self.identity.current_user_sid()
        self.assertFalse(self.kernel.CloseHandle.calls)
        self.assertFalse(self.advapi.GetTokenInformation.calls)

    def test_null_token_handle_rejected(self):
        self.token_handle = None
        with self.assertRaisesRegex(OSError, "invalid token handle"):
            self.identity.current_user_sid()
        self.assertFalse(self.advapi.GetTokenInformation.calls)

    def test_invalid_length_rejected_and_token_closed(self):
        for length in (0, 1, 2 * 1024 * 1024):
            with self.subTest(length=length):
                self.returned_length = length
                with self.assertRaisesRegex(OSError, "size"):
                    self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 3)

    def test_sizing_api_error_closes_token(self):
        self.advapi.GetTokenInformation.callback = lambda *args: False
        ctypes.set_last_error(5)
        with self.assertRaises(OSError):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)

    def test_second_token_information_error_closes_token(self):
        original = self.advapi.GetTokenInformation.callback
        self.advapi.GetTokenInformation.callback = lambda token, info, buffer, length, out: original(token, info, buffer, length, out) if buffer is None else False
        with self.assertRaises(OSError):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)
        self.assertFalse(self.advapi.ConvertSidToStringSidW.calls)

    def test_invalid_sid_closes_token_without_conversion(self):
        self.advapi.IsValidSid.callback = lambda *args: False
        with self.assertRaisesRegex(OSError, "invalid user SID"):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)
        self.assertFalse(self.advapi.ConvertSidToStringSidW.calls)

    def test_conversion_failure_and_null_pointer_fail_closed(self):
        for result in (False, True):
            with self.subTest(result=result):
                self.advapi.ConvertSidToStringSidW.callback = lambda *args: result
                with self.assertRaises(OSError):
                    self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 2)
        self.assertFalse(self.kernel.LocalFree.calls)

    def test_malformed_sid_string_freed_and_token_closed(self):
        self.user_sid_text = "untrusted\\pipe\\name"
        with self.assertRaisesRegex(OSError, "invalid SID string"):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.LocalFree.calls), 1)
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)

    def test_localfree_failure_still_closes_token(self):
        self.kernel.LocalFree.callback = lambda *args: 0x1234
        with self.assertRaises(OSError):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)

    def test_close_failure_propagates_after_sid_buffer_freed(self):
        self.kernel.CloseHandle.callback = lambda *args: False
        with self.assertRaises(OSError):
            self.identity.current_user_sid()
        self.assertEqual(len(self.kernel.LocalFree.calls), 1)


if __name__ == "__main__":
    unittest.main()
