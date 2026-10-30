"""
sftp_browser.py – Native SFTP/FTP file browser window for SSH 磁盘管理器.

Opens as a non-modal FramelessDialog when the user clicks the folder button on a
mounted connection. Provides directory navigation, file download/upload, rename,
delete, and new-folder operations — all without blocking the Qt UI thread.

The window drives SFTP, FTP and FTPS sessions: the transport is picked from
Connection.protocol, everything above it (model, workers, UI) is shared.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from typing import Any

from PyQt6.QtCore import (
    QAbstractTableModel, QFileSystemWatcher, QModelIndex, QSize,
    QSortFilterProxyModel, Qt, QThread, QTimer, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QProgressDialog,
    QPushButton, QSizePolicy, QTreeView, QVBoxLayout, QWidget,
)

from src.app_logger import logger
from src.config import Connection
from src.local_paths import validate_remote_filename, confined_download_path
from src.ftp_client import FtpClient
from src.i18n import tr
from src.sftp_client import SftpClient, SftpEntry
from src.ui.dialogs.styled_message_box import StyledInputDialog, StyledMessageBox
from src.ui.frameless_dialog import FramelessDialog
from src.ui.icons import icon as svg_icon, pixmap as svg_pixmap
from src.ui.sftp_worker import (
    SftpConnectWorker,
    SftpDeleteWorker,
    SftpDownloadWorker,
    SftpListWorker,
    SftpMkdirWorker,
    SftpRenameWorker,
    SftpUploadWorker,
)


# ── Table model ──────────────────────────────────────────────────────────────

_COL_NAME  = 0
_COL_SIZE  = 1
_COL_MTIME = 2
_COL_PERMS = 3
_COL_TYPE  = 4

_ENTRY_ROLE = Qt.ItemDataRole.UserRole


class SftpFileModel(QAbstractTableModel):
    """Table model backing the QTreeView in SftpBrowserWindow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: list[SftpEntry] = []

    def set_entries(self, entries: list[SftpEntry]) -> None:
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()

    def entry_at(self, row: int) -> SftpEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 5

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self._entries[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == _COL_NAME:
                return entry.name
            if col == _COL_SIZE:
                return "—" if entry.is_dir else _fmt_size(entry.size)
            if col == _COL_MTIME:
                try:
                    return datetime.fromtimestamp(entry.modified).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    return ""
            if col == _COL_PERMS:
                return entry.permissions
            if col == _COL_TYPE:
                return tr("sftp.type.dir") if entry.is_dir else tr("sftp.type.file")

        if role == Qt.ItemDataRole.TextAlignmentRole and col == _COL_SIZE:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == _ENTRY_ROLE:
            return entry

        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return [
                tr("sftp.col.name"),
                tr("sftp.col.size"),
                tr("sftp.col.modified"),
                tr("sftp.col.permissions"),
                tr("sftp.col.type"),
            ][section]
        return None


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"


# ── Browser window ───────────────────────────────────────────────────────────

class SftpBrowserWindow(FramelessDialog):
    """Non-modal SFTP/FTP browser with one instance per connection."""

    _tofu_requested = pyqtSignal(str, int, str, object)
    shutdown_failed = pyqtSignal(str)

    def __init__(
        self,
        conn: Connection,
        theme: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, show_maximize=True)
        self._tofu_requested.connect(
            self._dispatch_tofu_dialog,
            Qt.ConnectionType.QueuedConnection,
        )
        self._conn = conn
        self._theme = theme
        self._is_ftp = bool(getattr(conn, "is_ftp", False))
        self._client = FtpClient() if self._is_ftp else SftpClient()
        self._current_path: str = (conn.remote_path or "/").rstrip("/") or "/"
        self._history: list[str] = []
        self._forward_stack: list[str] = []
        self._active_list_worker: SftpListWorker | None = None
        self._workers: list = []   # keep references so Qt doesn't GC running workers
        self._pending_transfer_results = set()
        self._worker_reaper_timer = QTimer(self)
        self._worker_reaper_timer.setInterval(50)
        self._worker_reaper_timer.timeout.connect(self._reap_stopped_workers)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_watched_file_changed)
        # local_path → remote_path for files currently open for editing
        self._edit_map: dict[str, str] = {}
        # Debounce: local_path → QTimer to avoid double-upload on save
        self._edit_timers: dict[str, QTimer] = {}
        self._edit_generations: dict[str, int] = {}
        self._edit_uploaded_versions: dict[str, tuple] = {}
        self._edit_uploads: set[str] = set()
        self._close_observed_versions: dict[str, tuple] = {}
        self._close_last_change_at = time.monotonic()
        self._disconnect_versions: dict[str, tuple] = {}
        self._shutdown_recovery_reason = None
        self._edit_reconnecting = False
        self._edit_reconnect_failed = False
        self._aborting_shutdown = False
        self._close_requested = False
        self._close_ready = False
        self._close_wait_timer: QTimer | None = None
        self._close_pending_edits: list[tuple[str, str]] = []
        self._pending_tofu_requests = []
        self._disconnect_thread = None
        self._disconnect_done = threading.Event()
        self._disconnect_error = None

        self.setWindowTitle(tr(
            "ftp.title" if self._is_ftp else "sftp.title",
            name=conn.name, proto=conn.protocol_label,
        ))
        self.resize(960, 620)

        self._build_ui()
        self.set_dialog_theme(theme)
        self._apply_theme_stylesheet()

        # Defer connection so the window appears first
        QTimer.singleShot(0, self._connect_and_load)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self._fdlg_content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setObjectName("sftpToolbar")
        toolbar.setFixedHeight(44)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 6, 8, 6)
        tb_layout.setSpacing(4)

        icon_color = "#aab4c4" if self._theme == "dark" else "#4a5a6a"

        self._btn_back = QPushButton("←")
        self._btn_back.setObjectName("sftpNavBtn")
        self._btn_back.setFixedSize(QSize(32, 30))
        self._btn_back.setToolTip(tr("sftp.toolbar.back"))
        self._btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back.clicked.connect(self._on_back)
        tb_layout.addWidget(self._btn_back)

        self._btn_forward = QPushButton("→")
        self._btn_forward.setObjectName("sftpNavBtn")
        self._btn_forward.setFixedSize(QSize(32, 30))
        self._btn_forward.setToolTip(tr("sftp.toolbar.forward"))
        self._btn_forward.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_forward.clicked.connect(self._on_forward)
        tb_layout.addWidget(self._btn_forward)

        self._btn_up = QPushButton("↑")
        self._btn_up.setObjectName("sftpNavBtn")
        self._btn_up.setFixedSize(QSize(32, 30))
        self._btn_up.setToolTip(tr("sftp.toolbar.up"))
        self._btn_up.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_up.clicked.connect(self._on_up)
        tb_layout.addWidget(self._btn_up)

        self._btn_refresh = QPushButton()
        self._btn_refresh.setObjectName("sftpNavBtn")
        self._btn_refresh.setFixedSize(QSize(32, 30))
        self._btn_refresh.setIcon(svg_icon("refresh", icon_color, 15))
        self._btn_refresh.setIconSize(QSize(15, 15))
        self._btn_refresh.setToolTip(tr("sftp.toolbar.refresh"))
        self._btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_refresh.clicked.connect(self._on_refresh)
        tb_layout.addWidget(self._btn_refresh)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFixedWidth(1)
        sep1.setObjectName("sftpSep")
        tb_layout.addWidget(sep1)
        tb_layout.addSpacing(4)

        self._path_edit = QLineEdit(self._current_path)
        self._path_edit.setObjectName("sftpPathEdit")
        self._path_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._path_edit.returnPressed.connect(self._on_path_entered)
        tb_layout.addWidget(self._path_edit)

        tb_layout.addSpacing(4)
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFixedWidth(1)
        sep2.setObjectName("sftpSep")
        tb_layout.addWidget(sep2)
        tb_layout.addSpacing(4)

        self._btn_upload = QPushButton()
        self._btn_upload.setObjectName("sftpActionBtn")
        self._btn_upload.setFixedSize(QSize(32, 30))
        self._btn_upload.setIcon(svg_icon("plus", icon_color, 15))
        self._btn_upload.setIconSize(QSize(15, 15))
        self._btn_upload.setToolTip(tr("sftp.toolbar.upload"))
        self._btn_upload.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_upload.clicked.connect(self._on_upload)
        tb_layout.addWidget(self._btn_upload)

        self._btn_mkdir = QPushButton()
        self._btn_mkdir.setObjectName("sftpActionBtn")
        self._btn_mkdir.setFixedSize(QSize(32, 30))
        self._btn_mkdir.setIcon(svg_icon("folder", icon_color, 15))
        self._btn_mkdir.setIconSize(QSize(15, 15))
        self._btn_mkdir.setToolTip(tr("sftp.toolbar.mkdir"))
        self._btn_mkdir.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mkdir.clicked.connect(self._on_mkdir)
        tb_layout.addWidget(self._btn_mkdir)

        root.addWidget(toolbar)

        # Tree view
        self._model = SftpFileModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self._tree = QTreeView()
        self._tree.setObjectName("sftpTree")
        self._tree.setModel(self._proxy)
        self._tree.setSortingEnabled(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setUniformRowHeights(True)
        self._tree.doubleClicked.connect(self._on_double_click)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        header = self._tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(_COL_SIZE,  90)
        header.resizeSection(_COL_MTIME, 140)
        header.resizeSection(_COL_PERMS, 110)
        header.resizeSection(_COL_TYPE,  75)
        header.setSortIndicator(_COL_NAME, Qt.SortOrder.AscendingOrder)

        root.addWidget(self._tree, stretch=1)

        # Status bar
        self._status_lbl = QLabel(tr("sftp.status.connecting"))
        self._status_lbl.setObjectName("sftpStatusLbl")
        self._status_lbl.setContentsMargins(8, 3, 8, 3)
        root.addWidget(self._status_lbl)

        self._update_nav_buttons()

    # ── Theme ────────────────────────────────────────────────────────────────

    def set_theme(self, theme: str) -> None:
        self._theme = theme
        self.set_dialog_theme(theme)
        self._apply_theme_stylesheet()
        from src.theme_manager import get_theme_manager
        color = get_theme_manager().get_colors(theme)["muted"]
        self._btn_refresh.setIcon(svg_icon("refresh", color, 15))
        self._btn_upload.setIcon(svg_icon("plus", color, 15))
        self._btn_mkdir.setIcon(svg_icon("folder", color, 15))

    def _apply_theme_stylesheet(self) -> None:
        from src.theme_manager import get_theme_manager
        colors = get_theme_manager().get_colors(self._theme)
        bg = colors["background"]
        surface = colors["surface"]
        text = colors["text"]
        accent = colors["accent"]
        border = colors["border"]
        muted = colors["muted"]
        alt_row = bg
        sep_col = border

        self._fdlg_content.setStyleSheet(f"""
            QWidget#sftpToolbar {{
                background-color: {bg};
                border-bottom: 1px solid {border};
            }}
            QFrame#sftpSep {{
                background-color: {sep_col};
                border: none;
            }}
            QLineEdit#sftpPathEdit {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 6px 10px;
                min-height: 22px;
                font-size: 12px;
            }}
            QLineEdit#sftpPathEdit:focus {{
                border-color: {accent};
            }}
            QPushButton#sftpNavBtn, QPushButton#sftpActionBtn {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                font-size: 13px;
                padding: 4px 8px;
            }}
            QPushButton#sftpNavBtn:hover, QPushButton#sftpActionBtn:hover {{
                border-color: {accent};
            }}
            QPushButton#sftpNavBtn:disabled {{
                color: {muted};
                border-color: {border};
            }}
            QTreeView#sftpTree {{
                background-color: {surface};
                alternate-background-color: {alt_row};
                color: {text};
                border: none;
                gridline-color: {border};
                font-size: 12px;
            }}
            QTreeView#sftpTree::item {{
                padding: 6px 8px;
                border-bottom: 1px solid {border};
            }}
            QTreeView#sftpTree::item:selected {{
                background-color: {accent};
                color: #ffffff;
            }}
            QTreeView#sftpTree::item:hover {{
                background-color: {bg};
            }}
            QHeaderView::section {{
                background-color: {bg};
                color: {text};
                border: none;
                border-bottom: 1px solid {border};
                border-right: 1px solid {border};
                padding: 8px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#sftpStatusLbl {{
                background-color: {bg};
                color: {muted};
                font-size: 11px;
                border-top: 1px solid {border};
            }}
        """)

    # ── Connection ───────────────────────────────────────────────────────────

    def _error_title(self) -> str:
        """'SFTP Error' / 'FTPS Error' – used for all generic error popups."""
        return tr(
            "ftp.error.title" if self._is_ftp else "sftp.error.title",
            proto=self._conn.protocol_label,
        )

    def _connect_and_load(self) -> None:
        if self._close_requested:
            return
        self._set_status(tr("sftp.status.connecting"))
        # FTP has no host keys — only SFTP needs the trust-on-first-use prompt.
        tofu = None if self._is_ftp else self._tofu_callback
        worker = SftpConnectWorker(self._client, self._conn, tofu)
        worker.connected.connect(self._on_connected)
        worker.error.connect(self._on_connect_error)
        self._track_worker(worker)
        worker.finished.connect(lambda: self._remove_worker_when_stopped(worker))
        worker.start()

    def _on_connected(self) -> None:
        if self._close_requested:
            return
        self._navigate_to(self._current_path, push_history=False)

    def _on_connect_error(self, msg: str) -> None:
        if self._close_requested:
            return
        self._set_status(tr("sftp.status.connect_failed"))
        StyledMessageBox.critical(self, tr("sftp.error.connect_title"), msg)

    def _tofu_callback(self, host: str, port: int, fingerprint: str) -> bool:
        """Dispatch host-key confirmation to the GUI thread and wait for choice."""
        if self._close_requested:
            return False
        if QThread.currentThread() == self.thread():
            return self._show_tofu_dialog(host, fingerprint)
        state = {"event": threading.Event(), "accepted": False, "cancelled": False}
        self._pending_tofu_requests.append(state)
        try:
            self._tofu_requested.emit(host, port, fingerprint, state)
            if not state["event"].wait(timeout=60):
                state["cancelled"] = True
                return False
            return state["accepted"]
        finally:
            if state in self._pending_tofu_requests:
                self._pending_tofu_requests.remove(state)

    def _show_tofu_dialog(self, host: str, fingerprint: str) -> bool:
        return StyledMessageBox.question(
            self,
            tr("sftp.tofu.title"),
            tr("sftp.tofu.body", host=host, fingerprint=fingerprint),
            yes_text=tr("dialog.yes"),
            no_text=tr("dialog.no"),
        )

    @pyqtSlot(str, int, str, object)
    def _dispatch_tofu_dialog(self, host: str, port: int, fingerprint: str, state) -> None:
        try:
            if not self._close_requested and not state.get("cancelled", False):
                state["accepted"] = self._show_tofu_dialog(host, fingerprint)
        finally:
            state["event"].set()

    # ── Navigation ───────────────────────────────────────────────────────────

    def _navigate_to(self, path: str, *, push_history: bool = True) -> None:
        if push_history and self._current_path and self._current_path != path:
            self._history.append(self._current_path)
            self._forward_stack.clear()
        self._current_path = path
        self._path_edit.setText(path)
        self._load_directory(path)
        self._update_nav_buttons()

    def _on_back(self) -> None:
        if not self._history:
            return
        self._forward_stack.append(self._current_path)
        prev = self._history.pop()
        self._navigate_to(prev, push_history=False)

    def _on_forward(self) -> None:
        if not self._forward_stack:
            return
        self._history.append(self._current_path)
        nxt = self._forward_stack.pop()
        self._navigate_to(nxt, push_history=False)

    def _on_up(self) -> None:
        parent = self._current_path.rstrip("/").rsplit("/", 1)[0] or "/"
        if parent != self._current_path:
            self._navigate_to(parent)

    def _on_refresh(self) -> None:
        self._load_directory(self._current_path)

    def _on_path_entered(self) -> None:
        path = self._path_edit.text().strip() or "/"
        self._navigate_to(path)

    def _update_nav_buttons(self) -> None:
        self._btn_back.setEnabled(bool(self._history))
        self._btn_forward.setEnabled(bool(self._forward_stack))
        self._btn_up.setEnabled(self._current_path not in ("/", ""))

    # ── Directory loading ────────────────────────────────────────────────────

    def _load_directory(self, path: str) -> None:
        if self._close_requested:
            return
        if self._active_list_worker and self._active_list_worker.isRunning():
            try:
                self._active_list_worker.finished.disconnect()
                self._active_list_worker.error.disconnect()
            except Exception:
                pass
            self._active_list_worker = None

        self._set_status(tr("sftp.status.loading"))
        worker = SftpListWorker(self._client, path)
        worker.finished.connect(self._on_list_finished)
        worker.error.connect(self._on_list_error)
        self._active_list_worker = worker
        self._track_worker(worker)
        worker.finished.connect(lambda *_: self._remove_worker_when_stopped(worker))
        worker.error.connect(lambda *_: self._remove_worker_when_stopped(worker))
        worker.start()

    def _on_list_finished(self, path: str, entries: list) -> None:
        self._active_list_worker = None
        self._model.set_entries(entries)
        count = len(entries)
        self._set_status(tr("sftp.status.items", count=count))

    def _on_list_error(self, path: str, msg: str) -> None:
        self._active_list_worker = None
        self._set_status(tr("sftp.status.error", msg=msg))
        StyledMessageBox.critical(self, self._error_title(), msg)

    # ── Double click ─────────────────────────────────────────────────────────

    def _on_double_click(self, proxy_index: QModelIndex) -> None:
        src_index = self._proxy.mapToSource(proxy_index)
        entry = self._model.entry_at(src_index.row())
        if entry is None:
            return
        if entry.is_dir:
            self._navigate_to(entry.path)
        else:
            self._download_and_open(entry)

    # ── Download ─────────────────────────────────────────────────────────────

    def _download_and_open(self, entry: SftpEntry) -> None:
        # Remote names are untrusted even when the SSH host itself is trusted.
        try:
            validate_remote_filename(entry.name)
        except ValueError as error:
            StyledMessageBox.critical(self, self._error_title(), str(error))
            return
        tmp_dir = tempfile.mkdtemp(prefix="neossh_sftp_")
        try:
            local_path = confined_download_path(tmp_dir, entry.name)
        except (OSError, ValueError) as error:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            StyledMessageBox.critical(self, self._error_title(), str(error))
            return
        remote_path = entry.path

        worker = SftpDownloadWorker(self._client, remote_path, local_path)
        self._track_worker(worker)

        fname = entry.name
        prog = QProgressDialog(
            tr("sftp.progress.downloading", name=fname),
            tr("dialog.cancel"), 0, 100, self,
        )
        prog.setWindowTitle(tr("sftp.error.download_title"))
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)

        def _on_progress(done: int, total: int) -> None:
            if total > 0:
                prog.setValue(int(done * 100 / total))

        def _cleanup_temp_download() -> None:
            self._edit_map.pop(local_path, None)
            self._edit_uploaded_versions.pop(local_path, None)
            self._edit_generations.pop(local_path, None)
            if local_path in self._watcher.files():
                self._watcher.removePath(local_path)
            shutil.rmtree(tmp_dir, ignore_errors=True)

        def _on_finished(lp: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            if self._close_requested:
                _cleanup_temp_download()
                return
            # Register for edit-watch before opening so we catch the first save
            self._edit_map[lp] = remote_path
            self._edit_uploaded_versions[lp] = self._edit_version(lp)
            self._watcher.addPath(lp)
            try:
                os.startfile(lp)
            except Exception as e:
                _cleanup_temp_download()
                StyledMessageBox.critical(self, self._error_title(), str(e))

        def _on_error(msg: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            _cleanup_temp_download()
            StyledMessageBox.critical(self, tr("sftp.error.download_title"), msg)

        def _on_cancelled() -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            _cleanup_temp_download()

        prog.canceled.connect(worker.cancel)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.cancelled.connect(_on_cancelled)
        worker.start()

    def _edit_version(self, local_path: str) -> tuple:
        """Cheap conservative identity; watcher generations catch equal-stat saves."""
        info = os.stat(local_path)
        return (info.st_mtime_ns, info.st_size, info.st_ino,
                self._edit_generations.get(local_path, 0))

    def _queue_edit(self, local_path: str, remote_path: str) -> None:
        item = (local_path, remote_path)
        if item not in self._close_pending_edits:
            self._close_pending_edits.append(item)

    def _restore_edit_watch(self, local_path: str) -> None:
        # Atomic-replace saves remove QFileSystemWatcher's file registration.
        if (local_path in self._edit_map and os.path.exists(local_path)
                and local_path not in self._watcher.files()):
            self._watcher.addPath(local_path)

    def _schedule_edit_upload(self, local_path: str, remote_path: str) -> None:
        old = self._edit_timers.pop(local_path, None)
        if old is not None:
            old.stop()
            old.deleteLater()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._upload_edited_file(local_path, remote_path))
        self._edit_timers[local_path] = timer
        timer.start(500)

    def _on_watched_file_changed(self, local_path: str) -> None:
        """Retain saves during flush/disconnect instead of dropping late edits."""
        remote_path = self._edit_map.get(local_path)
        if not remote_path:
            return
        self._edit_generations[local_path] = self._edit_generations.get(local_path, 0) + 1
        self._close_last_change_at = time.monotonic()
        self._restore_edit_watch(local_path)
        QTimer.singleShot(200, lambda: self._restore_edit_watch(local_path))
        if self._close_requested or self._disconnect_thread is not None or self._edit_reconnecting:
            self._queue_edit(local_path, remote_path)
            if self._disconnect_thread is not None:
                self._shutdown_recovery_reason = "Local edits changed while disconnecting; shutdown cancelled"
            return
        self._schedule_edit_upload(local_path, remote_path)

    def _upload_edited_file(self, local_path: str, remote_path: str) -> None:
        """Upload a START version, never claim a later local save was uploaded."""
        timer = self._edit_timers.pop(local_path, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if (self._disconnect_thread is not None or self._edit_reconnecting
                or self._edit_reconnect_failed):
            self._queue_edit(local_path, remote_path)
            return
        if local_path in self._edit_uploads:
            self._queue_edit(local_path, remote_path)
            return
        try:
            start_version = self._edit_version(local_path)
        except OSError:
            if self._close_requested:
                reason = f"Edited local file is missing; shutdown cancelled: {local_path}"
                self._abort_shutdown(reason, local_path, remote_path)
                StyledMessageBox.critical(self, tr("sftp.error.upload_title"), reason)
            return
        if self._edit_uploaded_versions.get(local_path) == start_version:
            return
        fname = os.path.basename(local_path)
        worker = SftpUploadWorker(self._client, local_path, remote_path)
        self._edit_uploads.add(local_path)
        self._track_worker(worker)

        def _on_finished(_remote: str) -> None:
            self._edit_uploads.discard(local_path)
            self._edit_uploaded_versions[local_path] = start_version
            self._remove_worker_when_stopped(worker)
            self._set_status(tr("sftp.status.saved", name=fname))
            try:
                changed = self._edit_version(local_path) != start_version
            except OSError:
                changed = True
            if changed:
                self._close_last_change_at = time.monotonic()
                if self._close_requested:
                    self._queue_edit(local_path, remote_path)
                else:
                    self._schedule_edit_upload(local_path, remote_path)
            if not self._close_requested:
                self._on_refresh()

        def _on_error(msg: str) -> None:
            self._edit_uploads.discard(local_path)
            self._queue_edit(local_path, remote_path)
            self._remove_worker_when_stopped(worker)
            if self._close_requested:
                self._abort_shutdown(msg, local_path, remote_path)
            StyledMessageBox.critical(
                self, tr("sftp.error.upload_title"),
                tr("sftp.error.edit_upload_failed", name=fname, msg=msg),
            )

        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.start()

    def _start_download(
        self, remote_path: str, local_path: str, open_after: bool = False
    ) -> None:
        fname = os.path.basename(remote_path)
        prog = QProgressDialog(
            tr("sftp.progress.downloading", name=fname),
            tr("dialog.cancel"),
            0, 100, self,
        )
        prog.setWindowTitle(tr("sftp.error.download_title"))
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)

        worker = SftpDownloadWorker(self._client, remote_path, local_path)
        self._track_worker(worker)

        def _on_progress(done: int, total: int) -> None:
            if total > 0:
                prog.setValue(int(done * 100 / total))

        def _on_finished(lp: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            if open_after and not self._close_requested:
                try:
                    os.startfile(lp)
                except Exception as e:
                    StyledMessageBox.critical(self, self._error_title(), str(e))

        def _on_error(msg: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            StyledMessageBox.critical(self, tr("sftp.error.download_title"), msg)

        def _on_cancelled() -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)

        prog.canceled.connect(worker.cancel)
        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.cancelled.connect(_on_cancelled)
        worker.start()

    # ── Upload ───────────────────────────────────────────────────────────────

    def _on_upload(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            tr("sftp.upload.select"),
            "",
            tr("sftp.upload.filter"),
        )
        for local_path in paths:
            fname = os.path.basename(local_path)
            remote_path = self._current_path.rstrip("/") + "/" + fname
            self._start_upload(local_path, remote_path)

    def _start_upload(self, local_path: str, remote_path: str) -> None:
        fname = os.path.basename(local_path)
        prog = QProgressDialog(
            tr("sftp.progress.uploading", name=fname),
            "",
            0, 100, self,
        )
        prog.setWindowTitle(tr("sftp.error.upload_title"))
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)

        worker = SftpUploadWorker(self._client, local_path, remote_path)
        self._track_worker(worker)

        def _on_progress(done: int, total: int) -> None:
            if total > 0:
                prog.setValue(int(done * 100 / total))

        def _on_finished(_remote: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            self._on_refresh()

        def _on_error(msg: str) -> None:
            prog.close()
            self._remove_worker_when_stopped(worker)
            StyledMessageBox.critical(self, tr("sftp.error.upload_title"), msg)

        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.start()

    # ── Context menu ─────────────────────────────────────────────────────────

    def _on_context_menu(self, pos) -> None:
        proxy_index = self._tree.indexAt(pos)
        if not proxy_index.isValid():
            return
        src_index = self._proxy.mapToSource(proxy_index)
        entry = self._model.entry_at(src_index.row())
        if entry is None:
            return

        icon_color = "#aab4c4" if self._theme == "dark" else "#4a5a6a"
        menu = QMenu(self)

        act_dl = None
        if not entry.is_dir:
            act_dl = menu.addAction(
                svg_icon("arrow-right", icon_color, 15),
                tr("sftp.menu.download"),
            )
        act_rename = menu.addAction(svg_icon("edit",  icon_color, 15), tr("sftp.menu.rename"))
        act_delete = menu.addAction(svg_icon("trash", "#ef4444",   15), tr("sftp.menu.delete"))
        menu.addSeparator()
        act_mkdir  = menu.addAction(svg_icon("folder", icon_color, 15), tr("sftp.menu.new_folder"))
        act_upload = menu.addAction(svg_icon("plus",   icon_color, 15), tr("sftp.menu.upload"))

        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))

        if act_dl and chosen == act_dl:
            save_path, _ = QFileDialog.getSaveFileName(
                self, tr("sftp.upload.select"), entry.name
            )
            if save_path:
                self._start_download(entry.path, save_path, open_after=False)
        elif chosen == act_rename:
            self._on_rename(entry)
        elif chosen == act_delete:
            self._on_delete(entry)
        elif chosen == act_mkdir:
            self._on_mkdir()
        elif chosen == act_upload:
            self._on_upload()

    # ── CRUD operations ──────────────────────────────────────────────────────

    def _on_delete(self, entry: SftpEntry) -> None:
        confirmed = StyledMessageBox.question(
            self,
            tr("sftp.delete.title"),
            tr("sftp.delete.confirm", name=entry.name),
            yes_text=tr("dialog.yes"),
            no_text=tr("dialog.no"),
            destructive=True,
        )
        if not confirmed:
            return
        worker = SftpDeleteWorker(self._client, entry.path, entry.is_dir)
        self._track_worker(worker)
        worker.finished.connect(lambda _: (
            self._remove_worker_when_stopped(worker),
            self._on_refresh(),
        ))
        worker.error.connect(lambda msg: (
            self._remove_worker_when_stopped(worker),
            StyledMessageBox.critical(self, tr("sftp.error.delete_title"), msg),
        ))
        worker.start()

    def _on_rename(self, entry: SftpEntry) -> None:
        new_name, ok = StyledInputDialog.get_text(
            self, tr("sftp.rename.title"), tr("sftp.rename.prompt"), entry.name
        )
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        parent = entry.path.rsplit("/", 1)[0] or "/"
        new_path = parent.rstrip("/") + "/" + new_name.strip()
        worker = SftpRenameWorker(self._client, entry.path, new_path)
        self._track_worker(worker)
        worker.finished.connect(lambda *_: (
            self._remove_worker_when_stopped(worker),
            self._on_refresh(),
        ))
        worker.error.connect(lambda msg: (
            self._remove_worker_when_stopped(worker),
            StyledMessageBox.critical(self, tr("sftp.error.rename_title"), msg),
        ))
        worker.start()

    def _on_mkdir(self) -> None:
        name, ok = StyledInputDialog.get_text(
            self, tr("sftp.mkdir.title"), tr("sftp.mkdir.prompt")
        )
        if not ok or not name.strip():
            return
        new_path = self._current_path.rstrip("/") + "/" + name.strip()
        worker = SftpMkdirWorker(self._client, new_path)
        self._track_worker(worker)
        worker.finished.connect(lambda _: (
            self._remove_worker_when_stopped(worker),
            self._on_refresh(),
        ))
        worker.error.connect(lambda msg: (
            self._remove_worker_when_stopped(worker),
            StyledMessageBox.critical(self, tr("sftp.error.mkdir_title"), msg),
        ))
        worker.start()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _track_worker(self, worker) -> None:
        self._workers.append(worker)
        if isinstance(worker, (SftpDownloadWorker, SftpUploadWorker)):
            self._pending_transfer_results.add(worker)
        if not self._worker_reaper_timer.isActive():
            self._worker_reaper_timer.start()

    def _remove_worker_when_stopped(self, worker) -> None:
        # Result signals may be emitted from run() just before QThread stops.
        # Keep the reference until isRunning() is actually false; the GUI timer
        # below will reap it if this callback arrives too early or off-thread.
        if QThread.currentThread() != self.thread():
            return
        self._pending_transfer_results.discard(worker)
        try:
            if worker.isRunning() or not worker.wait(0):
                return
        except RuntimeError:
            pass
        if worker in self._workers:
            self._workers.remove(worker)
        if not self._workers:
            self._worker_reaper_timer.stop()

    def _reap_stopped_workers(self) -> None:
        for worker in list(self._workers):
            try:
                running = worker.isRunning() or not worker.wait(0)
            except RuntimeError:
                running = False
            if (not running and worker not in self._pending_transfer_results
                    and worker in self._workers):
                self._workers.remove(worker)
        if not self._workers:
            self._worker_reaper_timer.stop()

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def keyPressEvent(self, event) -> None:
        # QDialog would close/accept on Enter and Escape — suppress both so the
        # path bar's returnPressed and the tree's keyboard navigation work normally.
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            event.ignore()
            return
        super().keyPressEvent(event)

    # ── Close ────────────────────────────────────────────────────────────────

    def request_shutdown(self) -> None:
        if not self._close_ready:
            self.close()

    def is_shutdown_complete(self) -> bool:
        return self._close_ready and self._disconnect_done.is_set()

    def cancel_shutdown(self) -> None:
        if self._close_ready:
            return
        if self._disconnect_thread is not None:
            # Never reconnect before the actual disconnect has finished.
            self._shutdown_recovery_reason = "Shutdown cancelled while disconnecting"
            return
        was_closing = self._close_requested
        self._close_requested = False
        if self._close_wait_timer:
            self._close_wait_timer.stop()
        self._fdlg_content.setEnabled(not self._edit_reconnecting)
        for path, remote in self._edit_map.items():
            self._restore_edit_watch(path)
            if was_closing and not self._aborting_shutdown and not self._edit_reconnecting:
                try:
                    dirty = self._edit_uploaded_versions.get(path) != self._edit_version(path)
                except OSError:
                    continue
                if dirty:
                    self._schedule_edit_upload(path, remote)

    def _resume_after_disconnect(self, reason: str) -> None:
        """Recover the still-open editor session entirely through a Qt worker."""
        self._disconnect_thread = None
        self._disconnect_done.clear()
        self._disconnect_versions.clear()
        self._shutdown_recovery_reason = None
        self._close_requested = False
        self._close_ready = False
        self._close_wait_timer.stop()
        self._edit_reconnecting = True
        self._edit_reconnect_failed = False
        self._fdlg_content.setEnabled(False)
        # MainWindow cancels its own exit/unmount phase synchronously here.
        self.shutdown_failed.emit(reason)
        tofu = None if self._is_ftp else self._tofu_callback
        worker = SftpConnectWorker(self._client, self._conn, tofu)
        self._track_worker(worker)

        def connected() -> None:
            self._edit_reconnecting = False
            self._remove_worker_when_stopped(worker)
            self._fdlg_content.setEnabled(not self._close_requested)
            for path, remote in self._edit_map.items():
                self._restore_edit_watch(path)
                try:
                    dirty = self._edit_uploaded_versions.get(path) != self._edit_version(path)
                except OSError:
                    dirty = True
                if dirty:
                    self._queue_edit(path, remote)
                    if not self._close_requested:
                        self._schedule_edit_upload(path, remote)

        def failed(message: str) -> None:
            self._edit_reconnecting = False
            self._edit_reconnect_failed = True
            self._remove_worker_when_stopped(worker)
            self._fdlg_content.setEnabled(True)
            self.cancel_shutdown()
            self.shutdown_failed.emit(message)
            StyledMessageBox.critical(self, tr("sftp.error.connect_title"), message)

        worker.connected.connect(connected)
        worker.error.connect(failed)
        worker.finished.connect(lambda: self._remove_worker_when_stopped(worker))
        worker.start()

    def _abort_shutdown(self, reason: str, local_path: str, remote_path: str) -> None:
        item = (local_path, remote_path)
        if item not in self._close_pending_edits:
            self._close_pending_edits.insert(0, item)
        self._aborting_shutdown = True
        try:
            self.cancel_shutdown()
        finally:
            self._aborting_shutdown = False
        self.shutdown_failed.emit(reason)

    def _disconnect_client(self) -> None:
        try:
            self._client.disconnect()
        except Exception as error:
            self._disconnect_error = error
            logger.warning("File browser disconnect failed: %s", error)
        finally:
            self._disconnect_done.set()

    def reject(self) -> None:
        # Titlebar rejection must use the same asynchronous shutdown gate.
        if self._close_ready:
            super().reject()
        else:
            self.close()

    def done(self, result: int) -> None:
        if self._close_ready:
            super().done(result)
        else:
            self.close()

    def closeEvent(self, event) -> None:
        if self._close_ready:
            if self._close_wait_timer:
                self._close_wait_timer.stop()
            if self._worker_reaper_timer:
                self._worker_reaper_timer.stop()
            if self._watcher.files():
                self._watcher.removePaths(self._watcher.files())
            self._edit_map.clear()
            self._workers.clear()
            self._active_list_worker = None
            # The idle gate has already awaited the disconnect thread.
            super().closeEvent(event)
            return

        # Keep the window alive until pending edits have been uploaded and all
        # workers have stopped; disconnecting a shared Paramiko client mid-I/O
        # can corrupt remote uploads.
        event.ignore()
        if not self._close_requested:
            self._close_requested = True
            self._close_last_change_at = time.monotonic()
            self._close_observed_versions.clear()
            self._fdlg_content.setEnabled(False)
            for state in list(self._pending_tofu_requests):
                state["accepted"] = False
                state["cancelled"] = True
                state["event"].set()
            pending_edits = [
                (path, self._edit_map.get(path)) for path in self._edit_timers
            ]
            for timer in self._edit_timers.values():
                timer.stop()
            self._edit_timers.clear()
            # Keep edit watches alive through flush AND the disconnect thread.
            for local_path, remote_path in pending_edits:
                item = (local_path, remote_path)
                if remote_path and item not in self._close_pending_edits:
                    self._close_pending_edits.append(item)

        if self._close_wait_timer is None:
            self._close_wait_timer = QTimer(self)
            self._close_wait_timer.setInterval(100)
            self._close_wait_timer.timeout.connect(self._finish_close_when_idle)
        if not self._close_wait_timer.isActive():
            self._close_wait_timer.start()

    def _scan_close_edits(self) -> str | None:
        """Rescan every round: watcher events can be coalesced or lost on replace."""
        for path, remote in self._edit_map.items():
            self._restore_edit_watch(path)
            try:
                version = self._edit_version(path)
            except OSError:
                self._queue_edit(path, remote)
                return f"Edited local file is missing; shutdown cancelled: {path}"
            if self._close_observed_versions.get(path) != version:
                self._close_observed_versions[path] = version
                self._close_last_change_at = time.monotonic()
            if self._edit_uploaded_versions.get(path) != version and path not in self._edit_uploads:
                self._queue_edit(path, remote)
            if (self._disconnect_thread is not None
                    and self._disconnect_versions.get(path) != version):
                self._shutdown_recovery_reason = "Local edits changed while disconnecting; shutdown cancelled"
        return None

    def _finish_close_when_idle(self) -> None:
        if not self._close_requested:
            return
        problem = self._scan_close_edits()
        if (self._pending_transfer_results
                or any(worker.isRunning() or not worker.wait(0) for worker in self._workers)):
            return
        if self._disconnect_thread is not None:
            if problem:
                self._shutdown_recovery_reason = problem
            if not self._disconnect_done.is_set():
                return
            if self._shutdown_recovery_reason:
                self._resume_after_disconnect(self._shutdown_recovery_reason)
                return
            self._close_ready = True
            if self._close_wait_timer:
                self._close_wait_timer.stop()
            self.close()
            return
        if problem:
            path, remote = self._close_pending_edits[-1]
            self._abort_shutdown(problem, path, remote)
            StyledMessageBox.critical(self, tr("sftp.error.upload_title"), problem)
            return
        if self._edit_reconnect_failed:
            self.cancel_shutdown()
            self.shutdown_failed.emit("Reconnect failed; local edits retained")
            return
        if self._edit_reconnecting:
            return
        if self._close_pending_edits:
            local_path, remote_path = self._close_pending_edits.pop(0)
            self._upload_edited_file(local_path, remote_path)
            return
        # Watcher/stat changes reset this clock. No disconnect until every
        # successful START version is current and the editor has been quiet.
        if time.monotonic() - self._close_last_change_at < 0.5:
            return
        self._disconnect_versions = dict(self._close_observed_versions)
        self._disconnect_error = None
        self._disconnect_done.clear()
        self._disconnect_thread = threading.Thread(
            target=self._disconnect_client, daemon=True, name="SftpDisconnect"
        )
        self._disconnect_thread.start()
