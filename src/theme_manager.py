"""Qt-independent QSS discovery, semantic colours, and apply notifications."""

import logging
import os
import re
import shutil
import weakref
from pathlib import Path
from typing import Callable, Dict, List, Optional

_LOG = logging.getLogger(__name__)
_DEFINITION = re.compile(r"/\*\s*@([\w-]+)\s*:\s*(.*?)\s*\*/", re.S)
_REFERENCE = re.compile(r"/\*\s*@([\w-]+)\s*\*/")
_DARK = dict(background="#0d0d12", surface="#0d1117", text="#c8d6e5",
             accent="#0077b6", border="#1a1a2e", muted="#8fa4b8")
_LIGHT = dict(background="#f0f2f5", surface="#ffffff", text="#1a2332",
              accent="#0077b6", border="#d4d8df", muted="#5a6a7a")


def _color(value: str) -> Optional[str]:
    """Normalize common QSS colours without importing QColor (or any Qt)."""
    value = value.strip().lower()
    named = {"white": "#ffffff", "black": "#000000", "red": "#ff0000",
             "blue": "#0000ff", "green": "#008000", "gray": "#808080",
             "grey": "#808080", "navy": "#000080", "yellow": "#ffff00"}
    if value in named:
        return named[value]
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value
    # Qt uses #AARRGGBB; semantic surfaces are opaque RGB values.
    if re.fullmatch(r"#[0-9a-f]{8}", value):
        return "#" + value[3:]
    match = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*[\d.]+%?)?\s*\)", value)
    if match:
        rgb = tuple(int(v) for v in match.groups())
        if all(0 <= v <= 255 for v in rgb):
            return "#%02x%02x%02x" % rgb
    return None


def _is_dark(background: str) -> bool:
    """Classify by WCAG relative luminance, never by theme/file name."""
    rgb = [int(background[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722))) < .179


class ThemeManager:
    """Resolve tokens and notify explicitly subscribed consumers, without Qt.

    Callbacks receive the actual applied name on the caller's thread, after
    every successful apply (including repeat applies). UI callers must apply
    on their GUI thread. Bound methods are weakly held to avoid widget leaks.
    """

    def __init__(self):
        self._current_theme = "dark"
        self._theme_cache: Dict[str, str] = {}
        self._color_cache: Dict[str, dict] = {}
        self._subscribers = []

    @property
    def current_theme(self) -> str:
        """Actual applied name, including fallback/preview/rollback applies."""
        return self._current_theme

    def subscribe(self, callback: Callable[[str], None]) -> None:
        """Register callback(name), once; registration does not invoke it."""
        self.unsubscribe(callback)
        if getattr(callback, "__self__", None) is not None:
            try:
                self._subscribers.append(weakref.WeakMethod(callback))
                return
            except TypeError:
                pass  # Built-in bound callables cannot use WeakMethod.
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str], None]) -> None:
        """Remove a callback; safe to call repeatedly; prune dead methods."""
        self._subscribers[:] = [entry for entry in self._subscribers
                               if (entry() if isinstance(entry, weakref.WeakMethod) else entry)
                               not in (None, callback)]

    def get_builtin_theme_dir(self) -> Path:
        return Path(__file__).resolve().parents[1] / "assets" / "themes"

    def get_user_theme_dir(self) -> Path:
        """Create the user directory only for explicit import/open operations."""
        appdata = os.environ.get("APPDATA", str(Path.home()))
        user_dir = Path(appdata) / "SSHDriveMgr" / "themes"
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def get_theme_dirs(self) -> List[Path]:
        """User overrides first. Discovery/loading never writes to appdata."""
        appdata = os.environ.get("APPDATA", str(Path.home()))
        user_dir = Path(appdata) / "SSHDriveMgr" / "themes"
        return [p for p in (user_dir, self.get_builtin_theme_dir()) if p.is_dir()]

    def list_themes(self) -> Dict[str, Path]:
        themes: Dict[str, Path] = {}
        for theme_dir in self.get_theme_dirs():
            for qss_file in theme_dir.glob("*.qss"):
                themes.setdefault(qss_file.stem, qss_file)
        return themes

    @staticmethod
    def _variables(content: str) -> Dict[str, str]:
        variables = {m.group(1): m.group(2).strip() for m in _DEFINITION.finditer(content)}
        # Bounded resolution handles aliases without looping on cyclic input.
        for _ in range(len(variables)):
            updated = {}
            for key, value in variables.items():
                value = _REFERENCE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
                if value.startswith("@"):
                    value = variables.get(value[1:], value)
                updated[key] = value
            if updated == variables:
                break
            variables = updated
        return variables

    def _process_variables(self, content: str) -> str:
        variables = self._variables(content)
        return _REFERENCE.sub(lambda m: variables.get(m.group(1), m.group(0)), content)

    def _resolve_colors(self, content: str) -> dict:
        variables = self._variables(content)
        qss = re.sub(r"/\*.*?\*/", "", self._process_variables(content), flags=re.S)
        rules = []
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", qss):
            declarations = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)(?:;|$)", body))
            rules.append(([s.strip() for s in selectors.split(",")], declarations))

        def rule_color(selectors, properties):
            for selector in selectors:
                for candidates, declarations in reversed(rules):
                    if selector not in candidates:
                        continue
                    for prop in properties:
                        value = declarations.get(prop, "").strip()
                        result = _color(value)
                        if not result and prop.startswith("border"):
                            match = re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", value)
                            result = _color(match.group(0)) if match else None
                        if result:
                            return result
            return None

        def variable(*names):
            for name in names:
                result = _color(variables.get(name, ""))
                if result:
                    return result
            return None

        background = variable("background", "background-color", "bg") or rule_color(
            ("#MainWindow", "QMainWindow", "#fwOuter", "QWidget", "#customTitlebar", "QDialog"),
            ("background-color", "background")) or _DARK["background"]
        dark = _is_dark(background)
        colors = dict(_DARK if dark else _LIGHT)
        colors["background"] = background
        inferred = {
            "surface": variable("surface", "surface-color") or rule_color(
                ("QMessageBox", "#fullscreenPanel", "QMenu", "QFrame#rightPanel", "#rightPanel", "QLineEdit", "QTextEdit"),
                ("background-color", "background")),
            "text": variable("text", "text-color", "foreground", "fg") or rule_color(
                ("QWidget", "QMainWindow", "#MainWindow", "#customTitlebarTitle", "QLabel"), ("color",)),
            "accent": variable("accent", "accent-color", "primary") or rule_color(
                ("QCheckBox::indicator:checked", "#sectionLabel", "QTabBar::tab:selected", "QPushButton:default"),
                ("background-color", "background", "color")),
            "border": variable("border", "border-color") or rule_color(
                ("#customTitlebar", "#sidePanel", "QLineEdit", "QFrame"),
                ("border-color", "border-bottom", "border-right", "border")),
            "muted": variable("muted", "text-muted", "muted-text", "text-dim") or rule_color(
                ("#customTitlebarVersion", "#fieldLabel", "#hintLabel"), ("color",)),
        }
        colors.update({key: value for key, value in inferred.items() if value})
        colors["is_dark"] = dark
        return colors

    def load_theme(self, name: str) -> Optional[str]:
        if name in self._theme_cache:
            return self._theme_cache[name]
        file_path = self.list_themes().get(name)
        if file_path is None:
            return None
        try:
            content = file_path.read_text(encoding="utf-8-sig")
            self._color_cache[name] = self._resolve_colors(content)
            content = self._process_variables(content)
            content = self._process_builtin_placeholders(content, name)
            # Keep each theme's accent/canvas, but normalize shared Qt components
            # in one late QSS layer so fields, cards, buttons and focus agree.
            from src.ui.design_tokens import component_stylesheet
            content += "\n" + component_stylesheet(self._color_cache[name])
            self._theme_cache[name] = content
            return content
        except Exception:
            _LOG.exception("Failed to load theme %r", name)
            return None

    def get_colors(self, name: Optional[str] = None) -> dict:
        """Fresh dict: background,surface,text,accent,border,muted,is_dark.

        Missing/unreadable names use dark, matching apply fallback. No name
        resolves the actual currently applied theme.
        """
        name = self.current_theme if name is None else name
        if name not in self._color_cache:
            self.load_theme(name)
        if name not in self._color_cache and name != "dark":
            self.load_theme("dark")
        return dict(self._color_cache.get(name, self._color_cache.get("dark", dict(_DARK, is_dark=True))))

    def _process_builtin_placeholders(self, content: str, theme_name: str) -> str:
        icon_dir = Path(__file__).resolve().parents[1] / "assets" / "icons"
        colors = self._color_cache.get(theme_name) or self._resolve_colors(content)
        return (content.replace("__CHECKMARK_URL__", str(icon_dir / "check.svg").replace("\\", "/"))
                .replace("__CHEVRON_URL__", str(icon_dir / "chevron-down.svg").replace("\\", "/"))
                .replace("__SURFACE__", colors["surface"]))

    def apply_theme(self, app, name: str) -> bool:
        stylesheet = self.load_theme(name)
        if stylesheet is None:
            _LOG.warning("Theme %r unavailable; falling back to dark", name)
            stylesheet = self.load_theme("dark")
            if stylesheet is None:
                return False
            name = "dark"
        app.setStyleSheet(stylesheet)
        self._current_theme = name
        for entry in tuple(self._subscribers):
            callback = entry() if isinstance(entry, weakref.WeakMethod) else entry
            if callback is None:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)
                continue
            try:
                callback(name)
            except Exception:
                _LOG.exception("Theme subscriber failed for %r", name)
        return True

    def import_theme(self, file_path: str) -> Optional[str]:
        src = Path(file_path)
        if not src.exists() or src.suffix.lower() != ".qss":
            return None
        dst = self.get_user_theme_dir() / src.name
        try:
            shutil.copy2(src, dst)
            self._theme_cache.pop(dst.stem, None)
            self._color_cache.pop(dst.stem, None)
            return dst.stem
        except Exception:
            _LOG.exception("Failed to import theme")
            return None

    def open_user_theme_dir(self):
        user_dir = self.get_user_theme_dir()
        try:
            if os.name == "nt":
                os.startfile(str(user_dir))
            elif os.name == "posix":
                import subprocess
                subprocess.Popen(["xdg-open", str(user_dir)])
        except Exception:
            _LOG.exception("Failed to open theme directory")

    def clear_cache(self):
        self._theme_cache.clear()
        self._color_cache.clear()


_theme_manager: Optional[ThemeManager] = None


def get_theme_manager() -> ThemeManager:
    global _theme_manager
    if _theme_manager is None:
        _theme_manager = ThemeManager()
    return _theme_manager
