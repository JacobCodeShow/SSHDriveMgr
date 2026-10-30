"""Stdlib-only credential regression tests.

Run: python -m unittest discover -s tests -p test_credential_migration.py -v
AST-load selected REAL production definitions: no cryptography, Qt, registry,
appdata, network, or import-time application side effects. AES/Argon2 primitives
are mocked where needed; PBKDF2, verifier conversion and SQLite are real.
"""

import ast
import hashlib
import hmac
import logging
from pathlib import Path
import secrets
import sqlite3
from typing import Tuple
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CRYPTO_FUNCTIONS = {
    "upgrade_legacy_password_hash", "hash_password", "verify_password",
    "derive_key", "encrypt_key", "decrypt_key",
}
CRYPTO_CONSTANTS = {
    "_PBKDF2_ITERATIONS", "_PBKDF2_HASH", "_KEY_LEN",
    "_PASSWORD_VERIFIER_PREFIX", "_PASSWORD_VERIFIER_DOMAIN",
}


def load_definitions(relative_path, names, namespace, constants=()):
    """Compile source definitions, never a copied implementation or imports."""
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            targets = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if targets & set(constants):
                selected.append(node)
                found.update(targets)
    missing = (set(names) | set(constants)) - found
    if missing:
        raise AssertionError(f"Missing production definitions: {missing}")
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def production_namespace():
    namespace = {
        "hashlib": hashlib, "hmac": hmac, "secrets": secrets,
        "Tuple": Tuple, "sqlite3": sqlite3,
        "_db_logger": mock.Mock(spec=logging.Logger),
    }
    load_definitions("src/crypto.py", CRYPTO_FUNCTIONS, namespace, CRYPTO_CONSTANTS)
    load_definitions("src/database.py", {"_migrate_credential_verifiers", "init_db"}, namespace)
    return namespace


class CredentialMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "Original password \u5b89\u5168"
        cls.salt = bytes(range(32)).hex()
        cls.wrapping_key = hashlib.pbkdf2_hmac(
            "sha256", cls.password.encode("utf-8"), bytes.fromhex(cls.salt),
            600_000, dklen=32,
        )
        cls.legacy_hash = cls.wrapping_key.hex()

    def setUp(self):
        self.ns = production_namespace()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("""
            CREATE TABLE users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT NOT NULL,
                pw_salt TEXT, enc_key_enc TEXT, enc_key_iv TEXT,
                enc_key_kdf TEXT NOT NULL DEFAULT 'pbkdf2', is_admin INTEGER DEFAULT 0
            )
        """)

    def add_user(self, user_id="old", verifier=None, kdf="pbkdf2"):
        self.db.execute(
            "INSERT INTO users (id, username, pw_hash, pw_salt, enc_key_enc, enc_key_iv, enc_key_kdf) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, user_id, verifier if verifier is not None else self.legacy_hash,
             self.salt, "historical ciphertext", "historical iv", kdf),
        )
        self.db.commit()

    def migrate(self):
        return self.ns["_migrate_credential_verifiers"](self.db)

    def verifier(self, user_id="old"):
        return self.db.execute("SELECT pw_hash FROM users WHERE id=?", (user_id,)).fetchone()[0]

    def init_db(self):
        self.ns.update({
            "get_db_path": mock.Mock(return_value=Path("unused-in-memory.db")),
            "get_connection": lambda: self.db,
            "_set_secure_permissions": mock.Mock(),
        })
        self.ns["init_db"]()

    def test_domain_separation_is_fixed_one_way_format(self):
        expected = "v2$" + hashlib.sha256(
            b"SSHDriveMgr/password-verifier/v2\x00" + self.wrapping_key
        ).hexdigest()
        actual = self.ns["upgrade_legacy_password_hash"](self.legacy_hash)
        self.assertEqual(actual, expected)
        self.assertNotEqual(bytes.fromhex(actual[3:]), self.wrapping_key)
        with self.assertRaises(ValueError):
            bytes.fromhex(actual)

    def test_new_hash_uses_same_versioned_format_not_wrapping_key(self):
        with mock.patch.object(secrets, "token_bytes", return_value=bytes.fromhex(self.salt)):
            verifier, salt = self.ns["hash_password"](self.password)
        self.assertEqual(salt, self.salt)
        self.assertEqual(verifier, self.ns["upgrade_legacy_password_hash"](self.legacy_hash))
        self.assertTrue(self.ns["verify_password"](self.password, verifier, salt))
        self.assertFalse(self.ns["verify_password"]("wrong", verifier, salt))

    def test_legacy_backup_fallback_accepts_only_original_password(self):
        verify = self.ns["verify_password"]
        self.assertTrue(verify(self.password, self.legacy_hash, self.salt))
        self.assertTrue(verify(self.password, self.legacy_hash.upper(), self.salt))
        self.assertFalse(verify("wrong", self.legacy_hash, self.salt))
        self.assertFalse(verify(self.legacy_hash, self.legacy_hash, self.salt))

    def test_malformed_and_unknown_verifiers_fail_closed(self):
        good = self.ns["upgrade_legacy_password_hash"](self.legacy_hash)
        for malformed in (None, b"hash", "", "0" * 63, "g" * 64,
                          "0 " * 32, "v2$" + "0" * 63,
                          "v2$" + "g" * 64, "v3$" + good[3:],
                          "v2$v2$" + good[3:]):
            with self.subTest(verifier=malformed):
                self.assertFalse(self.ns["verify_password"](self.password, malformed, self.salt))
        for salt in (None, "not hex", "0"):
            with self.subTest(salt=salt):
                self.assertFalse(self.ns["verify_password"](self.password, good, salt))

    def test_upgrade_rejects_nonlegacy_input(self):
        for malformed in (None, "", "0" * 63, "0 " * 32,
                          "v2$" + "0" * 64, "g" * 64):
            with self.subTest(verifier=malformed):
                with self.assertRaises(ValueError):
                    self.ns["upgrade_legacy_password_hash"](malformed)

    def test_startup_migrates_all_legacy_users_preserving_credentials(self):
        self.add_user("inactive-pbkdf2")
        self.add_user("inactive-argon2", self.legacy_hash.upper(), kdf="argon2")
        current = self.ns["upgrade_legacy_password_hash"](self.legacy_hash)
        self.add_user("current", current, kdf="argon2")
        before = self.db.execute(
            "SELECT id, pw_salt, enc_key_enc, enc_key_iv, enc_key_kdf FROM users ORDER BY id"
        ).fetchall()
        self.init_db()
        after = self.db.execute(
            "SELECT id, pw_salt, enc_key_enc, enc_key_iv, enc_key_kdf FROM users ORDER BY id"
        ).fetchall()
        self.assertEqual(before, after)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        for user_id in ("inactive-pbkdf2", "inactive-argon2", "current"):
            verifier = self.verifier(user_id)
            self.assertEqual(verifier, current)
            self.assertTrue(self.ns["verify_password"](self.password, verifier, self.salt))
            self.assertFalse(self.ns["verify_password"]("wrong", verifier, self.salt))

    def test_migration_is_idempotent_and_preserves_nonlegacy_records(self):
        self.add_user()
        current = self.ns["upgrade_legacy_password_hash"](self.legacy_hash)
        self.add_user("current", current, "argon2")
        self.add_user("malformed", "not a legacy hash")
        self.add_user("future", "v3$" + self.legacy_hash)
        self.db.execute("PRAGMA secure_delete = OFF")
        self.assertEqual(self.migrate(), 1)
        self.assertEqual(self.db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        snapshot = self.db.execute("SELECT * FROM users ORDER BY id").fetchall()
        self.assertEqual(self.migrate(), 0)
        self.assertEqual(snapshot, self.db.execute("SELECT * FROM users ORDER BY id").fetchall())
        self.assertEqual(self.verifier("malformed"), "not a legacy hash")
        self.assertEqual(self.verifier("future"), "v3$" + self.legacy_hash)

    def test_failure_after_first_update_rolls_back_every_verifier(self):
        self.add_user("first")
        self.add_user("second")
        self.db.execute("""
            CREATE TRIGGER reject_second BEFORE UPDATE OF pw_hash ON users
            WHEN OLD.id = 'second' BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """)
        self.db.commit()
        statements = []
        self.db.set_trace_callback(statements.append)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected failure"):
            self.migrate()
        self.assertTrue(any("UPDATE users" in sql and "'first'" in sql for sql in statements))
        self.assertEqual(self.verifier("first"), self.legacy_hash)
        self.assertEqual(self.verifier("second"), self.legacy_hash)
        self.assertFalse(self.db.in_transaction)
        self.db.execute("DROP TRIGGER reject_second")
        self.assertEqual(self.migrate(), 2)

    def test_savepoint_does_not_commit_callers_transaction(self):
        self.add_user()
        self.db.execute("BEGIN")
        self.assertEqual(self.migrate(), 1)
        self.assertTrue(self.db.in_transaction)
        self.assertTrue(self.verifier().startswith("v2$"))
        self.db.rollback()
        self.assertEqual(self.verifier(), self.legacy_hash)

    def test_startup_migration_failure_propagates_and_rolls_back(self):
        self.add_user("first")
        self.add_user("second")
        self.db.execute("""
            CREATE TRIGGER reject_second BEFORE UPDATE OF pw_hash ON users
            WHEN OLD.id = 'second' BEGIN SELECT RAISE(ABORT, 'startup failure'); END
        """)
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "startup failure"):
            self.init_db()
        self.assertEqual(self.verifier("first"), self.legacy_hash)
        self.assertEqual(self.verifier("second"), self.legacy_hash)

    def test_migrated_login_still_derives_historical_wrapping_from_password(self):
        self.add_user()
        self.migrate()
        stored = self.verifier()
        self.assertTrue(self.ns["verify_password"](self.password, stored, self.salt))
        enc_key = bytes(reversed(range(32)))

        def decrypt_historical(ciphertext, iv, key):
            self.assertEqual((ciphertext, iv), ("historical ciphertext", "historical iv"))
            if not hmac.compare_digest(key, self.wrapping_key):
                raise ValueError("Wrong wrapping key")
            return enc_key.hex()

        self.ns["decrypt"] = mock.Mock(side_effect=decrypt_historical)
        self.assertEqual(self.ns["decrypt_key"](
            "historical ciphertext", "historical iv", self.password, self.salt, "pbkdf2"
        ), enc_key)
        with self.assertRaisesRegex(ValueError, "Wrong wrapping key"):
            self.ns["decrypt_key"](
                "historical ciphertext", "historical iv", stored, self.salt, "pbkdf2"
            )
        self.assertNotEqual(bytes.fromhex(stored[3:]), self.wrapping_key)
        self.assertEqual(self.ns["derive_key"](self.password, self.salt), self.wrapping_key)

    def test_password_change_preserves_enc_key_and_uses_argon2(self):
        self.add_user()
        self.migrate()
        self.db.row_factory = sqlite3.Row
        path = ROOT / "src/auth_manager.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        auth = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AuthManager")
        change = next(node for node in auth.body if isinstance(node, ast.FunctionDef) and node.name == "change_password")
        change.decorator_list = []
        enc_key = bytes(reversed(range(32)))
        new_password = "Changed password"
        secure_password = mock.Mock()
        secure_bytes = mock.Mock()
        secure_bytes.from_string.return_value = secure_password
        argon2_key = b"A" * 32
        self.ns.update({
            "get_connection": lambda: self.db,
            "Session": mock.Mock(), "logger": mock.Mock(),
            "_CRYPTO_AVAILABLE": True, "SecureBytes": secure_bytes,
            "derive_key_argon2": mock.Mock(return_value=(argon2_key, b"ignored")),
            "encrypt": mock.Mock(return_value=("new ciphertext", "new iv")),
        })

        def decrypt_historical(ciphertext, iv, wrapping_key):
            self.assertEqual(wrapping_key, self.wrapping_key)
            return enc_key.hex()

        self.ns["decrypt"] = mock.Mock(side_effect=decrypt_historical)
        exec(compile(ast.Module(body=[change], type_ignores=[]), str(path), "exec"), self.ns)
        self.assertTrue(self.ns["change_password"]("old", self.password, new_password))
        row = self.db.execute("SELECT * FROM users WHERE id='old'").fetchone()
        self.assertTrue(self.ns["verify_password"](new_password, row["pw_hash"], row["pw_salt"]))
        self.assertFalse(self.ns["verify_password"](self.password, row["pw_hash"], row["pw_salt"]))
        self.assertFalse(self.ns["verify_password"](new_password, row["pw_hash"], self.salt))
        self.assertEqual(row["enc_key_kdf"], "argon2")
        self.assertEqual((row["enc_key_enc"], row["enc_key_iv"]), ("new ciphertext", "new iv"))
        secure_bytes.from_string.assert_called_once_with(new_password)
        self.ns["derive_key_argon2"].assert_called_once_with(secure_password, bytes.fromhex(row["pw_salt"]))
        secure_password.wipe.assert_called_once_with()
        self.ns["encrypt"].assert_called_once_with(enc_key.hex(), argon2_key)
        self.ns["Session"].update_enc_key.assert_called_once_with("old", enc_key)

    def test_cleanup_occurs_after_commit_and_not_on_idempotent_startup(self):
        self.add_user()
        statements = []
        self.db.set_trace_callback(statements.append)
        self.init_db()
        vacuum = next(i for i, sql in enumerate(statements) if sql == "VACUUM")
        checkpoint = next(i for i, sql in enumerate(statements) if "wal_checkpoint(TRUNCATE)" in sql)
        release = max(i for i, sql in enumerate(statements) if "RELEASE SAVEPOINT" in sql)
        self.assertLess(release, vacuum)
        self.assertLess(vacuum, checkpoint)
        statements.clear()
        self.init_db()
        self.assertNotIn("VACUUM", statements)
        self.assertFalse(any("wal_checkpoint(TRUNCATE)" in sql for sql in statements))

    def test_cleanup_failures_warn_without_reverting_migration(self):
        self.add_user()
        proxy = mock.MagicMock(wraps=self.db)
        proxy.__enter__.return_value = proxy
        proxy.__exit__.side_effect = self.db.__exit__

        def execute(sql, *args):
            if sql == "VACUUM":
                raise sqlite3.OperationalError("cleanup busy")
            if "wal_checkpoint(TRUNCATE)" in sql:
                raise sqlite3.OperationalError("checkpoint busy")
            return self.db.execute(sql, *args)

        proxy.execute.side_effect = execute
        self.ns.update({
            "get_db_path": mock.Mock(return_value=Path("unused-in-memory.db")),
            "get_connection": lambda: proxy,
            "_set_secure_permissions": mock.Mock(),
        })
        self.ns["init_db"]()
        self.assertTrue(self.verifier().startswith("v2$"))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.ns["_db_logger"].warning.call_count, 2)

    def test_new_accounts_still_request_argon2_wrapping(self):
        path = ROOT / "src/auth_manager.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        auth = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AuthManager")
        register = next(node for node in auth.body if isinstance(node, ast.FunctionDef) and node.name == "register")
        calls = [node for node in ast.walk(register) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "encrypt_key"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(keyword.arg == "kdf" and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value == "argon2" for keyword in calls[0].keywords))


if __name__ == "__main__":
    unittest.main()
