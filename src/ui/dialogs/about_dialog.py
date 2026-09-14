"""
about_dialog.py – About dialog for SSH 磁盘管理器.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QScrollArea, QWidget, QFrame, QApplication
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QIcon, QPixmap, QDesktopServices

import os
from src.channel import display_name, display_version
from src.ui.dialog_utils import match_parent_height
from src.ui.frameless_dialog import FramelessDialog
from src.ui.widgets.no_wheel import NoWheelScrollArea
from src.i18n import tr

try:
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "version.txt"), "r", encoding="utf-8") as f:
        APP_VERSION = f.read().strip()
except Exception:
    APP_VERSION = "?"

# Current project links
_URL_PROJECT_DOCS      = "https://github.com/JacobCodeShow/SSHDriveMgr#readme"
_URL_PROJECT_GITHUB    = "https://github.com/JacobCodeShow/SSHDriveMgr"
_URL_CURRENT_AUTHOR_GH = "https://github.com/JacobCodeShow"

# Original project (NEO SSH-Win Manager) links — kept for attribution
_URL_ORIG_PROJECT_WEB  = "https://www.neosshwinmanager.org/"
_URL_ORIG_PROJECT_DOCS = "https://gregorkrebs.github.io/NeoSSHWinManager/"
_URL_ORIG_PROJECT_GH   = "https://github.com/gregorkrebs/NeoSSHWinManager"
_URL_ORIG_AUTHOR_WEB   = "https://www.gregorkrebs.de/"
_URL_ORIG_AUTHOR_GH    = "https://github.com/gregorkrebs"
_URL_CONTRIB_GITHUB    = "https://github.com/Den4ik53"


def _open(url: str):
    QDesktopServices.openUrl(QUrl(url))


def _link_btn(label: str, url: str, icon_char: str = "", obj_name: str = "") -> QPushButton:
    """Gleichgestalteter Link-Button für alle Sektionen."""
    text = f"{icon_char}  {label}" if icon_char else label
    btn = QPushButton(text)
    btn.setObjectName(obj_name or "aboutLinkBtn")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(34)
    btn.clicked.connect(lambda: _open(url))
    return btn


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("sectionLabel")
    return lbl


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("dialogSectionCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(10)
    return frame, layout


def _divider() -> QFrame:
    sep = QFrame()
    sep.setObjectName("divider")
    sep.setFixedHeight(1)
    return sep


class AboutDialog(FramelessDialog):
    def __init__(self, parent=None):
        super().__init__(parent, show_maximize=True)
        self.setObjectName("dialogSurface")
        self.setWindowTitle(tr("about.title"))
        self.setMinimumWidth(440)
        self.setMaximumWidth(540)
        self.setModal(True)
        self._build_ui()
        # Always start at full available screen height
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            full_h = int(geo.height() * 0.95)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # reset QWIDGETSIZE_MAX
            self.resize(self.width(), full_h)
            self._fdlg_titlebar.set_maximized(True)

    def _build_ui(self):
        outer = QVBoxLayout(self._fdlg_content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = NoWheelScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll, stretch=1)

        inner = QWidget()
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(20, 20, 20, 12)
        layout.setSpacing(12)

        # ── Hero ─────────────────────────────────────────────────────────
        hero = QFrame()
        hero.setObjectName("dialogHeroCard")
        hero_l = QVBoxLayout(hero)
        hero_l.setContentsMargins(22, 22, 22, 18)
        hero_l.setSpacing(8)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def _resource(rel):
            import sys
            if hasattr(sys, '_MEIPASS'):
                return os.path.join(sys._MEIPASS, rel)
            return os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                rel
            )

        icon_path = _resource(os.path.join("assets", "app_icon.png"))
        if os.path.exists(icon_path):
            pix = QPixmap(icon_path).scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pix)
        else:
            from src.ui.icons import pixmap as svg_pixmap
            icon_lbl.setPixmap(svg_pixmap("cloud", "#00b4d8", 64))
        hero_l.addWidget(icon_lbl)

        title_lbl = QLabel(display_name())
        title_lbl.setObjectName("dialogTitle")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_l.addWidget(title_lbl)

        ver_lbl = QLabel(display_version(APP_VERSION))
        ver_lbl.setObjectName("dialogPill")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_l.addWidget(ver_lbl, 0, Qt.AlignmentFlag.AlignCenter)

        desc = QLabel(tr("about.desc"))
        desc.setObjectName("dialogLead")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        hero_l.addWidget(desc)

        oss = QLabel(tr("about.open_source"))
        oss.setObjectName("accentLabel")
        oss.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_l.addWidget(oss)

        layout.addWidget(hero)

        # ── What It Does ──────────────────────────────────────────────────
        what_card, what_l = _card()

        what_title = _section_label(tr("about.what_it_does.title").upper())
        what_l.addWidget(what_title)
        what_l.addWidget(_divider())

        what_desc = QLabel(tr("about.what_it_does.body"))
        what_desc.setObjectName("dialogBody")
        what_desc.setWordWrap(True)
        what_l.addWidget(what_desc)

        layout.addWidget(what_card)

        # ── Project (links + author merged) ──────────────────────────────
        proj_card, proj_l = _card()

        proj_lbl = _section_label(tr("about.project.links").upper())
        proj_l.addWidget(proj_lbl)
        proj_l.addWidget(_divider())

        # Row 1: Docs + GitHub (current project)
        proj_btns = QHBoxLayout()
        proj_btns.setSpacing(8)
        db = _link_btn(tr("about.docs.btn"),   _URL_PROJECT_DOCS,   "📄", "aboutDocsBtn")
        gb = _link_btn(tr("about.github.btn"), _URL_PROJECT_GITHUB, "⌨", "aboutGithubBtn")
        for btn in (db, gb):
            proj_btns.addWidget(btn, stretch=1)
        proj_l.addLayout(proj_btns)

        proj_l.addWidget(_divider())

        # Author info (merged into project card)
        auth_hdr = QHBoxLayout()
        auth_hdr.setSpacing(8)
        auth_title = _section_label(tr("about.author.section").upper())
        auth_hdr.addWidget(auth_title)
        auth_hdr.addStretch()
        proj_l.addLayout(auth_hdr)

        auth_btns = QHBoxLayout()
        auth_btns.setSpacing(8)
        jacob_btn = _link_btn("Jacob", _URL_CURRENT_AUTHOR_GH, "👤", "aboutAuthorBtn")
        agb = _link_btn("GitHub (JacobCodeShow)", _URL_CURRENT_AUTHOR_GH, "⌨", "aboutGithubBtn")
        auth_btns.addWidget(jacob_btn, stretch=1)
        auth_btns.addWidget(agb, stretch=1)
        proj_l.addLayout(auth_btns)

        layout.addWidget(proj_card)

        # ── Based on original project ─────────────────────────────────────
        orig_card, orig_l = _card()

        based_lbl = _section_label("基于原项目 / Based on")
        orig_l.addWidget(based_lbl)
        orig_l.addWidget(_divider())
        based_desc = QLabel("NEO SSH-Win Manager")
        based_desc.setObjectName("dialogBody")
        based_desc.setWordWrap(True)
        orig_l.addWidget(based_desc)

        orig_btns = QHBoxLayout()
        orig_btns.setSpacing(8)
        oweb = _link_btn("官网", _URL_ORIG_PROJECT_WEB, "🌐", "aboutWebBtn")
        odoc = _link_btn("文档", _URL_ORIG_PROJECT_DOCS, "📄", "aboutDocsBtn")
        ogit = _link_btn("GitHub", _URL_ORIG_PROJECT_GH, "⌨", "aboutGithubBtn")
        for btn in (oweb, odoc, ogit):
            orig_btns.addWidget(btn, stretch=1)
        orig_l.addLayout(orig_btns)

        orig_l.addWidget(_divider())

        contrib_lbl = _section_label("原作者 / Original authors")
        orig_l.addWidget(contrib_lbl)

        contrib_row = QHBoxLayout()
        contrib_row.setSpacing(8)
        gk_btn = _link_btn("Gregor Krebs", _URL_ORIG_AUTHOR_GH, "⌨", "aboutGithubBtn")
        d53_btn = _link_btn("Den4ik53", _URL_CONTRIB_GITHUB, "⌨", "aboutGithubBtn")
        contrib_row.addWidget(gk_btn, stretch=1)
        contrib_row.addWidget(d53_btn, stretch=1)
        orig_l.addLayout(contrib_row)

        layout.addWidget(orig_card)

        # ── Requirements ─────────────────────────────────────────────────
        req_card, req_l = _card()
        req_lbl = QLabel(tr("about.requires"))
        req_lbl.setObjectName("accentLabel")
        req_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        req_l.addWidget(req_lbl)
        layout.addWidget(req_card)

        layout.addStretch()

        # ── Button bar ───────────────────────────────────────────────────
        btn_bar = QWidget()
        btn_bar.setObjectName("dialogBtnBar")
        btn_bar_layout = QVBoxLayout(btn_bar)
        btn_bar_layout.setContentsMargins(20, 8, 20, 16)
        btn_bar_layout.setSpacing(8)
        btn_bar_layout.addWidget(_divider())

        ok_btn = QPushButton(tr("dialog.close"))
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setFixedWidth(120)
        ok_btn.clicked.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 10, 0, 0)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addStretch()
        btn_bar_layout.addLayout(btn_row)

        self.layout().addWidget(btn_bar)
