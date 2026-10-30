"""Shared, semantic QSS layer for a restrained enterprise UI.

The shipped theme files still define their own palettes. This small final
component layer normalizes typography, controls, cards, focus, navigation and
status treatments while preserving each theme's accent and canvas.
Pure Python only: it is safe to use during pre-login theme loading.
"""
from __future__ import annotations


def _mix(first: str, second: str, amount: float) -> str:
    """Linear RGB blend, amount=0 returns first and amount=1 returns second."""
    a = first.lstrip("#")
    b = second.lstrip("#")
    if len(a) != 6 or len(b) != 6:
        return first
    amount = max(0.0, min(1.0, amount))
    channels = [
        round(int(a[i:i + 2], 16) * (1 - amount) + int(b[i:i + 2], 16) * amount)
        for i in (0, 2, 4)
    ]
    return "#%02x%02x%02x" % tuple(channels)


def _luminance(color: str) -> float:
    value = color.lstrip("#")
    if len(value) != 6:
        return 0.0
    rgb = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))


def component_stylesheet(colors: dict) -> str:
    """Compile shared components from ThemeManager's stable seven-key palette."""
    dark = bool(colors.get("is_dark", True))
    canvas = colors.get("background", "#0c1421")
    surface = colors.get("surface", "#111c2c")
    text = colors.get("text", "#e5edf6")
    accent = colors.get("accent", "#1688b5")
    border = colors.get("border", "#2b3b50")
    muted = colors.get("muted", "#98a9bc")
    # Layered neutral surfaces remain perceptible without relying on neon glows.
    panel = _mix(canvas, surface, .58 if dark else .28)
    raised = _mix(surface, "#ffffff" if dark else "#edf2f8", .055 if dark else .42)
    field = _mix(canvas, surface, .52 if dark else .34)
    subtle = _mix(canvas, border, .46 if dark else .34)
    border_strong = _mix(border, accent, .24)
    accent_hover = _mix(accent, "#ffffff" if dark else "#000000", .13)
    accent_soft = _mix(canvas, accent, .22 if dark else .12)
    selected = _mix(surface, accent, .13 if dark else .085)
    primary_text = "#ffffff" if _luminance(accent) < .42 else "#102031"
    brand_bg = "#0d1725"
    brand_surface = "#142337"
    brand_text = "#e8f0f8"
    brand_muted = "#a5b7c9"
    brand_border = "#2c4157"
    success = "#57c99a" if dark else "#147a55"
    success_soft = _mix(canvas, success, .18 if dark else .10)
    warning = "#e4b25c" if dark else "#95600e"
    danger = "#e77783" if dark else "#bb3c4b"
    danger_soft = _mix(canvas, danger, .17 if dark else .10)

    return f"""
/* === Shared enterprise component layer; accent remains theme-specific === */
QWidget {{
    font-family: "Segoe UI", "Inter", sans-serif;
    color: {text};
    selection-background-color: {accent};
    selection-color: {primary_text};
}}
QToolTip {{
    background-color: {raised}; color: {text};
    border: 1px solid {border}; border-radius: 7px;
    padding: 6px 9px; font-size: 12px;
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    color: {text}; background-color: {field};
    border: 1px solid {border}; border-radius: 8px;
    padding: 7px 10px; min-height: 20px;
    selection-background-color: {accent}; selection-color: {primary_text};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QTextEdit:hover, QPlainTextEdit:hover {{ border-color: {border_strong}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus {{
    background-color: {raised}; border: 1px solid {accent};
}}
/* Inline edit fields use a more specific legacy ancestor selector. Match it here. */
QFrame#rpInfoField QLineEdit, QFrame#rpInfoField QSpinBox, QFrame#rpInfoField QComboBox {{
    color: {text}; background-color: {field}; border: 1px solid {border};
    border-radius: 8px; padding: 5px 9px; min-height: 22px; max-height: 28px;
    font-size: 13px; font-weight: 400;
}}
QFrame#rpInfoField QLineEdit:focus, QFrame#rpInfoField QSpinBox:focus,
QFrame#rpInfoField QComboBox:focus {{ background-color: {raised}; border: 1px solid {accent}; }}
QFrame#rpInfoField QPushButton {{
    color: {text}; background-color: {raised}; border: 1px solid {border};
    border-radius: 8px; padding: 0 8px; min-height: 28px; max-height: 32px;
}}
QFrame#rpInfoField QPushButton:hover {{ background-color: {selected}; border-color: {accent}; }}
/* QSS sizes are CONTENT sizes: 38px + two 1px borders = 40px outside. */
QFrame#intervalControls {{
    background-color: {field}; border: 1px solid {border}; border-radius: 9px;
    padding: 0; min-height: 38px; max-height: 38px;
}}
QFrame#intervalControls:hover {{ border-color: {border_strong}; }}
QFrame#intervalControls[focusWithin="true"] {{ border-color: {accent}; }}
QFrame#intervalControls QWidget#intervalStepper {{
    background: transparent; border: none; border-radius: 0; padding: 0;
    min-height: 38px; max-height: 38px;
}}
QFrame#intervalControls QWidget#intervalStepper QPushButton {{
    background: transparent; color: {muted}; border: none; border-radius: 6px;
    padding: 0; min-height: 32px; max-height: 32px;
    font-size: 15px; font-weight: 650;
}}
QFrame#intervalControls QWidget#intervalStepper QPushButton:hover {{ background-color: {accent_soft}; color: {accent}; }}
QFrame#intervalControls QWidget#intervalStepper QPushButton:pressed {{ background-color: {selected}; }}
QFrame#intervalControls QWidget#intervalStepper QSpinBox,
QFrame#intervalControls QWidget#intervalStepper QSpinBox:hover,
QFrame#intervalControls QWidget#intervalStepper QSpinBox:focus {{
    background: transparent; color: {text}; border: none; border-radius: 0;
    padding: 0; min-height: 32px; max-height: 32px; font-size: 13px; font-weight: 650;
}}
QFrame#intervalControls QSpinBox QLineEdit {{ background: transparent; border: none; padding: 0; min-height: 0; }}
QFrame#intervalControls QComboBox#intervalUnit,
QFrame#intervalControls QComboBox#intervalUnit:hover,
QFrame#intervalControls QComboBox#intervalUnit:focus {{
    background: transparent; color: {text}; border: none; border-radius: 0;
    padding: 0 10px; min-height: 38px; max-height: 38px; font-size: 13px;
}}
QFrame#intervalControls QComboBox#intervalUnit::drop-down {{ border: none; width: 24px; }}
QFrame#intervalControls QFrame#intervalDivider {{ background-color: {border}; border: none; padding: 0; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {muted}; background-color: {panel}; border-color: {subtle};
}}
QComboBox::drop-down {{ border: 0; width: 26px; }}
QComboBox QAbstractItemView {{
    background-color: {raised}; color: {text};
    border: 1px solid {border}; selection-background-color: {selected};
    selection-color: {text}; padding: 4px;
}}

QPushButton {{
    color: {text}; background-color: {raised};
    border: 1px solid {border}; border-radius: 9px;
    padding: 5px 12px;
}}
QPushButton:hover {{ background-color: {selected}; border-color: {border_strong}; }}
QPushButton:pressed {{ background-color: {panel}; }}
QPushButton:disabled {{ color: {muted}; background-color: {panel}; border-color: {subtle}; }}
QPushButton#primaryBtn, #actionBtn[btn_type="primary"], #settingsActionBtn[btn_type="primary"] {{
    background-color: {accent}; color: {primary_text};
    border: 1px solid {accent}; font-weight: 650;
}}
QPushButton#primaryBtn:hover, #actionBtn[btn_type="primary"]:hover,
#settingsActionBtn[btn_type="primary"]:hover {{ background-color: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#primaryBtn:disabled {{ background-color: {panel}; color: {muted}; border-color: {subtle}; }}
QPushButton#secondaryBtn {{ background-color: {raised}; color: {text}; border: 1px solid {border}; }}
QPushButton#secondaryBtn:hover {{ background-color: {selected}; border-color: {border_strong}; }}
QPushButton#dangerBtn, #actionBtn[btn_type="danger"] {{
    background-color: {danger_soft}; color: {danger}; border: 1px solid {border};
}}
QPushButton#dangerBtn:hover, #actionBtn[btn_type="danger"]:hover {{ border-color: {danger}; }}

QCheckBox, QRadioButton {{ color: {text}; spacing: 9px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px; height: 17px; border: 1px solid {border}; background: {field};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {accent}; border: 1px solid {accent};
}}

#fwOuter, #MainWindow, #fullscreenPanel, #connectionsPanel {{ background-color: {canvas}; }}
#customTitlebar, #rightPanelHeader {{ background-color: {panel}; border-bottom: 1px solid {subtle}; }}
QWidget#connectionsHeader {{
    background-color: {panel}; border-bottom: 1px solid {subtle};
    min-height: 64px; max-height: 64px;
}}
#sidebar {{ background-color: {panel}; border-right: 1px solid {subtle}; min-width: 176px; max-width: 176px; }}
#rightPanel {{ background-color: {canvas}; border-left: 1px solid {subtle}; }}
#statusBar {{ background-color: {panel}; border-top: 1px solid {subtle}; font-family: "Segoe UI"; font-size: 11px; padding: 6px 12px; }}
#statusText, #statusPill {{ color: {muted}; font-family: "Segoe UI"; }}

#connectionCard {{
    background-color: {surface}; border: 1px solid {border}; border-radius: 12px;
}}
#connectionCard:hover {{ background-color: {raised}; border-color: {border_strong}; }}
#connectionCard[mounted="true"] {{
    background-color: {success_soft}; border: 1px solid {_mix(border, success, .38)};
}}
#connectionCard[selected="true"] {{
    background-color: {selected}; border: 1px solid {accent};
}}
#connectionCard[mounted="true"][selected="true"] {{
    background-color: {success_soft}; border: 1px solid {success};
}}
#connName {{ color: {text}; font-size: 14px; font-weight: 650; }}
#connDetail, #fieldLabel, #rowLabel {{ color: {muted}; }}
#driveBadge {{ background-color: {accent_soft}; color: {accent}; border: 1px solid {_mix(border, accent, .32)}; border-radius: 8px; }}
#driveBadge[mounted="true"] {{ background-color: {success_soft}; color: {success}; border-color: {_mix(border, success, .4)}; }}
#rpStatusDot[mounted="true"], #rpStatusLabel[mounted="true"] {{ color: {success}; }}
#groupPill {{ background-color: {accent_soft}; color: {accent}; border-color: {_mix(border, accent, .3)}; }}

QFrame#dialogHeroCard, QFrame#dialogSectionCard, QFrame#settingsGroupCard,
QFrame#fullscreenSectionCard, QFrame#sysinfoHeroCard, QFrame#sysinfoSectionCard,
QFrame#userBox {{ background-color: {surface}; border: 1px solid {border}; border-radius: 14px; }}
#dialogTitle {{ color: {text}; font-size: 20px; font-weight: 700; }}
#sectionLabel, #rpSectionLabel, #settingsSectionLabel {{ color: {accent}; font-size: 11px; font-weight: 650; }}
#dialogLead, #hintLabel, #mutedLabel {{ color: {muted}; }}

QPushButton#sidebarBtn {{ background-color: transparent; border: 1px solid transparent; border-radius: 10px; min-width: 156px; max-width: 156px; min-height: 44px; max-height: 44px; padding: 0 12px; text-align: left; }}
QPushButton#sidebarBtn:hover {{ background-color: {selected}; border-color: {border}; }}
QPushButton#sidebarBtn[active="true"] {{ background-color: {accent_soft}; border: 1px solid {_mix(border, accent, .40)}; }}
QPushButton#headerAddBtn {{ background-color: {accent}; color: {primary_text}; border: 1px solid {accent}; border-radius: 9px; min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px; padding: 0; }}
QPushButton#headerAddBtn:hover {{ background-color: {accent_hover}; }}
QPushButton#headerActionBtn, QToolButton#headerActionBtn {{
    min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px; padding: 0;
}}
QToolButton#headerActionBtn::menu-indicator {{ image: none; width: 0; }}
QComboBox#headerGroupsCombo {{ background-color: {raised}; color: {text}; border: 1px solid {border}; border-radius: 9px; min-height: 40px; max-height: 40px; padding: 4px 10px; }}
QLabel#connectionsSummary {{ color: {muted}; background: transparent; border: none; padding: 0; min-height: 14px; max-height: 14px; font-size: 11px; }}
QComboBox#headerGroupsCombo:hover, QComboBox#headerGroupsCombo:focus {{ border-color: {accent}; }}
QPushButton#headerActionBtn, QToolButton#headerActionBtn, QPushButton#rpHeaderBtn,
QPushButton#cardEditBtn, QPushButton#sshBtn, QPushButton#mountBtn,
QPushButton#connectionIconChoiceBtn {{
    background-color: {raised}; border: 1px solid {border}; border-radius: 9px;
}}
QPushButton#headerActionBtn:hover, QToolButton#headerActionBtn:hover,
QPushButton#rpHeaderBtn:hover, QPushButton#cardEditBtn:hover,
QPushButton#sshBtn:hover, QPushButton#mountBtn:hover, QPushButton#connectionIconChoiceBtn:hover {{
    background-color: {selected}; border-color: {border_strong};
}}
QPushButton#mountBtn[btn_type="primary"] {{ background-color: {accent_soft}; border-color: {_mix(border, accent, .32)}; }}
QPushButton#mountBtn[btn_type="danger"] {{ background-color: {danger_soft}; border-color: {_mix(border, danger, .34)}; }}
QPushButton#mountBtn[btn_type="danger"]:hover {{ border-color: {danger}; }}
QPushButton#connectionIconChoiceBtn:checked {{ background-color: {accent_soft}; border-color: {accent}; }}
#iconImportBtn {{
    background-color: transparent; border: 1px dashed {border_strong}; border-radius: 9px;
}}
#iconImportBtn:hover {{ background-color: {accent_soft}; border-color: {accent}; }}
#settingsThemeActionBtn {{ background-color: {raised}; border: 1px solid {border}; border-radius: 9px; }}
#settingsThemeActionBtn:hover {{ background-color: {selected}; border-color: {accent}; }}

#rightPanelPlaceholderTitle {{ color: {text}; font-size: 22px; font-weight: 700; }}
#rightPanelPlaceholderBody {{ color: {muted}; font-size: 13px; }}
#workspaceWelcome {{ background: transparent; }}
#workspaceWelcomeEyebrow, #loginBrandEyebrow {{
    color: {accent}; font-size: 10px; font-weight: 700; letter-spacing: 1.25px;
}}
#workspaceWelcomeMark {{
    background-color: {accent_soft}; border: 1px solid {_mix(border, accent, .42)}; border-radius: 36px;
}}
#workspaceMetricCard {{ background-color: {surface}; border: 1px solid {border}; border-radius: 11px; }}
#workspaceMetricValue {{ color: {text}; font-size: 19px; font-weight: 700; }}
#workspaceMetricLabel {{ color: {muted}; font-size: 11px; }}
#loginBrandPanel {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {brand_bg}, stop:0.72 {brand_surface}, stop:1 {accent_soft});
    border: 1px solid {brand_border}; border-radius: 16px;
}}
#loginBrandTitle {{ color: {brand_text}; font-size: 24px; font-weight: 720; }}
#loginBrandTagline {{ color: {brand_muted}; font-size: 13px; }}
#loginFeatureText {{ color: {brand_text}; font-size: 12px; }}
#loginFeatureIcon {{ background: transparent; }}
#loginTrustBadge {{
    background-color: {brand_surface}; color: {brand_muted}; border: 1px solid {brand_border};
    border-radius: 9px; padding: 10px 12px; font-size: 11px;
}}
#loginVersion {{ color: {brand_muted}; font-size: 10px; font-family: "Segoe UI"; }}
#messageIcon {{ background-color: {accent_soft}; border: 1px solid {_mix(border, accent, .32)}; border-radius: 22px; }}
#loginFormSurface {{ background: transparent; }}
#loginFormHeading {{ color: {text}; font-size: 23px; font-weight: 700; }}
#loginFormCaption, #loginLocalNote {{ color: {muted}; font-size: 12px; }}
#loginFormCard {{ background-color: {surface}; border: 1px solid {border}; border-radius: 14px; }}
QLabel#connectionsBadge, QLabel#dialogPill, QLabel#userRoleBadge {{
    background-color: {accent_soft}; color: {accent}; border: 1px solid {_mix(border, accent, .3)};
    border-radius: 8px; padding: 4px 9px;
}}
#userRoleBadge[variant="success"] {{ background-color: {success_soft}; color: {success}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 3px 1px; }}
QScrollBar::handle:vertical {{ background-color: {_mix(border, muted, .22)}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background-color: {muted}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QMenu {{ background-color: {raised}; color: {text}; border: 1px solid {border}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 7px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {selected}; color: {text}; }}
"""
