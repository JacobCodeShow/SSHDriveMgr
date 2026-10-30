"""Transfer lifecycle regressions: isolated files, mocked wires, no SSH/network.

Uses real Paramiko HostKeys/SSHClient verification and ftplib transfer loops;
only their transports are replaced. Thread ordering uses Events, never sleeps.
"""
from collections import deque
import ftplib
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace
from unittest import mock

import paramiko
import pytest
from PyQt6.QtCore import Qt

from src.config import Connection
from src.ftp_client import FtpClient, FtpClientError
from src.sftp_client import SftpClient, SftpClientError
from src.ui.sftp_worker import SftpDownloadWorker


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """No test can reach the user's SSH directory or a real network/process."""
    known_hosts = tmp_path / ".ssh" / "known_hosts"
    monkeypatch.setattr("src.sftp_client.os.path.expanduser", lambda _: str(known_hosts))
    monkeypatch.setattr(socket, "socket", mock.Mock(side_effect=AssertionError("network forbidden")))
    monkeypatch.setattr(subprocess, "run", mock.Mock(side_effect=AssertionError("SSH tools forbidden")))
    return known_hosts


def connection(protocol="sftp", port=22):
    return Connection("test", "transfer.invalid", "test-user", port=port,
                      password="not-a-real-password", protocol=protocol)


class ObservedRLock:
    """Expose a contender reaching acquire without relying on OS scheduling."""
    def __init__(self):
        self.lock = threading.RLock()
        self.contending = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == "contender":
            self.contending.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


def run_thread(name, action, errors):
    def run():
        try:
            action()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def ssh_mock():
    ssh = mock.Mock()
    ssh.get_host_keys.return_value = paramiko.HostKeys()
    return ssh


@pytest.mark.parametrize("operation", ["list", "mkdir", "rename", "remove", "upload", "download", "disconnect", "connect"])
def test_sftp_entire_operation_and_lifecycle_are_serialized(tmp_path, monkeypatch, operation):
    client = SftpClient()
    wire = mock.Mock()
    ssh = ssh_mock()
    client._sftp, client._ssh, client._connected = wire, ssh, True
    client._lock = ObservedRLock()
    entered, release = threading.Event(), threading.Event()
    errors = []
    source = tmp_path / "source"
    source.write_bytes(b"upload")
    destination = tmp_path / "first"

    def get(remote, local, callback=None):
        if remote == "/first":
            entered.set()
            assert release.wait(5), "test did not release first transfer"
        Path(local).write_bytes(b"download")
        if callback:
            callback(8, 8)
    wire.get.side_effect = get
    wire.listdir_attr.return_value = []
    new_ssh = ssh_mock()
    monkeypatch.setattr("src.sftp_client.paramiko.SSHClient", lambda: new_ssh)
    actions = {
        "list": lambda: client.list_directory("/"),
        "mkdir": lambda: client.make_directory("/new"),
        "rename": lambda: client.rename("/old", "/new"),
        "remove": lambda: client.remove("/file"),
        "upload": lambda: client.upload(str(source), "/file"),
        "download": lambda: client.download("/second", str(tmp_path / "second")),
        "disconnect": client.disconnect,
        "connect": lambda: client.connect(connection()),
    }
    first = run_thread("transfer", lambda: client.download("/first", str(destination)), errors)
    assert entered.wait(5)
    # Status polling from the GUI must never try to acquire the transfer lock.
    assert client.is_connected is True
    second = run_thread("contender", actions[operation], errors)
    try:
        assert client._lock.contending.wait(5)
        assert wire.method_calls == [mock.call.get("/first", mock.ANY, callback=None)]
        ssh.close.assert_not_called()
        new_ssh.connect.assert_not_called()
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert destination.read_bytes() == b"download"


@pytest.mark.parametrize("failure", ["load", "policy", "auth", "open", "credentials"])
def test_sftp_failed_connect_closes_new_and_previous_sessions(monkeypatch, isolated_environment, failure):
    client = SftpClient()
    old_sftp, old_ssh = mock.Mock(), mock.Mock()
    client._sftp, client._ssh, client._connected = old_sftp, old_ssh, True
    ssh = ssh_mock()
    conn = connection()
    if failure == "load":
        isolated_environment.parent.mkdir()
        isolated_environment.write_text("bad pin file", encoding="utf-8")
        ssh.load_host_keys.side_effect = ValueError("bad pin")
    elif failure == "policy":
        ssh.set_missing_host_key_policy.side_effect = RuntimeError("policy failure")
    elif failure == "auth":
        ssh.connect.side_effect = paramiko.AuthenticationException("credentials must not be logged")
    elif failure == "open":
        ssh.open_sftp.side_effect = OSError("channel rejected")
    else:
        conn.password = ""
    monkeypatch.setattr("src.sftp_client.paramiko.SSHClient", lambda: ssh)
    with pytest.raises(SftpClientError):
        client.connect(conn)
    old_sftp.close.assert_called_once()
    old_ssh.close.assert_called_once()
    ssh.close.assert_called_once()
    assert not client.is_connected
    assert client._ssh is None and client._sftp is None
    client.disconnect()
    ssh.close.assert_called_once()


def test_sftp_disconnect_closes_ssh_even_if_channel_close_fails():
    client = SftpClient()
    client._sftp, client._ssh, client._connected = mock.Mock(), mock.Mock(), True
    client._sftp.close.side_effect = OSError("broken channel")
    ssh = client._ssh
    client.disconnect()
    client.disconnect()
    ssh.close.assert_called_once()
    assert client._sftp is None and client._ssh is None
    assert not client.is_connected


@pytest.fixture(scope="module")
def host_keys():
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024), paramiko.ECDSAKey.generate()


def install_handshake(monkeypatch, server_key):
    """Run actual SSHClient.connect key verification with a fake Transport."""
    real_ssh = paramiko.SSHClient
    transport = mock.Mock()
    transport.gss_kex_used = False
    transport.gss_host = "transfer.invalid"
    transport.get_remote_server_key.return_value = server_key
    transport.get_security_options.return_value = SimpleNamespace(key_types=["ssh-rsa", "ecdsa-sha2-nistp256"])
    channel = mock.Mock()

    class HandshakeClient(real_ssh):
        def connect(self, *args, **kwargs):
            return super().connect(*args, **kwargs, sock=object(),
                                   transport_factory=lambda *a, **k: transport)

        def _auth(self, *args, **kwargs):
            pass

        def open_sftp(self):
            return channel

    instances = []
    def factory():
        instance = HandshakeClient()
        instances.append(instance)
        return instance
    monkeypatch.setattr("src.sftp_client.paramiko.SSHClient", factory)
    return transport, channel, instances


def save_pin(path, target, key, hashed=True):
    pins = paramiko.HostKeys()
    # Fixed salt keeps this a deterministic hashed-host regression.
    name = paramiko.HostKeys.hash_host(target, "MDEyMzQ1Njc4OTAxMjM0NTY3ODk=") if hashed else target
    pins.add(name, key.get_name(), key)
    path.parent.mkdir(exist_ok=True)
    pins.save(str(path))


@pytest.mark.parametrize("port", [22, 2222])
@pytest.mark.parametrize("changed", [False, True, "algorithm"])
def test_real_paramiko_hashed_pin_never_falls_back_to_tofu(monkeypatch, isolated_environment, host_keys, port, changed):
    pinned, replacement, different_algorithm = host_keys
    conn = connection(port=port)
    target = conn.host if port == 22 else f"[{conn.host}]:{port}"
    save_pin(isolated_environment, target, pinned)
    presented = different_algorithm if changed == "algorithm" else replacement if changed else pinned
    transport, channel, instances = install_handshake(monkeypatch, presented)
    confirm = mock.Mock(return_value=True)
    client = SftpClient()
    before = isolated_environment.read_bytes()
    if changed:
        with pytest.raises(SftpClientError) as error:
            client.connect(conn, tofu_callback=confirm)
        assert isinstance(error.value.__cause__, paramiko.BadHostKeyException)
        assert not client.is_connected
        transport.close.assert_called_once()
    else:
        client.connect(conn, tofu_callback=confirm)
        assert client.is_connected
        assert isinstance(instances[0]._policy, paramiko.RejectPolicy)
        client.disconnect()
        channel.close.assert_called_once()
        transport.close.assert_called_once()
    confirm.assert_not_called()
    assert isolated_environment.read_bytes() == before


@pytest.mark.parametrize("accept", [False, True, None])
def test_tofu_uses_handshake_key_only_for_truly_unknown_host(monkeypatch, isolated_environment, host_keys, accept):
    pinned, presented, _ = host_keys
    # An existing known_hosts must be loaded even when this particular host is new.
    save_pin(isolated_environment, "other.invalid", pinned)
    transport, _, instances = install_handshake(monkeypatch, presented)
    confirm = mock.Mock(return_value=accept) if accept is not None else None
    client = SftpClient()
    before = isolated_environment.read_bytes()
    if accept:
        client.connect(connection(), tofu_callback=confirm)
        args = confirm.call_args.args
        assert args[:2] == ("transfer.invalid", 22)
        assert args[2].startswith("SHA256:")
        pins = paramiko.HostKeys(str(isolated_environment))
        assert pins.check("transfer.invalid", presented)
        assert instances[0].get_host_keys().check("other.invalid", pinned)
        client.disconnect()
    else:
        with pytest.raises(SftpClientError):
            client.connect(connection(), tofu_callback=confirm)
        assert isolated_environment.read_bytes() == before
        transport.close.assert_called_once()
        assert not client.is_connected


class DataSocket:
    def __init__(self, ftp, mode):
        self.ftp = ftp
        self.mode = mode
        self.blocks = deque([b"new contents", b""])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # A server may send both 426 and 226 after a callback aborts. ftplib
        # does not consume them when the callback unwinds its transfer loop.
        self.ftp.pending.extend(["426 Transfer aborted", "226 Transfer complete"] if exc[0] else ["226 Transfer complete"])

    def recv(self, size):
        if self.ftp.fail_data:
            raise OSError("data socket failed")
        return self.blocks.popleft()

    def sendall(self, block):
        if self.ftp.fail_data:
            raise OSError("data socket failed")


class ProtocolFTP(ftplib.FTP):
    """Real retrbinary/storbinary loops over an instrumented fake FTP wire."""
    def __init__(self, fail_data=False):
        super().__init__()
        self.pending = deque()
        self.commands = []
        self.closed = False
        self.fail_data = fail_data

    def voidcmd(self, command):
        assert not self.closed, "closed control channel reused"
        self.commands.append(command)
        if self.pending:
            response = self.pending.popleft()
            if response[0] != "2":
                raise ftplib.error_temp(response)
            return response
        return "200 OK"

    def transfercmd(self, command, rest=None):
        assert not self.closed
        self.commands.append(command)
        return DataSocket(self, command)

    def voidresp(self):
        return self.pending.popleft()

    def size(self, path):
        return 12

    def mkd(self, path):
        assert not self.pending, "stale transfer replies polluted the next operation"
        self.commands.append("MKD " + path)

    def close(self):
        self.closed = True


def ftp_client(wire):
    client = FtpClient()
    client._ftp = wire
    client._conn = connection("ftp", 21)
    client._connected = True
    return client


@pytest.mark.parametrize("operation", ["mkdir", "disconnect", "connect"])
def test_ftp_lifecycle_cannot_interrupt_active_transfer(tmp_path, monkeypatch, operation):
    wire, fresh = ProtocolFTP(), ProtocolFTP()
    client = ftp_client(wire)
    client._lock = ObservedRLock()
    monkeypatch.setattr(client, "_open_session", mock.Mock(return_value=fresh))
    entered, release = threading.Event(), threading.Event()
    errors = []
    destination = tmp_path / "download"

    def progress(done, total):
        entered.set()
        assert release.wait(5)
    actions = {
        "mkdir": lambda: client.make_directory("/after"),
        "disconnect": client.disconnect,
        "connect": lambda: client.connect(connection("ftp", 21)),
    }
    first = run_thread("transfer", lambda: client.download("/file", str(destination), progress), errors)
    assert entered.wait(5)
    second = run_thread("contender", actions[operation], errors)
    try:
        assert client._lock.contending.wait(5)
        assert not wire.closed
        assert wire.commands == ["NOOP", "TYPE I", "RETR /file"]
        client._open_session.assert_not_called()
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert destination.read_bytes() == b"new contents"
    if operation in ("disconnect", "connect"):
        assert wire.closed
    else:
        assert not wire.closed and wire.commands[-1] == "MKD /after"


@pytest.mark.parametrize("direction", ["download", "upload"])
@pytest.mark.parametrize("failure", ["cancel", "callback", "wire"])
def test_ftp_aborted_data_loop_discards_stale_replies_and_reconnects(tmp_path, monkeypatch, direction, failure):
    wire = ProtocolFTP(fail_data=failure == "wire")
    fresh = ProtocolFTP()
    client = ftp_client(wire)
    saved_conn = client._conn
    open_session = mock.Mock(return_value=fresh)
    monkeypatch.setattr(client, "_open_session", open_session)
    local = tmp_path / "local"
    local.write_bytes(b"original")

    def callback(done, total):
        if failure == "cancel":
            raise InterruptedError("cancelled")
        raise ValueError("callback failed")
    with pytest.raises(FtpClientError):
        if direction == "download":
            client.download("/file", str(local), callback)
        else:
            client.upload(str(local), "/file", callback)
    assert wire.closed
    assert list(wire.pending) == ["426 Transfer aborted", "226 Transfer complete"]
    assert client._ftp is None
    assert client.is_connected
    assert client._conn is saved_conn
    assert local.read_bytes() == b"original"
    assert not list(tmp_path.glob(".neossh-download-*.part"))
    commands = list(wire.commands)
    client.make_directory("/after-abort")
    open_session.assert_called_once_with(saved_conn)
    assert fresh.commands == ["MKD /after-abort"]
    assert wire.commands == commands  # no NOOP/ABOR/QUIT on the stale wire


@pytest.mark.parametrize("direction", ["download", "upload"])
def test_ftp_success_consumes_final_reply_and_keeps_channel(tmp_path, direction):
    wire = ProtocolFTP()
    client = ftp_client(wire)
    local = tmp_path / "local"
    local.write_bytes(b"original")
    progress = []
    getattr(client, direction)(*( ("/file", str(local)) if direction == "download" else (str(local), "/file") ),
                               progress_callback=lambda *args: progress.append(args))
    assert not wire.closed and not wire.pending
    assert client._ftp is wire
    assert progress
    if direction == "download":
        assert local.read_bytes() == b"new contents"
    assert not list(tmp_path.glob(".neossh-download-*.part"))


def test_ftp_reconnect_failure_preserves_saved_logical_session(monkeypatch):
    stale, fresh = ProtocolFTP(), ProtocolFTP()
    stale.voidcmd = mock.Mock(side_effect=EOFError("idle timeout"))
    client = ftp_client(stale)
    saved_conn = client._conn
    reopen = mock.Mock(side_effect=[FtpClientError("temporary outage"), fresh])
    monkeypatch.setattr(client, "_open_session", reopen)
    with pytest.raises(FtpClientError, match="temporary outage"):
        client.make_directory("/first")
    assert stale.closed and client._ftp is None
    assert client.is_connected and client._conn is saved_conn
    client.make_directory("/second")
    assert reopen.call_args_list == [mock.call(saved_conn), mock.call(saved_conn)]
    assert fresh.commands == ["MKD /second"]


@pytest.mark.parametrize("invalidated", [False, True])
def test_ftp_explicit_disconnect_never_reconnects(monkeypatch, invalidated):
    wire = ProtocolFTP()
    client = ftp_client(wire)
    saved_conn = client._conn
    if invalidated:
        client._invalidate_session()
    reopen = mock.Mock()
    monkeypatch.setattr(client, "_open_session", reopen)
    client.disconnect()
    client.disconnect()
    with pytest.raises(FtpClientError, match="Not connected"):
        client.make_directory("/must-not-reconnect")
    assert wire.closed
    assert not client.is_connected
    assert client._conn is saved_conn
    reopen.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_ftp_repeated_connect_closes_previous_session(monkeypatch, failure):
    old, fresh = ProtocolFTP(), ProtocolFTP()
    client = ftp_client(old)
    conn = connection("ftp", 2121)
    opener = mock.Mock(side_effect=FtpClientError("login failed")) if failure else mock.Mock(return_value=fresh)
    monkeypatch.setattr(client, "_open_session", opener)
    if failure:
        with pytest.raises(FtpClientError):
            client.connect(conn)
        assert not client.is_connected and client._ftp is None
    else:
        client.connect(conn)
        assert client.is_connected and client._ftp is fresh
    assert old.closed
    assert client._conn is conn


def test_ftp_session_setup_failure_closes_control_channel(monkeypatch):
    wire = mock.Mock()
    wire.login.side_effect = ftplib.error_perm("530 Login incorrect")
    monkeypatch.setattr("src.ftp_client.ftplib.FTP", lambda: wire)
    client = FtpClient()
    with pytest.raises(FtpClientError, match="Authentication failed"):
        client.connect(connection("ftp", 21))
    wire.close.assert_called_once()
    assert client._ftp is None and not client.is_connected


@pytest.mark.parametrize("protocol", ["sftp", "ftp"])
def test_worker_cancellation_keeps_destination_and_removes_partial(tmp_path, protocol):
    destination = tmp_path / "saved"
    destination.write_bytes(b"original")
    if protocol == "ftp":
        wire = ProtocolFTP()
        client = ftp_client(wire)
    else:
        wire = mock.Mock()
        client = SftpClient()
        client._sftp, client._connected = wire, True
        def get(remote, local, callback):
            Path(local).write_bytes(b"partial")
            callback(7, 100)
        wire.get.side_effect = get
    worker = SftpDownloadWorker(client, "/file", str(destination))
    cancelled, finished, errors = [], [], []
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)
    # Request cancellation inside the callback, as if a GUI action arrived
    # during IO, without requiring a QApplication or event-loop timing.
    on_progress = worker._on_progress
    def cancel_on_progress(done, total):
        worker.cancel()
        on_progress(done, total)
    worker._on_progress = cancel_on_progress
    worker.run()
    assert cancelled == [True] and not finished and not errors
    assert destination.read_bytes() == b"original"
    assert not list(tmp_path.glob(".neossh-download-*.part"))
    if protocol == "ftp":
        assert wire.closed and client._ftp is None


def test_worker_cancel_before_start_never_enters_client():
    client = mock.Mock()
    worker = SftpDownloadWorker(client, "/file", "unused")
    cancelled = []
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.cancel()
    worker.run()
    assert cancelled == [True]
    client.download.assert_not_called()


def test_worker_late_cancel_does_not_relabel_committed_download(tmp_path):
    client = mock.Mock()
    destination = str(tmp_path / "committed")
    worker = SftpDownloadWorker(client, "/file", destination)
    def committed(*args, **kwargs):
        Path(destination).write_bytes(b"committed")
        worker.cancel()
    client.download.side_effect = committed
    finished, cancelled = [], []
    worker.finished.connect(finished.append)
    worker.cancelled.connect(lambda: cancelled.append(True))
    worker.run()
    assert finished == [destination] and not cancelled


def test_worker_cancel_does_not_wait_for_transfer_lock(tmp_path):
    wire = ProtocolFTP()
    client = ftp_client(wire)
    entered, release = threading.Event(), threading.Event()
    original_transfercmd = wire.transfercmd
    def transfercmd(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_transfercmd(*args, **kwargs)
    wire.transfercmd = transfercmd
    destination = tmp_path / "local"
    worker = SftpDownloadWorker(client, "/file", str(destination))
    cancelled = []
    worker.cancelled.connect(lambda: cancelled.append(True), Qt.ConnectionType.DirectConnection)
    worker.start()
    try:
        assert entered.wait(5)
        # This call is made from the GUI side while the worker holds _lock.
        worker.cancel()
        assert worker._cancel_requested.is_set()
    finally:
        release.set()
        assert worker.wait(5000)
    assert cancelled == [True]
    assert not destination.exists()
    assert wire.closed
