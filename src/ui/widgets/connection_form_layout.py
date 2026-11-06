"""Layout helpers for the inline add/edit connection form."""
from PyQt6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout


_ICON_BUTTON_SIZE = 36
_ICON_GRID_SPACING = 4
_ICON_GRID_COLUMNS = 9
_ICON_GRID_VISIBLE_ROWS = 3


def build_connection_form_field(label_text: str, input_widget, parent=None) -> QFrame:
    """Build a label/input card whose height follows its real Qt size hints.

    Shared QSS adds padding and borders to controls, so hard-coding the old
    54-pixel card height clips the input. Minimum vertical policy lets wrapped
    or scaled labels grow naturally while the page's outer scroll area handles
    form overflow.
    """
    container = QFrame(parent)
    container.setObjectName("rpInfoField")
    container.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    layout = QVBoxLayout(container)
    layout.setContentsMargins(16, 8, 16, 8)
    layout.setSpacing(4)
    label = QLabel(label_text.upper(), container)
    label.setObjectName("rpFieldLabelCaps")
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(input_widget)
    return container


def icon_grid_viewport_height(icon_count: int) -> int:
    """Fit the built-in icon rows completely; scroll only for many imports."""
    if type(icon_count) is not int or icon_count < 0:
        raise ValueError("icon_count must be a non-negative integer")
    # The import button is also a grid item.
    item_count = icon_count + 1
    rows = max(1, (item_count + _ICON_GRID_COLUMNS - 1) // _ICON_GRID_COLUMNS)
    visible_rows = min(rows, _ICON_GRID_VISIBLE_ROWS)
    return (visible_rows * _ICON_BUTTON_SIZE
            + (visible_rows - 1) * _ICON_GRID_SPACING)


__all__ = ["build_connection_form_field", "icon_grid_viewport_height"]
