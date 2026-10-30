"""Stdlib-only IPC ACL tests: no Qt, registry, appdata or Windows required."""

import ctypes
import gc
import struct
import sys
import types
import unittest
from unittest import mock

from src import ipc_security as security


SID = "S-1-5-21-123-456-789-1001"
DESCRIPTOR = struct.pack("<BBHIIII", 1, 0, 0x9004, 0, 0, 0, 20) + b"\x02\x00\x08\x00\x00\x00\x00\x00"


class NativeDouble:
    """Model LocalAlloc ownership and ctypes out parameters without Windows."""

    def __init__(self):
        self.allocation = ctypes.create_string_buffer(DESCRIPTOR, len(DESCRIPTOR))
        self.control = 0x9004
        self.revision = 1
        self.size = len(DESCRIPTOR)
        self.advapi = types.SimpleNamespace(
            ConvertStringSecurityDescriptorToSecurityDescriptorW=mock.Mock(side_effect=self.convert),
            IsValidSecurityDescriptor=mock.Mock(return_value=1),
            GetSecurityDescriptorControl=mock.Mock(side_effect=self.get_control),
            GetSecurityDescriptorLength=mock.Mock(return_value=len(DESCRIPTOR)),
        )
        self.kernel = types.SimpleNamespace(LocalFree=mock.Mock(side_effect=self.free))

    def convert(self, sddl, revision, descriptor, size):
        self.sddl = sddl
        self.sddl_revision = revision
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(self.allocation)
        ctypes.cast(size, ctypes.POINTER(security._DWORD))[0] = self.size
        return 1

    def get_control(self, descriptor, control, revision):
        ctypes.cast(control, ctypes.POINTER(security._WORD))[0] = self.control
        ctypes.cast(revision, ctypes.POINTER(security._DWORD))[0] = self.revision
        return 1

    def free(self, pointer):
        # Invalidate the original allocation to prove the returned bytes own a copy.
        ctypes.memset(pointer, 0, len(DESCRIPTOR))
        return None

    def patches(self):
        return mock.patch.object(
            security, "_load_windows_libraries", return_value=(self.advapi, self.kernel)
        )


class PipeAttributesTests(unittest.TestCase):
    def test_exact_protected_current_user_only_dacl_and_owned_buffer(self):
        with mock.patch.object(security, "_current_user_sid", return_value=SID), mock.patch.object(
            security, "_descriptor_from_sddl", return_value=DESCRIPTOR
        ) as convert:
            attributes = security.make_pipe_security_attributes()
        convert.assert_called_once_with("D:P(A;;GRGW;;;" + SID + ")")
        self.assertIsInstance(attributes, security.SECURITY_ATTRIBUTES)
        self.assertEqual(attributes.nLength, ctypes.sizeof(security.SECURITY_ATTRIBUTES))
        self.assertEqual(attributes.bInheritHandle, 0)
        self.assertEqual(ctypes.sizeof(security._BOOL), 4)
        self.assertEqual(ctypes.sizeof(security._DWORD), 4)
        self.assertEqual(security.SECURITY_ATTRIBUTES.bInheritHandle.size, 4)
        self.assertEqual(attributes.lpSecurityDescriptor, ctypes.addressof(attributes._descriptor_buffer))
        gc.collect()
        self.assertEqual(ctypes.string_at(attributes.lpSecurityDescriptor, len(DESCRIPTOR)), DESCRIPTOR)

    def test_malformed_sid_is_rejected_before_native_conversion(self):
        invalid = [
            None, b"S-1-5-21", "", "BA", "S-1-5", "S-2-5-21",
            "S-1-5-21 ", " S-1-5-21", "S-1-5-21\n", "S-1-5--1",
            "S-1-5-21)(A;;GA;;;WD", "S-1-5-+1", "S-1-5-4294967296",
            "S-1-281474976710656-1", "S-1-0x1000000000000-1",
            "S-1-5" + "-1" * 16,
        ]
        for sid in invalid:
            with self.subTest(sid=sid), mock.patch.object(
                security, "_current_user_sid", return_value=sid
            ), mock.patch.object(security, "_descriptor_from_sddl") as convert:
                with self.assertRaises(ValueError):
                    security.make_pipe_security_attributes()
                convert.assert_not_called()

    def test_sid_numeric_limits(self):
        for sid in [SID, "S-1-281474976710655-4294967295", "S-1-0xffffffffffff-1"]:
            self.assertEqual(security._validate_sid(sid), sid)

    def test_sid_lookup_failure_propagates_without_fallback(self):
        error = OSError("token denied")
        with mock.patch.object(security, "_current_user_sid", side_effect=error), mock.patch.object(
            security, "_descriptor_from_sddl"
        ) as convert:
            with self.assertRaises(OSError) as caught:
                security.make_pipe_security_attributes()
        self.assertIs(caught.exception, error)
        convert.assert_not_called()

    def test_default_native_backend_failure_propagates(self):
        error = OSError("native API unavailable")
        with mock.patch.object(security, "_current_user_sid", return_value=SID), mock.patch.object(
            security, "_load_windows_libraries", side_effect=error
        ):
            with self.assertRaises(OSError) as caught:
                security.make_pipe_security_attributes()
        self.assertIs(caught.exception, error)

    def test_empty_descriptor_never_returns_default_attributes(self):
        for descriptor in [None, b"", b"short", bytearray(DESCRIPTOR)]:
            with self.subTest(descriptor=descriptor), mock.patch.object(
                security, "_current_user_sid", return_value=SID
            ), mock.patch.object(security, "_descriptor_from_sddl", return_value=descriptor):
                with self.assertRaises(ValueError):
                    security.make_pipe_security_attributes()


class TokenLookupTests(unittest.TestCase):
    def setUp(self):
        self.token = mock.Mock()
        self.win32api = types.SimpleNamespace(GetCurrentProcess=mock.Mock(return_value="process"))
        self.win32con = types.SimpleNamespace(TOKEN_QUERY=8)
        self.win32security = types.SimpleNamespace(
            OpenProcessToken=mock.Mock(return_value=self.token),
            GetTokenInformation=mock.Mock(return_value=("sid object", 0)),
            ConvertSidToStringSid=mock.Mock(return_value=SID),
            TokenUser=1,
        )
        self.modules = mock.patch.dict(sys.modules, {
            "win32api": self.win32api, "win32con": self.win32con,
            "win32security": self.win32security,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_uses_process_token_user_and_closes_token(self):
        self.assertEqual(security._current_user_sid(), SID)
        self.win32security.OpenProcessToken.assert_called_once_with("process", 8)
        self.win32security.GetTokenInformation.assert_called_once_with(self.token, 1)
        self.win32security.ConvertSidToStringSid.assert_called_once_with("sid object")
        self.token.Close.assert_called_once_with()

    def test_all_token_failures_propagate(self):
        for operation in ("OpenProcessToken", "GetTokenInformation", "ConvertSidToStringSid"):
            with self.subTest(operation=operation):
                self.token.reset_mock()
                function = getattr(self.win32security, operation)
                function.side_effect = OSError("denied")
                try:
                    with self.assertRaises(OSError):
                        security._current_user_sid()
                finally:
                    function.side_effect = None
                self.assertEqual(self.token.Close.call_count, int(operation != "OpenProcessToken"))

    def test_token_close_failure_propagates(self):
        self.token.Close.side_effect = OSError("close failed")
        with self.assertRaises(OSError):
            security._current_user_sid()

    def test_missing_pywin32_is_not_silently_ignored(self):
        with mock.patch.dict(sys.modules, {"win32security": None}):
            with self.assertRaises(ImportError):
                security._current_user_sid()


class NativeConversionTests(unittest.TestCase):
    def test_conversion_is_typed_copied_and_freed(self):
        native = NativeDouble()
        with native.patches():
            descriptor = security._descriptor_from_sddl("D:P(A;;GRGW;;;" + SID + ")")
        self.assertEqual(descriptor, DESCRIPTOR)
        self.assertEqual(native.allocation.raw, bytes(len(DESCRIPTOR)))
        self.assertEqual(native.sddl_revision, 1)
        native.kernel.LocalFree.assert_called_once()
        self.assertIs(native.kernel.LocalFree.restype, ctypes.c_void_p)
        self.assertEqual(native.kernel.LocalFree.argtypes, [ctypes.c_void_p])
        self.assertIs(native.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype, security._BOOL)
        self.assertEqual(len(native.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes), 4)

    def test_native_api_failures_raise_and_free_allocations(self):
        for operation in (
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            "IsValidSecurityDescriptor", "GetSecurityDescriptorControl",
        ):
            native = NativeDouble()
            function = getattr(native.advapi, operation)
            if operation == "ConvertStringSecurityDescriptorToSecurityDescriptorW":
                function.side_effect = None  # Failure does not allocate memory.
            function.return_value = 0
            if operation == "GetSecurityDescriptorControl":
                function.side_effect = None
            with self.subTest(operation=operation), native.patches(), mock.patch.object(
                security, "_api_error", return_value=OSError("native failure")
            ):
                with self.assertRaises(OSError):
                    security._descriptor_from_sddl("ignored")
            self.assertEqual(native.kernel.LocalFree.call_count, int(operation != "ConvertStringSecurityDescriptorToSecurityDescriptorW"))

    def test_invalid_descriptor_metadata_is_rejected_and_freed(self):
        for field, value in [("control", 0x8004), ("control", 0x1004), ("control", 0x9000), ("revision", 2), ("size", 0), ("size", 20)]:
            native = NativeDouble()
            setattr(native, field, value)
            with self.subTest(field=field, value=value), native.patches():
                with self.assertRaises(ValueError):
                    security._descriptor_from_sddl("ignored")
            native.kernel.LocalFree.assert_called_once()

    def test_null_descriptor_is_rejected(self):
        native = NativeDouble()
        native.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = None
        native.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = 1
        with native.patches(), self.assertRaises(ValueError):
            security._descriptor_from_sddl("ignored")
        native.kernel.LocalFree.assert_not_called()

    def test_local_free_failure_raises(self):
        native = NativeDouble()
        native.kernel.LocalFree.side_effect = None
        native.kernel.LocalFree.return_value = 123
        with native.patches(), mock.patch.object(security, "_api_error", return_value=OSError("free failed")):
            with self.assertRaises(OSError):
                security._descriptor_from_sddl("ignored")

    def test_non_windows_default_loader_fails_closed(self):
        with mock.patch.object(security.os, "name", "posix"):
            with self.assertRaises(OSError):
                security._load_windows_libraries()


class ListenerIntegrationTests(unittest.TestCase):
    def test_acl_failure_never_creates_a_default_security_pipe(self):
        import ast
        import ctypes.wintypes
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "src/ui/main_window.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MainWindow")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_ipc_listener")
        kernel = mock.Mock()
        fake_ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=kernel), wintypes=ctypes.wintypes,
            c_void_p=ctypes.c_void_p, POINTER=ctypes.POINTER,
        )
        logger = mock.Mock()
        namespace = {"ctypes": fake_ctypes, "logger": logger}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), "exec"), namespace)
        listener = types.SimpleNamespace(_ipc_running=True)
        with mock.patch.object(security, "make_pipe_security_attributes", side_effect=OSError("ACL unavailable")):
            namespace["_ipc_listener"](listener)
        kernel.CreateNamedPipeW.assert_not_called()
        self.assertFalse(listener._ipc_running)
        logger.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
