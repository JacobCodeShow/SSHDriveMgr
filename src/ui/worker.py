"""Qt workers: payload results and native QThread completion are distinct."""
from PyQt6.QtCore import QThread, pyqtSignal
from src.sshfs_controller import SSHFSController, MountResult
from src.config import Connection


class MountWorker(QThread):
    result_ready = pyqtSignal(str, MountResult)

    def __init__(self, conn: Connection, controller: SSHFSController, disable_cache: bool = False):
        super().__init__()
        self.conn = conn
        self.controller = controller
        self.disable_cache = disable_cache

    def run(self):
        try:
            result = self.controller.mount(self.conn, disable_cache=self.disable_cache)
        except Exception as error:
            result = MountResult(False, f"Thread Error: {error}")
        self.result_ready.emit(self.conn.id, result)


class UnmountWorker(QThread):
    result_ready = pyqtSignal(str, MountResult)

    def __init__(self, conn_id: str, drive_letter: str, controller: SSHFSController):
        super().__init__()
        self.conn_id = conn_id
        self.drive_letter = drive_letter
        self.controller = controller

    def run(self):
        try:
            result = self.controller.unmount(self.drive_letter)
        except Exception as error:
            result = MountResult(False, f"Thread Error: {error}")
        self.result_ready.emit(self.conn_id, result)


class TerminalConnectWorker(QThread):
    result_ready = pyqtSignal(str, object)

    def __init__(self, bridge_server, session_key: str, conn):
        super().__init__()
        self.bridge_server = bridge_server
        self.session_key = session_key
        self.conn = conn

    def run(self):
        try:
            token = self.bridge_server.create_session_token(self.session_key, self.conn)
        except Exception:
            token = None
        self.result_ready.emit(self.session_key, token)


class ShutdownUnmountWorker(QThread):
    """Unmount only this manager's recorded drives, never global process names."""
    result_ready = pyqtSignal(list)

    def __init__(self, mounts, controller: SSHFSController):
        super().__init__()
        self.mounts = list(mounts)
        self.controller = controller

    def run(self):
        failures = []
        for conn_id, drive_letter in self.mounts:
            try:
                result = self.controller.unmount(drive_letter)
                if not result.success:
                    failures.append((conn_id, drive_letter, result.message))
            except Exception as error:
                failures.append((conn_id, drive_letter, str(error)))
        self.result_ready.emit(failures)
