"""One bordered interval input; child controls never own an outer border."""
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QSpinBox, QWidget

from src.i18n import tr
from src.ui.widgets.no_wheel import NoWheelComboBox, NoWheelSpinBox


class IntervalInput(QFrame):
    """Numeric stepper and unit selector inside one exact 256 x 40 logical-pixel frame.

    Range/unit conversion remains the responsibility of the settings form.
    QSS uses 38px of content plus two 1px borders for the 40px outer height.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("intervalControls")
        self.setFixedSize(256, 40)
        self.setProperty("focusWithin", False)

        self.stepper = QWidget(self)
        self.stepper.setObjectName("intervalStepper")
        self.stepper.setFixedSize(142, 38)
        numeric = QHBoxLayout(self.stepper)
        numeric.setContentsMargins(4, 3, 4, 3)
        numeric.setSpacing(0)

        self.decrease = QPushButton("−", self.stepper)
        self.decrease.setObjectName("intervalMinusBtn")
        self.decrease.setFixedSize(32, 32)
        self.decrease.setToolTip(tr("settings.interval.decrease"))
        self.decrease.setAccessibleName(tr("settings.interval.decrease"))
        self.decrease.setCursor(Qt.CursorShape.PointingHandCursor)

        self.value = NoWheelSpinBox(self.stepper)
        self.value.setObjectName("intervalValue")
        self.value.setFixedSize(70, 32)
        self.value.setRange(1, 3600)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.value.setAccessibleName(tr("settings.check_interval"))

        self.increase = QPushButton("+", self.stepper)
        self.increase.setObjectName("intervalPlusBtn")
        self.increase.setFixedSize(32, 32)
        self.increase.setToolTip(tr("settings.interval.increase"))
        self.increase.setAccessibleName(tr("settings.interval.increase"))
        self.increase.setCursor(Qt.CursorShape.PointingHandCursor)
        numeric.addWidget(self.decrease)
        numeric.addWidget(self.value)
        numeric.addWidget(self.increase)
        self.decrease.clicked.connect(self.value.stepDown)
        self.increase.clicked.connect(self.value.stepUp)

        self.divider = QFrame(self)
        self.divider.setObjectName("intervalDivider")
        self.divider.setFixedSize(1, 22)

        self.unit = NoWheelComboBox(self)
        self.unit.setObjectName("intervalUnit")
        self.unit.setFixedSize(111, 38)
        self.unit.addItem(tr("settings.interval.seconds"), "s")
        self.unit.addItem(tr("settings.interval.minutes"), "m")
        self.unit.addItem(tr("settings.interval.hours"), "h")
        self.unit.setAccessibleName(tr("settings.check_interval"))

        layout = QHBoxLayout(self)
        # The QFrame supplies its own 1px content inset; don't add it twice.
        # Exactly 142 + 1 + 111 + two 1px frame borders = 256px.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.stepper)
        layout.addWidget(self.divider, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.unit)

        for control in (self.decrease, self.value, self.value.lineEdit(), self.increase, self.unit):
            control.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            focused = event.type() == QEvent.Type.FocusIn
            if self.property("focusWithin") != focused:
                self.setProperty("focusWithin", focused)
                self.style().unpolish(self)
                self.style().polish(self)
                self.update()
        return super().eventFilter(watched, event)
