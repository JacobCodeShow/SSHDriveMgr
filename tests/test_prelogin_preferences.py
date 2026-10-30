"""Pre-login appearance lookup reads only a username and UI preferences."""
from contextlib import contextmanager
import sqlite3
from unittest.mock import patch

from src.prelogin_preferences import load_prelogin_preferences


def fake_connection(users, settings, remembered=None, statements=None):
    @contextmanager
    def connect():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE users (id TEXT, username TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE app_settings (user_id TEXT, language TEXT, theme TEXT)")
        for user_id, username in users:
            conn.execute("INSERT INTO users VALUES (?, ?, ?)", (user_id, username, user_id))
        for user_id, language, theme in settings:
            conn.execute("INSERT INTO app_settings VALUES (?, ?, ?)", (user_id, language, theme))
        if remembered is not None:
            conn.execute("CREATE TABLE remembered_login (id INTEGER, username TEXT, password_enc TEXT)")
            conn.execute("INSERT INTO remembered_login VALUES (1, ?, ?)", (remembered, "must-never-be-read"))
        if statements is not None:
            conn.set_trace_callback(statements.append)
        try:
            yield conn
        finally:
            conn.close()
    return connect


def lookup(users, settings, remembered=None, statements=None):
    connect = fake_connection(users, settings, remembered, statements)
    with patch("src.prelogin_preferences.get_connection", connect):
        return load_prelogin_preferences()


def test_unique_local_account_uses_saved_language_and_theme():
    assert lookup([("u1", "alice")], [("u1", "zh", "ocean")]) == {
        "language": "zh", "theme": "ocean",
    }


def test_remembered_username_selects_only_that_users_appearance():
    statements = []
    result = lookup(
        [("u1", "alice"), ("u2", "bob")],
        [("u1", "en", "dark"), ("u2", "ar", "forest")],
        remembered="BoB",
        statements=statements,
    )
    assert result == {"language": "ar", "theme": "forest"}
    assert all("password_enc" not in sql.lower() for sql in statements)


def test_multi_user_install_without_remembered_username_uses_neutral_defaults():
    assert lookup(
        [("u1", "alice"), ("u2", "bob")],
        [("u1", "de", "ocean"), ("u2", "es", "light")],
    ) is None


def test_no_accounts_returns_none_and_uses_neutral_defaults():
    assert lookup([], []) is None


def test_missing_or_unreadable_settings_fail_to_neutral_defaults():
    assert lookup([("u1", "alice")], []) is None
