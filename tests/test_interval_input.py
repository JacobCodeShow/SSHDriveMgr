"""Rendered geometry and real interaction checks, not just stylesheet strings."""
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication
from src.i18n import current_language, set_language
from src.theme_manager import get_theme_manager
from src.ui.widgets.interval_input import IntervalInput


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance


@pytest.mark.parametrize("theme", ["dark", "light", "ocean", "forest", "sunset", "aurora"])
def test_rendered_interval_frame_is_exactly_40px_and_children_fit(app, theme):
    manager = get_theme_manager()
    previous = manager.current_theme
    control = None
    try:
        assert manager.apply_theme(app, theme)
        control = IntervalInput()
        control.show()
        app.processEvents()
        assert (control.width(), control.height()) == (256, 40)
        assert control.stepper.height() == control.unit.height() == 38
        assert control.stepper.mapTo(control, QPoint()).y() == control.unit.y() == 1
        # No overflow outside the single shared border, including after polish.
        for child in (control.stepper, control.unit, control.value, control.decrease, control.increase):
            position = child.mapTo(control, QPoint())
            assert 1 <= position.y() and position.y() + child.height() <= 39
            assert 1 <= position.x() and position.x() + child.width() <= 255
        assert control.value.height() == control.decrease.height() == control.increase.height() == 32
        assert not control.grab().isNull()
    finally:
        if control is not None:
            control.hide()
            control.deleteLater()
            app.processEvents()
        manager.apply_theme(app, previous)


def test_numeric_edit_step_buttons_units_and_settings_seconds(app):
    from src.ui.main_window import MainWindow
    control = IntervalInput()
    holder = SimpleNamespace(_sf_interval_value=control.value, _sf_interval_unit=control.unit)
    control.unit.currentIndexChanged.connect(
        lambda index: MainWindow._sf_on_interval_unit_changed(holder, index)
    )
    try:
        control.show()
        app.processEvents()
        control.value.setValue(30)
        control.increase.click()
        assert control.value.value() == 31
        control.decrease.click()
        assert control.value.value() == 30
        control.value.lineEdit().setText("45")
        control.value.interpretText()
        assert MainWindow._sf_get_interval_seconds(holder) == 45
        control.unit.setCurrentIndex(control.unit.findData("m"))
        assert MainWindow._sf_get_interval_seconds(holder) == 45 * 60
        control.unit.setCurrentIndex(control.unit.findData("h"))
        assert control.value.maximum() == 24 and control.value.value() == 24
        assert MainWindow._sf_get_interval_seconds(holder) == 24 * 3600
        control.increase.click()
        assert control.value.value() == 24
        control.value.setValue(1)
        control.decrease.click()
        assert control.value.value() == 1
        control.unit.setCurrentIndex(control.unit.findData("s"))
        assert control.value.maximum() == 3600
        assert MainWindow._sf_get_interval_seconds(holder) == 1
    finally:
        control.hide()
        control.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("language", ["zh", "en", "de", "es", "nl", "ru", "ar"])
def test_all_locales_and_rtl_keep_one_aligned_frame(app, language):
    previous = current_language()
    control = None
    try:
        set_language(language)
        control = IntervalInput()
        control.setLayoutDirection(Qt.LayoutDirection.RightToLeft if language == "ar"
                                   else Qt.LayoutDirection.LeftToRight)
        control.show()
        app.processEvents()
        assert control.height() == 40
        assert control.stepper.y() == control.unit.y() == 1
        assert control.unit.count() == 3
        assert all(control.unit.itemText(index) for index in range(3))
        # The offscreen Windows plugin has no system font database and reports
        # replacement-glyph widths; native Windows rendering checks real text fit.
        if app.platformName() != "offscreen":
            from PyQt6.QtWidgets import QStyle, QStyleOptionComboBox
            option = QStyleOptionComboBox()
            control.unit.initStyleOption(option)
            text_rect = control.unit.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox, option, QStyle.SubControl.SC_ComboBoxEditField,
                control.unit,
            )
            assert all(control.unit.fontMetrics().horizontalAdvance(control.unit.itemText(index))
                       <= text_rect.width() for index in range(3))
    finally:
        if control is not None:
            control.hide()
            control.deleteLater()
            app.processEvents()
        set_language(previous)
