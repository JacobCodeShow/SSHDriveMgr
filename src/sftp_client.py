"""
sftp_client.py – Synchronous SFTP client wrapping paramiko.

All public methods raise SftpClientError on failure. Intended to be called
exclusively from QThread workers so the Qt UI thread is never blocked.
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from functools import wraps
from typing import Callable, Optional

import paramiko

from src.app_logger import logger
from src.config import Connection
from src.ui.host_key_utils import TOFUAcceptPolicy


@dataclass
class SftpEntry:
    """Metadata for a single remote file or directory."""

    name: str
    path: str           # absolute remote path
    size: int           # bytes; 0 for directories
    modified: float     # Unix timestamp
    permissions: str    # e.g. "drwxr-xr-x"
    is_dir: bool


class SftpClientError(Exception):
    """Raised for all SFTP-level errors (auth failures, network, IO)."""


def _serialized(method):
    """Keep whole operations (including callbacks) on the shared channel ordered."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class SftpClient:
    """
    Synchronous SFTP wrapper around paramiko.SSHClient + paramiko.SFTPClient.

    Create one instance per browser window. Call connect() before any other
    operation; call disconnect() when the window closes. Lifecycle and operations
    share one RLock because Paramiko SFTP responses must not be interleaved.
    Call them only from workers (or disconnect after all workers are idle);
    is_connected is a non-blocking status snapshot suitable for the GUI.
    """

    def __init__(self) -> None:
        self._ssh: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._connected: bool = False
        self._lock = threading.RLock()

    # ── Connection lifecycle ────────────────────────────────────────────────

    @_serialized
    def connect(
        self,
        conn: Connection,
        *,
        tofu_callback: Optional[Callable[[str, int, str], bool]] = None,
    ) -> None:
        """
        Establish SSH + SFTP session using credentials from conn.

        tofu_callback(host, port, fingerprint) -> bool is called when the host
        is not in known_hosts. Return True to accept and save the key.
        Raises SftpClientError on any failure.
        """
        # Replacing a session must not orphan its old SSH transport.
        self.disconnect()
        client = None
        password = ""
        try:
            known_hosts = os.path.expanduser(r"~\.ssh\known_hosts")
            client = paramiko.SSHClient()
            # Always load existing pins, including hashed entries. External
            # ssh-keygen availability must never determine whether we trust a key.
            if os.path.exists(known_hosts):
                client.load_host_keys(known_hosts)
            target = conn.host if conn.port == 22 else f"[{conn.host}]:{conn.port}"
            if client.get_host_keys().lookup(target) or tofu_callback is None:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                client.set_missing_host_key_policy(
                    TOFUAcceptPolicy(known_hosts, conn.host, conn.port, tofu_callback)
                )

            user = getattr(conn, "user", "") or getattr(conn, "ssh_user", "") or ""
            password = getattr(conn, "password", "") or ""
            key_path = getattr(conn, "key_path", "") or ""
            putty_key_path = getattr(conn, "putty_key_path", "") or ""
            if conn.auth_method == "key" and (key_path or putty_key_path):
                client.connect(
                    conn.host, port=conn.port, username=user,
                    key_filename=key_path or putty_key_path, timeout=15,
                )
            elif conn.auth_method in ("password", "ask") and password:
                client.connect(
                    conn.host, port=conn.port, username=user,
                    password=password, timeout=15,
                )
            else:
                raise SftpClientError("No usable credentials configured")

            sftp = client.open_sftp()
        except BaseException as e:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if isinstance(e, SftpClientError):
                raise
            if isinstance(e, paramiko.AuthenticationException):
                raise SftpClientError("Authentication failed") from e
            if isinstance(e, Exception):
                raise SftpClientError(str(e)) from e
            raise
        finally:
            password = ""   # wipe from local scope

        # Publish only a fully initialized session.
        self._ssh = client
        self._sftp = sftp
        self._connected = True
        logger.debug("SftpClient: connected to %s@%s:%d", user, conn.host, conn.port)

    @_serialized
    def disconnect(self) -> None:
        """Close the SFTP and SSH connections. Safe to call multiple times."""
        try:
            if self._sftp:
                self._sftp.close()
        except Exception:
            pass
        try:
            if self._ssh:
                self._ssh.close()
        except Exception:
            pass
        self._sftp = None
        self._ssh = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        # A non-blocking snapshot: the GUI may read this during a transfer.
        return self._connected

    # ── Directory operations ────────────────────────────────────────────────

    @_serialized
    def list_directory(self, remote_path: str) -> list[SftpEntry]:
        """
        Return sorted directory listing for remote_path.

        Directories come first, then files, both in case-insensitive alphabetical
        order. Raises SftpClientError on failure.
        """
        self._require_connected()
        try:
            attrs_list = self._sftp.listdir_attr(remote_path)
        except Exception as e:
            raise SftpClientError(str(e)) from e

        entries: list[SftpEntry] = []
        for a in attrs_list:
            if a.filename in (".", ".."):
                continue
            full = remote_path.rstrip("/") + "/" + a.filename
            is_dir = stat.S_ISDIR(a.st_mode or 0)
            perm = stat.filemode(a.st_mode or 0)
            entries.append(SftpEntry(
                name=a.filename,
                path=full,
                size=a.st_size or 0,
                modified=float(a.st_mtime or 0),
                permissions=perm,
                is_dir=is_dir,
            ))

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    @_serialized
    def make_directory(self, remote_path: str) -> None:
        """Create a remote directory. Raises SftpClientError on failure."""
        self._require_connected()
        try:
            self._sftp.mkdir(remote_path)
        except Exception as e:
            raise SftpClientError(str(e)) from e

    @_serialized
    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a remote file or directory. Raises SftpClientError on failure."""
        self._require_connected()
        try:
            self._sftp.rename(old_path, new_path)
        except Exception as e:
            raise SftpClientError(str(e)) from e

    @_serialized
    def remove(self, remote_path: str, *, is_dir: bool = False) -> None:
        """
        Delete a remote file or empty directory.

        Note: non-empty directories are not supported (sftp.rmdir requires the
        directory to be empty). Raises SftpClientError on failure.
        """
        self._require_connected()
        try:
            if is_dir:
                self._sftp.rmdir(remote_path)
            else:
                self._sftp.remove(remote_path)
        except Exception as e:
            raise SftpClientError(str(e)) from e

    # ── Transfer operations ─────────────────────────────────────────────────

    @_serialized
    def download(
        self,
        remote_path: str,
        local_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """
        Download remote_path to local_path.

        progress_callback(bytes_transferred, total_bytes) is called periodically
        during the transfer. Raises SftpClientError on failure.
        """
        self._require_connected()
        destination = os.path.abspath(local_path)
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".neossh-download-",
                suffix=".part",
                dir=os.path.dirname(destination),
            )
            os.close(fd)
            self._sftp.get(remote_path, temp_path, callback=progress_callback)
            os.replace(temp_path, destination)
            temp_path = None
        except Exception as e:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise SftpClientError(str(e)) from e

    @_serialized
    def upload(
        self,
        local_path: str,
        remote_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """
        Upload local_path to remote_path.

        progress_callback(bytes_transferred, total_bytes) is called periodically
        during the transfer. Raises SftpClientError on failure.
        """
        self._require_connected()
        try:
            self._sftp.put(local_path, remote_path, callback=progress_callback)
        except Exception as e:
            raise SftpClientError(str(e)) from e

    # ── Internal ────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if not self._connected:
            raise SftpClientError("Not connected to SFTP server")
