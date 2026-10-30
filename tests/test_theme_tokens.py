"""Theme tokens and notifications: stdlib only, no Qt or host appdata writes."""
import gc
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from src.theme_manager import ThemeManager
from src.ui.titlebar_theme import get_palette


class ThemeTokenTests(unittest.TestCase):
    def setUp(self):
        # Fully in-memory custom QSS; no temp directory or appdata is written.
        self.root = Path(__file__).parent / "_theme_test_nonexistent"
        self.env = patch.dict(os.environ, {"APPDATA": str(self.root / "appdata")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = ThemeManager()
        self.themes = self.manager.list_themes()

    def custom(self, name, content):
        class MemoryTheme:
            def read_text(self, encoding):
                return content
        self.themes[name] = MemoryTheme()
        self.manager.list_themes = lambda: self.themes

    def test_all_builtin_tokens_and_surfaces(self):
        expected = {
            "dark": ("#0d0d12", "#0d1117", "#0077b6", True),
            "light": ("#f0f2f5", "#ffffff", "#0077b6", False),
            "ocean": ("#0a141a", "#0f1c24", "#0891b2", True),
            "forest": ("#0a1410", "#0f1c17", "#059669", True),
            "sunset": ("#1a0f0f", "#221515", "#dc2626", True),
            "aurora": ("#0f0a1a", "#151022", "#7c3aed", True),
        }
        for name, tokens in expected.items():
            with self.subTest(theme=name):
                colors = self.manager.get_colors(name)
                self.assertEqual(tuple(colors[k] for k in ("background", "surface", "accent", "is_dark")), tokens)
                self.assertEqual(set(colors), {"background", "surface", "text", "accent", "border", "muted", "is_dark"})
                stylesheet = self.manager.load_theme(name)
                self.assertNotIn("__SURFACE__", stylesheet)
                if name != "light":
                    self.assertIn("background-color: " + colors["surface"], stylesheet)
        self.assertFalse((self.root / "appdata").exists())

    def test_hyphen_variables_and_luminance_not_name(self):
        self.custom("dark-sounding", """
/* @background: #fafafa */
/* @surface: #eee */
/* @accent-hover: #123abc */
/* @accent: @accent-hover */
/* @text-muted: #666 */
QWidget { color: #123; background: /* @background */; }
QPushButton:hover { background: /*   @accent-hover   */; }
QMessageBox { background: __SURFACE__; }
""")
        colors = self.manager.get_colors("dark-sounding")
        self.assertFalse(colors["is_dark"])
        self.assertEqual(colors["surface"], "#eeeeee")
        self.assertEqual(colors["text"], "#112233")
        self.assertEqual(colors["accent"], "#123abc")
        self.assertEqual(colors["muted"], "#666666")
        self.assertIn("background: #123abc", self.manager.load_theme("dark-sounding"))

    def test_custom_qss_without_definitions(self):
        self.custom("light-in-name-only", """
QMainWindow { background: rgb(12, 24, 36); }
QWidget { color: #def; }
QLineEdit { background: #123456; border: 1px solid #345678; }
#sectionLabel { color: #abcdef; }
#hintLabel { color: #789abc; }
""")
        colors = self.manager.get_colors("light-in-name-only")
        self.assertTrue(colors["is_dark"])
        self.assertEqual(colors["background"], "#0c1824")
        self.assertEqual(colors["surface"], "#123456")
        self.assertEqual(colors["accent"], "#abcdef")
        self.assertEqual(colors["border"], "#345678")
        self.assertEqual(colors["muted"], "#789abc")

    def test_titlebar_uses_same_tokens(self):
        with patch("src.theme_manager._theme_manager", self.manager):
            for name in ("dark", "light", "ocean", "forest", "sunset", "aurora"):
                colors = self.manager.get_colors(name)
                palette = get_palette(name)
                self.assertEqual(palette.bg, colors["background"])
                self.assertEqual(palette.text, colors["text"])
                self.assertEqual(palette.border, colors["border"])
                self.assertEqual(palette.text_dim, colors["muted"])

    def test_apply_preview_rollback_repeat_fallback_unsubscribe(self):
        class App:
            def setStyleSheet(self, sheet):
                self.sheet = sheet
        app = App()
        events = []
        def changed(name):
            self.assertEqual(self.manager.current_theme, name)
            events.append(name)
        self.manager.subscribe(changed)
        self.manager.subscribe(changed)
        for name in ("dark", "ocean", "dark", "dark", "missing"):
            self.assertTrue(self.manager.apply_theme(app, name))
        self.assertEqual(events, ["dark", "ocean", "dark", "dark", "dark"])
        self.assertEqual(self.manager.current_theme, "dark")
        self.manager.unsubscribe(changed)
        self.manager.unsubscribe(changed)
        self.manager.apply_theme(app, "light")
        self.assertEqual(len(events), 5)

    def test_weak_bound_subscriber_and_callback_isolation(self):
        class Consumer:
            def changed(self, name):
                pass
        consumer = Consumer()
        self.manager.subscribe(consumer.changed)
        del consumer
        gc.collect()
        def broken(name):
            raise RuntimeError("expected subscriber failure")
        events = []
        self.manager.subscribe(broken)
        self.manager.subscribe(lambda name: events.append(name))
        with self.assertLogs("src.theme_manager", level="ERROR"):
            self.manager.apply_theme(type("App", (), {"setStyleSheet": lambda self, sheet: None})(), "ocean")
        self.assertEqual(events, ["ocean"])

    def test_cache_invalidation_and_result_copy(self):
        self.custom("custom", "/* @background: #123456 */")
        colors = self.manager.get_colors("custom")
        colors["background"] = "#ffffff"
        self.assertEqual(self.manager.get_colors("custom")["background"], "#123456")
        self.custom("custom", "/* @background: #fafafa */")
        self.manager.clear_cache()
        self.assertEqual(self.manager.get_colors("custom")["background"], "#fafafa")

    def test_imports_do_not_load_qt_or_write_appdata(self):
        code = "import sys; import src.theme_manager; import src.ui.titlebar_theme; assert not any(n.startswith(('PyQt', 'PySide')) for n in sys.modules)"
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "appdata").exists())


if __name__ == "__main__":
    unittest.main()
