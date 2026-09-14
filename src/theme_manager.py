"""
theme_manager.py - Theme management for SSH 磁盘管理器.

Scans theme directories, loads .qss files, and applies them to the
QApplication. Supports built-in themes (assets/themes/) and user themes
(%APPDATA%/SSHDriveMgr/themes/).
"""

import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional


class ThemeManager:
    """Manages theme discovery, loading, and application."""

    def __init__(self):
        self._current_theme: str = "dark"
        self._theme_cache: Dict[str, str] = {}

    @property
    def current_theme(self) -> str:
        return self._current_theme

    def get_builtin_theme_dir(self) -> Path:
        """Return the built-in themes directory (assets/themes/)."""
        return Path(__file__).resolve().parents[1] / "assets" / "themes"

    def get_user_theme_dir(self) -> Path:
        """Return the user themes directory (%APPDATA%/SSHDriveMgr/themes/)."""
        appdata = os.environ.get("APPDATA", str(Path.home()))
        user_dir = Path(appdata) / "SSHDriveMgr" / "themes"
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def get_theme_dirs(self) -> List[Path]:
        """Return all theme directories in priority order (user first)."""
        dirs = []
        user_dir = self.get_user_theme_dir()
        if user_dir.exists():
            dirs.append(user_dir)
        builtin_dir = self.get_builtin_theme_dir()
        if builtin_dir.exists():
            dirs.append(builtin_dir)
        return dirs

    def list_themes(self) -> Dict[str, Path]:
        """
        Scan all theme directories and return {theme_name: file_path}.
        User themes override built-in themes with the same name.
        """
        themes: Dict[str, Path] = {}
        for theme_dir in self.get_theme_dirs():
            for qss_file in theme_dir.glob("*.qss"):
                name = qss_file.stem
                if name not in themes:  # user dirs come first, so they win
                    themes[name] = qss_file
        return themes

    def load_theme(self, name: str) -> Optional[str]:
        """Load a theme file by name, processing variable substitutions."""
        if name in self._theme_cache:
            return self._theme_cache[name]

        themes = self.list_themes()
        if name not in themes:
            return None

        file_path = themes[name]
        try:
            content = file_path.read_text(encoding="utf-8")
            content = self._process_variables(content)
            content = self._process_builtin_placeholders(content, name)
            self._theme_cache[name] = content
            return content
        except Exception as e:
            print(f"[ThemeManager] Failed to load theme '{name}': {e}")
            return None

    def _process_builtin_placeholders(self, content: str, theme_name: str) -> str:
        """Replace built-in placeholders (__CHECKMARK_URL__, etc.)."""
        icon_dir = Path(__file__).resolve().parents[1] / "assets" / "icons"
        checkmark_url = str(icon_dir / "check.svg").replace("\\", "/")
        chevron_url = str(icon_dir / "chevron-down.svg").replace("\\", "/")

        # Surface color based on theme
        surface = "#0D1117" if theme_name == "dark" else "#ffffff"

        content = content.replace("__CHECKMARK_URL__", checkmark_url)
        content = content.replace("__CHEVRON_URL__", chevron_url)
        content = content.replace("__SURFACE__", surface)
        return content

    def _process_variables(self, content: str) -> str:
        """
        Process theme variables defined as comments:
            /* @var_name: value */
        and replace /* @var_name */ placeholders in the stylesheet.
        """
        variables: Dict[str, str] = {}

        # Extract variable definitions: /* @name: value */
        for match in re.finditer(r'/\*\s*@(\w+)\s*:\s*(.+?)\s*\*/', content):
            var_name = match.group(1)
            var_value = match.group(2).strip()
            variables[var_name] = var_value

        # Replace placeholders: /* @name */
        for var_name, var_value in variables.items():
            placeholder = f"/* @{var_name} */"
            content = content.replace(placeholder, var_value)

        return content

    def apply_theme(self, app, name: str) -> bool:
        """Apply a theme to the QApplication. Returns True on success."""
        stylesheet = self.load_theme(name)
        if stylesheet is None:
            print(f"[ThemeManager] Theme '{name}' not found, falling back to dark")
            stylesheet = self.load_theme("dark")
            if stylesheet is None:
                return False
            name = "dark"

        app.setStyleSheet(stylesheet)
        self._current_theme = name
        print(f"[ThemeManager] Applied theme: {name}")
        return True

    def import_theme(self, file_path: str) -> Optional[str]:
        """
        Import a .qss file into the user theme directory.
        Returns the theme name on success, None on failure.
        """
        src = Path(file_path)
        if not src.exists() or src.suffix.lower() != ".qss":
            return None

        user_dir = self.get_user_theme_dir()
        dst = user_dir / src.name
        try:
            shutil.copy2(src, dst)
            self._theme_cache.pop(dst.stem, None)
            return dst.stem
        except Exception as e:
            print(f"[ThemeManager] Failed to import theme: {e}")
            return None

    def open_user_theme_dir(self):
        """Open the user theme directory in the system file explorer."""
        user_dir = self.get_user_theme_dir()
        try:
            if os.name == "nt":
                os.startfile(str(user_dir))
            elif os.name == "posix":
                import subprocess
                subprocess.Popen(["xdg-open", str(user_dir)])
        except Exception as e:
            print(f"[ThemeManager] Failed to open theme dir: {e}")

    def clear_cache(self):
        """Clear the theme cache (call after importing/deleting themes)."""
        self._theme_cache.clear()


# Global singleton
_theme_manager: Optional[ThemeManager] = None


def get_theme_manager() -> ThemeManager:
    """Get the global ThemeManager singleton."""
    global _theme_manager
    if _theme_manager is None:
        _theme_manager = ThemeManager()
    return _theme_manager
