"""Rendered geometry regressions for the inline Add/Edit connection form."""
import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from src.i18n import current_language, set_language, tr
from src.theme_manager import get_theme_manager
from src.ui.widgets.connection_form_layout import (
    build_connection_form_field,
    icon_grid_viewport_height,
)

ROOT = Path(__file__).resolve().parents[1]
THEMES = ("dark", "light", "ocean", "forest", "sunset", "aurora")
LANGUAGES = ("zh", "en", "de", "es", "nl", "ru", "ar")


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


def assert_field_contents_fit(field, label, editor):
    layout = field.layout()
    layout.activate()
    assert field.height() >= layout.sizeHint().height()
    assert label.geometry().bottom() < editor.geometry().top()
    assert editor.geometry().bottom() < field.height() - layout.contentsMargins().bottom()
    assert label.height() > 0 and editor.height() > 0


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("field_width", [260, 360])
def test_form_fields_grow_to_fit_theme_translation_and_narrow_width(
        app, theme, language, field_width):
    manager = get_theme_manager()
    previous_theme = manager.current_theme
    previous_language = current_language()
    field = None
    try:
        set_language(language)
        assert manager.apply_theme(app, theme)
        label_text = tr("addedit.label.protocol")
        editor = QLineEdit()
        editor.setPlaceholderText("SFTP (based on SSH)")
        field = build_connection_form_field(label_text, editor)
        field.setFixedWidth(field_width)
        field.show()
        app.processEvents()

        label = field.findChild(type(field.layout().itemAt(0).widget()), "rpFieldLabelCaps")
        assert label is not None
        assert_field_contents_fit(field, label, editor)
        # The old 54px cap is smaller than the shared QSS field's actual hint.
        assert field.height() > 54
        assert field.height() >= field.sizeHint().height()
    finally:
        if field is not None:
            field.hide()
            field.deleteLater()
            app.processEvents()
        manager.apply_theme(app, previous_theme)
        set_language(previous_language)


def test_wrapped_field_label_increases_card_height_instead_of_clipping(app):
    field = build_connection_form_field(
        "AUTHENTICATION METHOD AND ADVANCED SECURITY OPTIONS", QLineEdit())
    field.setFixedWidth(220)
    field.show()
    app.processEvents()
    label = field.findChild(type(field.layout().itemAt(0).widget()), "rpFieldLabelCaps")
    editor = field.layout().itemAt(1).widget()
    assert label is not None and label.wordWrap()
    assert label.height() > label.fontMetrics().height()
    assert_field_contents_fit(field, label, editor)
    field.hide()
    field.deleteLater()
    app.processEvents()


def test_outer_form_scrolls_while_each_field_remains_complete(app):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    content = QWidget()
    layout = QVBoxLayout(content)
    fields = []
    for index in range(5):
        editor = QLineEdit()
        editor.setPlaceholderText(f"field-{index}")
        field = build_connection_form_field(f"CONNECTION FIELD {index}", editor)
        layout.addWidget(field)
        fields.append((field, field.layout().itemAt(0).widget(), editor))
    scroll.setWidget(content)
    scroll.resize(300, 180)
    scroll.show()
    app.processEvents()
    assert scroll.verticalScrollBar().maximum() > 0
    for field, label, editor in fields:
        assert_field_contents_fit(field, label, editor)
    scroll.hide()
    scroll.deleteLater()
    content.deleteLater()
    app.processEvents()


def test_icon_viewport_fits_all_default_icons_without_an_inner_scrollbar(app):
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QScrollArea
    from src.ui.main_window import MainWindow

    manager = get_theme_manager()
    colors = manager.get_colors()
    defaults = ["", "cloud", "server", "terminal", "database", "globe", "laptop", "monitor",
                "router", "shield", "lock", "code", "git-branch", "box", "folder", "key",
                "users", "cpu"]
    for count, should_scroll in ((len(defaults), False), (27, True)):
        scroll = QScrollArea()
        scroll.resize(420, 160)
        icons = defaults if count == len(defaults) else (defaults + ["cloud"] * (count - len(defaults)))
        page = SimpleNamespace(
            _ef_icon_scroll=scroll,
            _ef_icon_name="",
            _ef_icon_btns={},
            _theme_colors=lambda: colors,
            _ef_get_icon_names=lambda icons=icons: icons,
            _ef_select_icon=lambda _name: None,
            _ef_import_custom_icon=lambda: None,
        )
        MainWindow._ef_rebuild_icon_grid(page)
        scroll.show()
        app.processEvents()
        assert scroll.height() == icon_grid_viewport_height(count) == 116
        assert (scroll.verticalScrollBar().maximum() > 0) is should_scroll
        scroll.hide()
        scroll.deleteLater()
        app.processEvents()

    assert icon_grid_viewport_height(0) == 36
    with pytest.raises(ValueError):
        icon_grid_viewport_height(-1)


def test_inline_form_uses_adaptive_fields_and_full_rows_for_icons():
    tree = ast.parse((ROOT / "src/ui/main_window.py").read_text(encoding="utf-8"))
    main_window = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "MainWindow")
    build = next(node for node in main_window.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_build_edit_form")
    helper_imports = [node for node in ast.walk(build) if isinstance(node, ast.ImportFrom)
                      and node.module == "src.ui.widgets.connection_form_layout"]
    assert helper_imports
    rebuild = next(node for node in main_window.body
                   if isinstance(node, ast.FunctionDef) and node.name == "_ef_rebuild_icon_grid")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "setFixedHeight"
               and any(isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                       and arg.func.id == "icon_grid_viewport_height" for arg in node.args)
               for node in ast.walk(rebuild))
    refresh = next(node for node in main_window.body
                   if isinstance(node, ast.FunctionDef) and node.name == "_on_theme_changed")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "_ef_rebuild_icon_grid" for node in ast.walk(refresh))
