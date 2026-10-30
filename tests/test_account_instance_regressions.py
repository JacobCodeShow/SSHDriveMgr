"""Isolated account/instance regressions: no app imports, real DB, or mutex.

Production definitions are AST-loaded to skip auth's import-time DB access.
SQLite :memory: and real AES-GCM cover account storage; Win32/token APIs are
entirely fake, including descriptor conversion and Create/Open/CloseMutex.
"""
import ast
import ctypes
import ctypes.wintypes
from dataclasses import dataclass, field
import hashlib
import importlib.util
import logging
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from typing import List, Optional, Tuple
import unittest
from unittest import mock
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parents[1]


def definitions(path, names, namespace):
    source = ROOT / path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise AssertionError("Missing production definitions")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)


class AccountRegressions(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.ns = ns = {
            "dataclass": dataclass, "field": field, "Optional": Optional,
            "List": List, "Tuple": Tuple, "uuid": uuid, "hashlib": hashlib,
            "sqlite3": sqlite3, "threading": threading, "time": time,
            "logger": mock.Mock(spec=logging.Logger), "_db_logger": mock.Mock(),
            "get_connection": lambda: self.db,
            "get_db_path": lambda: Path("unused-memory-db"),
            "_set_secure_permissions": mock.Mock(),
            "_migrate_credential_verifiers": mock.Mock(return_value=0),
            "_LOGIN_LOCK": threading.Lock(), "_login_attempts": {},
            "_LOCKOUT_SCHEDULE": [(5, 30), (10, 600), (5, 3600), (3, 86400), (2, 604800)],
            "AppSettings": SimpleNamespace, "CliHistoryEntry": SimpleNamespace,
            "PROTOCOL_SFTP": "sftp", "PROTOCOL_FTP": "ftp", "PROTOCOL_FTPS": "ftps",
            "AESGCM": AESGCM, "secrets": secrets, "_CRYPTO_AVAILABLE": True,
        }
        definitions("src/config.py", {"Connection"}, ns)
        definitions("src/crypto.py", {"encrypt", "decrypt"}, ns)
        # Password KDF is tested by test_credential_migration; stub it here to
        # focus on business transactions, without expensive Argon2 work.
        ns["hash_password"] = lambda pw: ("v2$" + hashlib.sha256(pw.encode()).hexdigest(), "salt")
        ns["verify_password"] = lambda pw, verifier, salt: ns["hash_password"](pw)[0] == verifier
        ns["generate_enc_key"] = lambda: secrets.token_bytes(32)
        ns["encrypt_key"] = lambda key, pw, salt, kdf: ns["encrypt"](key.hex(), hashlib.sha256(pw.encode()).digest())
        ns["decrypt_key"] = lambda enc, iv, pw, salt, kdf: bytes.fromhex(ns["decrypt"](enc, iv, hashlib.sha256(pw.encode()).digest()))
        definitions("src/database.py", {"init_db"}, ns)
        ns["init_db"]()
        definitions("src/auth_manager.py", {
            "AppUser", "Session", "AuthManager", "UserConnectionManager", "LoginLockedError",
            "_ensure_login_attempts_table", "_clear_login_attempt_from_db",
            "_persist_login_attempt", "_lockout_for_count",
        }, ns)
        ns["_ensure_login_attempts_table"]()
        self.auth = ns["AuthManager"]
        self.session = ns["Session"]
        self.alice = self.user("alice", "Alice", True)
        self.bob = self.user("bob", "Bob", False)
        self.session.login(self.alice)
        self.mgr = ns["UserConnectionManager"](self.alice)
        self.auth._ensure_remembered_table()

    def user(self, user_id, username, admin):
        key = self.ns["generate_enc_key"]()
        enc, iv = self.ns["encrypt_key"](key, "old password", "salt", "argon2")
        self.db.execute("INSERT INTO users (id, username, pw_hash, pw_salt, enc_key_enc, enc_key_iv, enc_key_kdf, is_admin) VALUES (?, ?, ?, ?, ?, ?, 'argon2', ?)",
                        (user_id, username, self.ns["hash_password"]("old password")[0], "salt", enc, iv, int(admin)))
        self.db.commit()
        return self.ns["AppUser"](user_id, username, admin, key)

    def connection(self):
        return self.mgr.add(self.ns["Connection"](
            name="Sensitive server", host="host.example", user="ssh-user",
            remote_path="/private", password="SSH secret", drive_letter="X:",
            cli_access_enabled=True, cli_access_key="bearer secret",
        ))

    def remember(self, username):
        self.db.execute("INSERT OR REPLACE INTO remembered_login(id, username, password_enc) VALUES(1, ?, 'encrypted old master password')", (username,))
        self.db.commit()

    def saved(self):
        return self.db.execute("SELECT * FROM remembered_login").fetchone()

    def seed_lock(self):
        self.ns["_login_attempts"]["alice"] = {"count": 5, "locked_until": time.time() + 3600}
        self.db.execute("INSERT INTO login_attempts VALUES('alice', 5, ?)", (time.time() + 3600,))
        self.db.commit()

    def test_duplicate_does_not_inherit_bearer_preserves_encryption_name_drive(self):
        original = self.connection()
        before = dict(self.db.execute("SELECT * FROM connections WHERE id=?", (original.id,)).fetchone())
        duplicate = self.mgr.duplicate(original.id, "Unique copy name")
        copied = dict(self.db.execute("SELECT * FROM connections WHERE id=?", (duplicate.id,)).fetchone())
        self.assertNotEqual(original.id, duplicate.id)
        self.assertEqual(duplicate.name, "Unique copy name")
        self.assertEqual((duplicate.host, duplicate.user, duplicate.remote_path, duplicate.password, duplicate.drive_letter),
                         (original.host, original.user, original.remote_path, original.password, original.drive_letter))
        self.assertFalse(duplicate.cli_access_enabled)
        self.assertFalse(duplicate.cli_access_key)
        for column in ("cli_access_key", "cli_access_key_iv", "cli_access_key_hash"):
            self.assertIsNone(copied[column])
            self.assertTrue(before[column])
        for column in ("name", "host", "ssh_user", "remote_path"):
            self.assertEqual(copied[column], "")
        self.assertNotIn("SSH secret", str(copied))
        self.assertEqual(copied["pw_enc"], before["pw_enc"])
        self.assertGreater(copied["sort_order"], before["sort_order"])
        self.assertEqual(self.mgr.get_by_cli_key("bearer secret").id, original.id)
        self.assertEqual(self.mgr.duplicate(original.id).name, original.name + " (copy)")
        self.assertEqual(dict(self.db.execute("SELECT * FROM connections WHERE id=?", (original.id,)).fetchone()), before)

    def test_duplicate_is_user_scoped(self):
        original = self.connection()
        other = self.ns["UserConnectionManager"](self.bob)
        self.assertIsNone(other.duplicate(original.id))
        self.assertIsNone(other.get_by_cli_key("bearer secret"))
        self.assertIsNone(self.mgr.duplicate("missing"))

    def test_reset_clears_normalized_lockout_and_saved_password_and_rows(self):
        connection = self.connection()
        self.db.execute("INSERT INTO active_mounts(user_id, conn_id) VALUES('alice', ?)", (connection.id,))
        self.db.execute("INSERT INTO cli_history(user_id, conn_id, kind, started_at) VALUES('alice', ?, 'session', 'now')", (connection.id,))
        self.db.commit()
        self.seed_lock()
        self.remember("  aLiCe  ")
        seen_deletes = []
        def trace(sql):
            if sql.startswith("DELETE"):
                seen_deletes.append((self.ns["_LOGIN_LOCK"].locked(),
                                     "alice" in self.ns["_login_attempts"]))
        self.db.set_trace_callback(trace)
        old_key = self.alice.enc_key
        password = self.auth.admin_reset_password("alice")
        self.db.set_trace_callback(None)
        self.assertTrue(seen_deletes and all(locked and cached for locked, cached in seen_deletes))
        self.assertFalse(self.db.in_transaction)
        self.assertNotIn("alice", self.ns["_login_attempts"])
        self.assertFalse(self.db.execute("SELECT * FROM login_attempts").fetchall())
        self.assertIsNone(self.saved())
        for table in ("connections", "cli_history", "active_mounts"):
            self.assertFalse(self.db.execute(f"SELECT * FROM {table} WHERE user_id='alice'").fetchall())
        self.assertNotEqual(old_key, self.alice.enc_key)
        self.assertIsNone(self.auth.authenticate("Alice", "old password"))
        user = self.auth.authenticate(" ALICE ", password)
        self.assertEqual(user.enc_key, self.alice.enc_key)

    def test_reset_failure_rolls_back_password_rows_remember_and_cache(self):
        original = self.connection()
        self.seed_lock()
        self.remember("ALICE")
        before = dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone())
        self.db.execute("CREATE TRIGGER reject_cleanup BEFORE DELETE ON login_attempts BEGIN SELECT RAISE(ABORT, 'cleanup failure'); END")
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cleanup failure"):
            self.auth.admin_reset_password("alice")
        self.assertEqual(dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone()), before)
        self.assertIsNotNone(self.mgr.get_by_id(original.id))
        self.assertIsNotNone(self.saved())
        self.assertIn("alice", self.ns["_login_attempts"])
        self.assertTrue(self.db.execute("SELECT * FROM login_attempts").fetchall())
        self.assertFalse(self.ns["_LOGIN_LOCK"].locked())

    def test_saved_cleanup_failure_rolls_back_reset_lockout_and_cache(self):
        self.seed_lock()
        self.remember("Alice")
        before = dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone())
        self.db.execute("CREATE TRIGGER reject_reset_saved BEFORE DELETE ON remembered_login BEGIN SELECT RAISE(ABORT, 'saved reset failure'); END")
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "saved reset failure"):
            self.auth.admin_reset_password("alice")
        self.assertEqual(dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone()), before)
        self.assertIn("alice", self.ns["_login_attempts"])
        self.assertIsNotNone(self.db.execute("SELECT * FROM login_attempts WHERE username_key='alice'").fetchone())
        self.assertIsNotNone(self.saved())

    def test_reset_preserves_other_saved_user_and_missing_reset_has_no_effect(self):
        self.remember("Bob")
        self.auth.admin_reset_password("alice")
        self.assertEqual(self.saved()["username"], "Bob")
        self.assertIsNone(self.auth.admin_reset_password("missing"))
        self.assertEqual(self.saved()["username"], "Bob")

    def test_existing_admin_api_guards_enforced(self):
        for user in (None, self.bob):
            if user:
                self.session.login(user)
            else:
                self.session.logout()
            for call in (lambda: self.auth.admin_reset_password("alice"),
                         lambda: self.auth.delete_user("alice"), self.auth.list_users):
                with self.assertRaises(PermissionError):
                    call()
        self.assertIsNotNone(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone())

    def test_change_password_clears_only_matching_saved_password_after_success(self):
        self.remember("  ALIce ")
        self.assertFalse(self.auth.change_password("alice", "wrong", "new password"))
        self.assertIsNotNone(self.saved())
        old_key = self.alice.enc_key
        self.assertTrue(self.auth.change_password("alice", "old password", "new password"))
        self.assertIsNone(self.saved())
        self.assertEqual(old_key, self.alice.enc_key)
        self.assertEqual(self.auth.authenticate("Alice", "new password").enc_key, old_key)
        self.remember("Bob")
        self.assertTrue(self.auth.change_password("alice", "new password", "next password"))
        self.assertEqual(self.saved()["username"], "Bob")

    def test_change_password_cleanup_failure_rolls_back_password(self):
        self.remember("Alice")
        before = dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone())
        self.db.execute("CREATE TRIGGER reject_saved BEFORE DELETE ON remembered_login BEGIN SELECT RAISE(ABORT, 'saved failure'); END")
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "saved failure"):
            self.auth.change_password("alice", "old password", "new password")
        self.assertEqual(dict(self.db.execute("SELECT * FROM users WHERE id='alice'").fetchone()), before)
        self.assertIsNotNone(self.saved())

    def test_optional_remember_table_not_required_for_password_changes(self):
        self.db.execute("DROP TABLE remembered_login")
        self.db.commit()
        self.assertTrue(self.auth.change_password("alice", "old password", "new password"))
        self.assertTrue(self.auth.admin_reset_password("alice"))


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []
    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


@unittest.skipUnless(sys.platform == "win32", "Windows ctypes API contract")
class InstanceRegressions(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("isolated_single_instance", ROOT / "src/single_instance.py")
        self.module = m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.handle = 0x1234567887654321
        self.error = 0
        self.create_result = self.handle
        self.open_result = self.handle
        self.close_result = True
        def create(attributes, name, flags, access):
            ctypes.set_last_error(self.error)
            return self.create_result
        def convert(sddl, revision, descriptor, length):
            ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 0x1234
            return True
        self.kernel = SimpleNamespace(
            CreateMutexExW=FakeFunction(create),
            OpenMutexW=FakeFunction(lambda *args: self.open_result),
            CloseHandle=FakeFunction(lambda handle: self.close_result),
            LocalFree=FakeFunction(lambda descriptor: None),
        )
        self.advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=FakeFunction(convert))
        self.dll = mock.patch.object(ctypes, "WinDLL", side_effect=lambda name, **kwargs: self.kernel if name == "kernel32" else self.advapi)
        self.dll.start()
        self.addCleanup(self.dll.stop)
        self.focus = mock.patch.object(m, "_find_and_focus_existing_window", return_value=False).start()
        self.addCleanup(mock.patch.stopall)
        self.token = mock.Mock()
        self.security = SimpleNamespace(
            OpenProcessToken=mock.Mock(return_value=self.token), TokenUser=1,
            GetTokenInformation=mock.Mock(return_value=("token user", 0)),
            ConvertSidToStringSid=mock.Mock(return_value="S-1-5-21-111-222-333-1001"),
        )
        patch = mock.patch.dict(sys.modules, {
            "win32api": SimpleNamespace(GetCurrentProcess=mock.Mock(return_value=-1)),
            "win32security": self.security,
        })
        patch.start()
        self.addCleanup(patch.stop)

    def test_first_handle_is_pointer_sized_least_privilege_and_release_only_closes(self):
        m = self.module
        m.ensure_single_instance()
        self.assertEqual(m._mutex_handle, self.handle)
        create = self.kernel.CreateMutexExW
        self.assertIs(create.restype, ctypes.wintypes.HANDLE)
        self.assertIs(self.kernel.OpenMutexW.restype, ctypes.wintypes.HANDLE)
        self.assertEqual(self.kernel.CloseHandle.argtypes, [ctypes.wintypes.HANDLE])
        args = create.calls[0]
        self.assertEqual(args[2:], (0, 0x00100000))
        self.assertTrue(args[1].startswith("Global\\"))
        self.assertTrue(args[1].endswith("S-1-5-21-111-222-333-1001"))
        descriptor_args = self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.calls[0]
        self.assertEqual(descriptor_args[0], "D:P(A;;0x00100000;;;S-1-5-21-111-222-333-1001)S:(ML;;NW;;;ME)")
        self.assertEqual(len(self.kernel.LocalFree.calls), 1)
        self.token.Close.assert_called_once_with()
        m.ensure_single_instance()
        self.assertEqual(len(create.calls), 1)
        m.release_instance_lock()
        self.assertEqual(self.kernel.CloseHandle.calls, [(self.handle,)])
        self.assertIsNone(m._mutex_handle)
        m.release_instance_lock()
        self.assertEqual(len(self.kernel.CloseHandle.calls), 1)

    def test_existing_closes_its_reference_and_exits_even_without_focus(self):
        self.error = 183
        with self.assertRaises(SystemExit) as exc:
            self.module.ensure_single_instance()
        self.assertEqual(exc.exception.code, 0)
        self.focus.assert_called_once_with()
        self.assertIsNone(self.module._mutex_handle)
        self.assertEqual(self.kernel.CloseHandle.calls, [(self.handle,)])

    def test_access_denied_probes_synchronize_only_existing_mutex(self):
        self.create_result = None
        self.error = 5
        with self.assertRaises(SystemExit):
            self.module.ensure_single_instance()
        self.assertEqual(self.kernel.OpenMutexW.calls[0][0:2], (0x00100000, False))
        self.assertEqual(self.kernel.OpenMutexW.calls[0][2], self.kernel.CreateMutexExW.calls[0][1])
        self.assertIsNone(self.module._mutex_handle)

    def test_access_denied_probe_failure_never_claims_first_instance(self):
        self.create_result = None
        self.open_result = None
        self.error = 5
        with self.assertRaises(OSError):
            self.module.ensure_single_instance()
        self.assertIsNone(self.module._mutex_handle)
        self.focus.assert_not_called()
        self.assertEqual(len(self.kernel.LocalFree.calls), 1)

    def test_null_create_other_error_fails_closed(self):
        self.create_result = 0
        self.error = 6
        with self.assertRaises(OSError):
            self.module.ensure_single_instance()
        self.assertFalse(self.kernel.OpenMutexW.calls)
        self.assertIsNone(self.module._mutex_handle)

    def test_descriptor_failure_prevents_mutex_creation(self):
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.callback = lambda *args: False
        with self.assertRaises(OSError):
            self.module.ensure_single_instance()
        self.assertFalse(self.kernel.CreateMutexExW.calls)
        self.assertFalse(self.kernel.LocalFree.calls)

    def test_null_descriptor_prevents_mutex_creation(self):
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.callback = lambda *args: True
        with self.assertRaisesRegex(OSError, "NULL"):
            self.module.ensure_single_instance()
        self.assertFalse(self.kernel.CreateMutexExW.calls)

    def test_names_stable_across_version_environment_but_differ_by_token_user(self):
        self.module.ensure_single_instance()
        first = self.kernel.CreateMutexExW.calls[-1][1]
        self.module.release_instance_lock()
        with mock.patch.dict("os.environ", {"USERNAME": "untrusted", "APP_VERSION": "999"}):
            self.module.ensure_single_instance()
        self.assertEqual(first, self.kernel.CreateMutexExW.calls[-1][1])
        self.module.release_instance_lock()
        self.security.ConvertSidToStringSid.return_value = "S-1-5-21-111-222-333-1002"
        self.module.ensure_single_instance()
        self.assertNotEqual(first, self.kernel.CreateMutexExW.calls[-1][1])

    def test_token_query_failure_closes_token_and_never_creates_mutex(self):
        self.security.GetTokenInformation.side_effect = OSError("token failure")
        with self.assertRaisesRegex(OSError, "token failure"):
            self.module.ensure_single_instance()
        self.token.Close.assert_called_once_with()
        self.assertFalse(self.kernel.CreateMutexExW.calls)

    def test_existing_reference_close_failure_fails_closed(self):
        self.error = 183
        self.close_result = False
        with self.assertRaises(OSError):
            self.module.ensure_single_instance()
        self.assertIsNone(self.module._mutex_handle)
        self.focus.assert_not_called()

    def test_close_failure_is_not_silently_discarded(self):
        self.module.ensure_single_instance()
        self.close_result = False
        with self.assertRaises(OSError):
            self.module.release_instance_lock()
        self.assertEqual(self.module._mutex_handle, self.handle)


if __name__ == "__main__":
    unittest.main()
