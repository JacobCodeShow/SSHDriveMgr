"""Visual-system contracts: shared tokens, accessible navigation and login layout."""
import ast
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def class_node(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)


def method_source(path, class_name, method_name):
    cls = class_node(path, class_name)
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method_name)
    return ast.get_source_segment(path.read_text(encoding="utf-8"), method)


def test_shared_component_layer_covers_all_builtin_themes_and_preserves_color_contract():
    from src.theme_manager import ThemeManager
    manager = ThemeManager()
    expected = {"background", "surface", "text", "accent", "border", "muted", "is_dark"}
    for name in ("dark", "light", "ocean", "forest", "sunset", "aurora"):
        colors = manager.get_colors(name)
        qss = manager.load_theme(name)
        assert set(colors) == expected
        for selector in ("#loginBrandPanel", "#loginFormCard", "#connectionCard",
                         "#workspaceMetricCard", "QPushButton#sidebarBtn", "QLineEdit:focus",
                         "#messageIcon", "QWidget#intervalStepper QSpinBox", "QToolTip",
                         "QPushButton#primaryBtn"):
            assert selector in qss, (name, selector)
        assert "__CHECKMARK_URL__" not in qss
        assert "__SURFACE__" not in qss


def test_every_locale_has_brand_empty_state_and_toolbar_labels():
    keys = {
        "app.title", "login.brand.eyebrow", "login.brand.tagline",
        "login.brand.feature.credentials", "login.brand.feature.protocols",
        "login.brand.feature.mounts", "login.brand.trust", "login.form.signin",
        "login.form.register", "panel.placeholder.eyebrow", "panel.placeholder.add",
        "panel.placeholder.settings", "panel.placeholder.saved", "panel.placeholder.mounted",
        "main.more_actions", "settings.update.current", "settings.update.failed",
        "about.based_on", "about.original.website", "about.original.docs", "about.original.github",
    }
    for path in (ROOT / "src/translations").glob("*.json"):
        values = json.loads(path.read_text(encoding="utf-8"))
        assert keys <= values.keys(), (path.stem, keys - values.keys())
        if path.stem != "zh":
            for key in ("app.title", "about.title", "login.title", "update.lead"):
                assert not any(0x4E00 <= ord(char) <= 0x9FFF for char in values[key]), (path.stem, key)


def test_login_uses_split_brand_form_with_compact_mode_and_no_duplicate_hero_cards():
    path = ROOT / "src/ui/dialogs/login_dialog.py"
    source = path.read_text(encoding="utf-8")
    dialog = class_node(path, "LoginDialog")
    names = {node.name for node in dialog.body if isinstance(node, ast.FunctionDef)}
    assert "_build_brand_panel" in names and "_brand_feature" in names
    build = method_source(path, "LoginDialog", "_build_ui")
    assert "self._compact_layout" in build
    brand = method_source(path, "LoginDialog", "_build_brand_panel")
    assert "_build_brand_panel()" in build and "loginFormCard" in build
    assert "loginBrandPanel" in brand
    assert "dialogHeroCard" not in build and "dialogSectionCard" not in build
    assert "setMinimumSize" in source and "login.form.signin" in source


def test_shell_has_discoverable_nav_responsive_toolbar_and_actionable_workspace():
    path = ROOT / "src/ui/main_window.py"
    source = path.read_text(encoding="utf-8")
    sidebar = method_source(path, "MainWindow", "_sidebar_btn")
    assert "setAccessibleName(label)" in sidebar and "setToolTip(label)" in sidebar
    assert "QToolButton" in source and "_update_connection_header_layout" in source
    responsive = method_source(path, "MainWindow", "_update_connection_header_layout")
    assert "self._body_splitter.widget(0).width()" in responsive
    assert "pane_width < 520" in responsive and "pane_width < 420" in responsive
    assert "self._badge_lbl.setVisible(not narrow)" in responsive
    tokens = (ROOT / "src/ui/design_tokens.py").read_text(encoding="utf-8")
    assert "QToolButton#headerActionBtn::menu-indicator" in tokens
    placeholder = method_source(path, "MainWindow", "_show_right_panel_placeholder")
    for marker in ("workspaceMetrics", "workspaceWelcomeMark", "panel.placeholder.saved",
                   "panel.placeholder.mounted", "self._on_add", "self._on_settings"):
        assert marker in placeholder
    assert "self._active_mounts" in source


def test_check_interval_uses_one_aligned_segmented_control_group():
    path = ROOT / "src/ui/main_window.py"
    source = method_source(path, "MainWindow", "_build_settings_form")
    assert "interval_controls = IntervalInput()" in source
    assert "self._sf_interval_value = interval_controls.value" in source
    assert "self._sf_interval_unit = interval_controls.unit" in source
    assert "interval_hl.addWidget(interval_controls" in source
    tokens = (ROOT / "src/ui/design_tokens.py").read_text(encoding="utf-8")
    assert "QWidget#intervalStepper QSpinBox" in tokens
    assert "QComboBox#intervalUnit" in tokens


def test_home_toolbar_and_check_interval_share_a_40px_control_rhythm():
    main = (ROOT / "src/ui/main_window.py").read_text(encoding="utf-8")
    tokens = (ROOT / "src/ui/design_tokens.py").read_text(encoding="utf-8")
    assert main.count("setFixedSize(QSize(40, 40))") >= 5  # plus, mount, unmount, overflow, filter
    assert 'setObjectName("connectionsSummary")' in main
    component = (ROOT / "src/ui/widgets/interval_input.py").read_text(encoding="utf-8")
    assert "self.setFixedSize(256, 40)" in component
    assert "QLabel#connectionsSummary" in tokens
    assert "QToolButton#headerActionBtn::menu-indicator" in tokens
    # The border belongs only to the outer frame; child QSS must not add 40px
    # content minimums on top of padding (the original 42px vs 50px regression).
    assert "QFrame#intervalControls" in tokens
    assert "QComboBox#intervalUnit {{ min-height: 40px" not in tokens


def test_connection_editor_uses_the_same_frameless_dialog_shell():
    path = ROOT / "src/ui/dialogs/add_edit_dialog.py"
    cls = class_node(path, "AddEditDialog")
    assert any(isinstance(base, ast.Name) and base.id == "FramelessDialog" for base in cls.bases)
    text = path.read_text(encoding="utf-8")
    assert "QVBoxLayout(self._fdlg_content)" in text


def test_about_links_use_localized_labels_and_shared_svg_iconography():
    path = ROOT / "src/ui/dialogs/about_dialog.py"
    source = path.read_text(encoding="utf-8")
    for key in ("about.based_on", "about.original.website", "about.original.docs",
                "about.original.github", "about.other.devs"):
        assert f'tr("{key}")' in source
    assert "setIcon(svg_icon(icon_name" in source
    assert not any(char in source for char in ("🌐", "📄", "⌨", "👤"))


def test_fullscreen_page_switch_hides_deferred_widgets_before_deleting():
    source = method_source(ROOT / "src/ui/main_window.py", "MainWindow", "_clear_fs_content")
    assert source.index("w.hide()") < source.index("w.deleteLater()")


@pytest.mark.skipif(sys.platform != "win32", reason="Native offscreen Qt dialog contract")
def test_login_widget_tree_renders_with_each_builtin_theme():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QWidget
    from src.database import init_db
    from src.i18n import set_language
    from src.theme_manager import get_theme_manager
    from src.ui.dialogs.login_dialog import LoginDialog

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    init_db()
    set_language("zh")
    themes = get_theme_manager()
    for name in ("dark", "light", "ocean", "forest", "sunset", "aurora"):
        assert themes.apply_theme(app, name)
        dialog = LoginDialog()
        dialog.resize(960, 620)
        dialog.show()
        app.processEvents()
        assert dialog.findChild(QWidget, "loginBrandPanel") is not None
        assert dialog.findChild(QWidget, "loginFormSurface") is not None
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
