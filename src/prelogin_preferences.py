"""Read non-sensitive UI preferences before authentication.

Only the remembered username (never its password) or a unique local account is
used to choose the login language/theme. Multi-user installations without a
remembered username retain the neutral application defaults.
"""
from src.database import get_connection


def load_prelogin_preferences() -> dict[str, str] | None:
    try:
        with get_connection() as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            user_id = None
            if "remembered_login" in tables:
                remembered = conn.execute(
                    "SELECT username FROM remembered_login WHERE id = 1"
                ).fetchone()
                if remembered:
                    user = conn.execute(
                        "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                        (remembered["username"],),
                    ).fetchone()
                    if user:
                        user_id = user["id"]
            if user_id is None:
                users = conn.execute("SELECT id FROM users ORDER BY created_at LIMIT 2").fetchall()
                if len(users) == 1:
                    user_id = users[0]["id"]
            if user_id is None:
                return None
            settings = conn.execute(
                "SELECT language, theme FROM app_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not settings:
                return None
            return {
                "language": str(settings["language"] or "en"),
                "theme": str(settings["theme"] or "dark"),
            }
    except Exception:
        # A first run/legacy schema always falls back to neutral defaults.
        return None
