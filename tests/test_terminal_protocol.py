"""Offline terminal regressions: no listeners, real SSH or user config access."""

import asyncio
import base64
import ctypes
import hashlib
import io
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import paramiko
import pytest

from src import ssh_launcher, sshfs_controller
from src.config import Connection
from src.terminal import bridge_server
from src.terminal.bridge_server import TerminalBridgeServer, TerminalSession, _PendingToken


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("NEOSSH_EXEC_TIMEOUT", raising=False)


class WindowLimitedExecChannel:
    """stdout cannot finish until stderr's >2 MiB SSH window is drained."""

    def __init__(self, size=4 * 1024 * 1024):
        self.remaining = size
        self.stdout = b"done\n"
        self.closed = False
        self.eof_received = False
        self.read_sizes = []

    def recv_ready(self):
        return self.remaining == 0 and bool(self.stdout)

    def recv_stderr_ready(self):
        return self.remaining > 0

    def recv(self, size):
        assert self.recv_ready()
        self.read_sizes.append(size)
        data, self.stdout = self.stdout, b""
        self.eof_received = self.remaining == 0
        return data

    def recv_stderr(self, size):
        self.read_sizes.append(size)
        count = min(self.remaining, size)
        self.remaining -= count
        self.eof_received = self.remaining == 0 and not self.stdout
        return b"E" * count

    def exit_status_ready(self):
        return self.remaining == 0 and not self.stdout

    def recv_exit_status(self):
        assert self.exit_status_ready(), "status awaited before draining both streams"
        return 37

    def close(self):
        self.closed = True


def test_exec_streams_4mb_stderr_and_preserves_exit_output_and_log_cap(monkeypatch):
    channel = WindowLimitedExecChannel()
    client = Mock()
    client.exec_command.return_value = (
        Mock(), SimpleNamespace(channel=channel), Mock(read=Mock(side_effect=AssertionError("unbounded read")))
    )
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    # Never open the test runner's real Windows console or SSH files.
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=Mock()), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace())
    out, err = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))
    monkeypatch.setattr(sys, "stderr", SimpleNamespace(buffer=err))
    log = Mock()
    monkeypatch.setattr(ssh_launcher, "_send_cli_log", log)

    rc = ssh_launcher.launch_ssh_in_current_terminal(
        {"host": "example.test", "user": "alice", "password": "fake"},
        exec_command="produce-stderr", cli_key="test-only",
    )

    assert rc == 37
    assert out.getvalue() == b"done\n"
    assert err.getvalue().endswith(b"E" * (4 * 1024 * 1024))
    assert max(channel.read_sizes) <= 64 * 1024
    client.close.assert_called_once()
    entry = log.call_args.args[1]
    assert entry["exit_code"] == 37
    assert entry["truncated"] is True
    assert entry["output"].startswith("done\n")
    assert len(entry["output"].encode()) <= ssh_launcher._CLI_LOG_MAX_BYTES + 32


def test_exec_drains_stdout_and_stderr_fairly():
    class BothReady(WindowLimitedExecChannel):
        def recv_ready(self):
            return bool(self.stdout)

    channel = BothReady()
    writes = []
    assert ssh_launcher._drain_exec_channel(
        channel, lambda data: writes.append(("out", data)),
        lambda data: writes.append(("err", len(data))),
    ) == 37
    assert writes[0] == ("out", b"done\n")
    assert writes[1] == ("err", 64 * 1024)


@pytest.mark.parametrize("output_eof", [False, True])
def test_exec_optional_timeout_is_overall_deadline(monkeypatch, output_eof):
    channel = Mock(closed=False, eof_received=output_eof)
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = False
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with pytest.raises(TimeoutError):
        ssh_launcher._drain_exec_channel(channel, Mock(), Mock(), timeout=1.0)
    channel.recv_exit_status.assert_not_called()


class FakeChannel:
    def __init__(self, output=(), eof=False):
        self.output = deque(output)
        self.closed = False
        self.eof_received = eof
        self.sent = []
        self.resizes = []

    def recv_ready(self):
        return bool(self.output)

    def recv_stderr_ready(self):
        return False

    def recv(self, size):
        return self.output.popleft()

    def recv_stderr(self, size):
        raise AssertionError("stderr is never ready in this fixture")

    def exit_status_ready(self):
        return self.eof_received

    def sendall(self, data):
        self.sent.append(data)

    def resize_pty(self, width, height):
        self.resizes.append((width, height))

    def setblocking(self, flag):
        pass

    def close(self):
        self.closed = True


class FakeWebSocket:
    def __init__(self, messages=(), wait_for_close=False):
        self.messages = deque(messages)
        self.wait_for_close = wait_for_close
        self.closed = asyncio.Event()
        self.close_calls = []
        self.output = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.messages:
            return self.messages.popleft()
        if self.wait_for_close:
            await self.closed.wait()
        raise StopAsyncIteration

    async def send(self, data):
        self.output.append(data)

    async def close(self, code=1000, reason=""):
        self.close_calls.append((code, reason))
        self.closed.set()


def install_session(bridge, channel=None):
    session = TerminalSession("conn", Mock(), channel or FakeChannel())
    bridge._sessions["conn"] = session
    bridge._pending_tokens["token"] = _PendingToken("conn", session=session)
    return session


def test_binary_json_input_is_preserved_and_only_explicit_text_resize_is_control():
    async def run():
        bridge = TerminalBridgeServer()
        session = install_session(bridge)
        pasted = b'{"type":"resize","cols":123,"rows":45}'
        unknown = '{"type":"my-command","payload":"hello"}'
        malformed = '{"type":"resize","cols":"oops","rows":24}'
        extra = '{"type":"resize","cols":80,"rows":24,"shell":"echo"}'
        unicode_input = "你好\r".encode()
        ws = FakeWebSocket([
            pasted, unknown, malformed, extra, unicode_input,
            '{"type":"resize","cols":100,"rows":30}',
        ])
        await bridge._ws_handler(ws, "/ws/token")
        assert session.channel.sent == [pasted, unknown.encode(), malformed.encode(), extra.encode(), unicode_input]
        assert session.channel.resizes == [(100, 30)]
        assert session.channel.closed
        assert session.client.close.called
        assert ws.closed.is_set()
        assert not bridge._sessions
        assert session.forwarder_task is None

    asyncio.run(run())


def test_frontend_encodes_terminal_input_as_binary():
    html = (Path(__file__).parents[1] / "assets" / "terminal" / "index.html").read_text(encoding="utf-8")
    assert "new TextEncoder()" in html
    assert "ws.send(inputEncoder.encode(data))" in html
    assert "ws.send(data)" not in html


@pytest.mark.parametrize("receive_error", [False, True])
def test_final_output_then_eof_or_error_closes_websocket_and_releases_session(receive_error):
    async def run():
        bridge = TerminalBridgeServer()
        channel = FakeChannel([b"last output\r\n"], eof=True)
        session = install_session(bridge, channel)
        if receive_error:
            original = bridge._recv_channel

            def recv(ch):
                if ch.output:
                    return original(ch)
                raise OSError("remote disconnected")

            bridge._recv_channel = recv
        ws = FakeWebSocket(wait_for_close=True)
        await asyncio.wait_for(bridge._ws_handler(ws, "/ws/token"), timeout=2)
        assert ws.output == [b"last output\r\n"]
        assert ws.closed.is_set()
        assert channel.closed
        assert session.client.close.called
        assert not bridge.is_session_alive("conn")
        assert not bridge._sessions
        assert not bridge._pending_tokens

    asyncio.run(run())


def test_recv_channel_drains_output_even_when_channel_is_already_closed():
    channel = FakeChannel([b"last packet"], eof=True)
    channel.closed = True
    assert TerminalBridgeServer._recv_channel(channel) == b"last packet"
    assert TerminalBridgeServer._recv_channel(channel) is None


@pytest.mark.parametrize("would_block", [None, TimeoutError, BlockingIOError])
def test_recv_channel_rechecks_stderr_after_select_with_real_paramiko_channel(monkeypatch, would_block):
    import select

    channel = paramiko.Channel(1)
    channel.setblocking(False)
    select_calls = []
    original_receive = channel.recv_stderr
    failures = [would_block] if would_block else []

    def receive(size):
        if failures:
            raise failures.pop()("temporary would-block")
        return original_receive(size)

    def select_stderr(readers, writers, errors, timeout):
        select_calls.append(timeout)
        if len(select_calls) == 1:
            # This arrives AFTER recv_ready/recv_stderr_ready returned False.
            channel.in_stderr_buffer.feed(b"late diagnostic")
        assert readers == [channel]
        return readers, [], []

    monkeypatch.setattr(select, "select", select_stderr)
    monkeypatch.setattr(channel, "recv_stderr", receive)
    try:
        assert TerminalBridgeServer._recv_channel(channel) == b"late diagnostic"
        assert select_calls == [0.2] * (2 if would_block else 1)
        assert not channel.closed
    finally:
        channel.close()


def test_recv_channel_waits_for_tail_after_exit_status_with_real_paramiko_channel(monkeypatch):
    import select

    channel = paramiko.Channel(1)
    channel.setblocking(False)
    channel.exit_status = 37
    channel.status_event.set()
    select_calls = []

    def deliver_tail(readers, writers, errors, timeout):
        select_calls.append(timeout)
        channel.in_buffer.feed(b"late stdout")
        channel.in_stderr_buffer.feed(b"late stderr")
        channel.eof_received = True
        channel.in_buffer.close()
        channel.in_stderr_buffer.close()
        return readers, [], []

    monkeypatch.setattr(select, "select", deliver_tail)
    try:
        assert channel.exit_status_ready()
        assert TerminalBridgeServer._recv_channel(channel) == b"late stdout"
        assert TerminalBridgeServer._recv_channel(channel) == b"late stderr"
        assert TerminalBridgeServer._recv_channel(channel) is None
        assert select_calls == [0.2]
    finally:
        channel.close()


def test_exec_waits_for_tail_after_exit_status_with_real_paramiko_channel(monkeypatch):
    channel = paramiko.Channel(1)
    channel.setblocking(False)
    channel.exit_status = 37
    channel.status_event.set()
    output, errors, sleeps = [], [], []

    def deliver_tail(delay):
        sleeps.append(delay)
        channel.in_buffer.feed(b"late stdout")
        channel.in_stderr_buffer.feed(b"late stderr")
        channel.eof_received = True
        channel.in_buffer.close()
        channel.in_stderr_buffer.close()

    monkeypatch.setattr(time, "sleep", deliver_tail)
    try:
        assert ssh_launcher._drain_exec_channel(channel, output.append, errors.append, timeout=1) == 37
        assert output == [b"late stdout"]
        assert errors == [b"late stderr"]
        assert sleeps == [0.01]
    finally:
        channel.close()


@pytest.mark.parametrize("receiver", ["bridge", "exec"])
def test_final_packet_arriving_between_ready_and_eof_checks_is_drained(monkeypatch, receiver):
    channel = paramiko.Channel(1)
    channel.setblocking(False)
    channel.exit_status = 37
    channel.status_event.set()
    readiness_checks = []
    inject_at = 1 if receiver == "bridge" else 2

    def stderr_ready():
        readiness_checks.append(None)
        if len(readiness_checks) == inject_at:
            # Both ready snapshots said empty, but the transport queues the
            # last stdout packet and EOF before the receiver reads EOF state.
            channel.in_buffer.feed(b"final race packet")
            channel.eof_received = True
            channel.in_buffer.close()
            channel.in_stderr_buffer.close()
        return False

    monkeypatch.setattr(channel, "recv_stderr_ready", stderr_ready)
    try:
        if receiver == "bridge":
            assert TerminalBridgeServer._recv_channel(channel) == b"final race packet"
            assert TerminalBridgeServer._recv_channel(channel) is None
        else:
            output = []
            assert ssh_launcher._drain_exec_channel(channel, output.append, Mock(), timeout=1) == 37
            assert output == [b"final race packet"]
    finally:
        channel.close()


@pytest.mark.parametrize("on_handshake", [False, True])
def test_expired_tokens_close_associated_ssh_session(on_handshake):
    async def run():
        bridge = TerminalBridgeServer()
        session = install_session(bridge)
        bridge._pending_tokens["token"].created_at -= 100
        if on_handshake:
            ws = FakeWebSocket()
            await bridge._ws_handler(ws, "/ws/token")
            assert ws.close_calls[-1][0] == 1008
        else:
            await bridge._expire_pending_tokens()
        assert session.channel.closed
        assert session.client.close.called
        assert not bridge._sessions
        assert not bridge._pending_tokens

    asyncio.run(run())


def test_old_generation_cleanup_does_not_close_replacement():
    async def run():
        bridge = TerminalBridgeServer()
        old = install_session(bridge)
        new = TerminalSession("conn", Mock(), FakeChannel())
        bridge._sessions["conn"] = new
        bridge._pending_tokens["new-token"] = _PendingToken("conn", session=new)
        await bridge._close_session_async("conn", old)
        assert old.channel.closed
        assert not new.channel.closed
        assert bridge._sessions["conn"] is new
        assert set(bridge._pending_tokens) == {"new-token"}
        await bridge._close_session_async("conn")

    asyncio.run(run())


@pytest.fixture
def fake_ssh_handshake(monkeypatch):
    """Real SSHClient.connect key validation with an in-memory transport/socket."""
    client_class = paramiko.SSHClient
    clients = []
    state = SimpleNamespace(remote_key=None, channel=FakeChannel(), on_connect=None)
    transport = Mock(gss_kex_used=False, gss_host="example.test")
    transport.get_security_options.return_value = SimpleNamespace(key_types=["ssh-rsa"])
    transport.get_remote_server_key.side_effect = lambda: state.remote_key

    class OfflineClient(client_class):
        def __init__(self):
            super().__init__()
            clients.append(self)

        def connect(self, *args, **kwargs):
            if state.on_connect:
                state.on_connect()
            return super().connect(*args, **kwargs, sock=object(),
                                   transport_factory=lambda *a, **k: transport)

        def _auth(self, *args, **kwargs):
            pass

        def invoke_shell(self, **kwargs):
            return state.channel

        def _families_and_addresses(self, *args, **kwargs):
            raise AssertionError("Real network lookup attempted")

    monkeypatch.setattr(paramiko, "SSHClient", OfflineClient)
    state.clients = clients
    state.transport = transport
    return state


@pytest.mark.parametrize("port", [22, 2222])
def test_hashed_known_host_changed_key_rejected_without_openssh(
    monkeypatch, tmp_path, fake_ssh_handshake, port
):
    known_key, changed_key = paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)
    hostname = "example.test" if port == 22 else "[example.test]:2222"
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"{paramiko.HostKeys.hash_host(hostname)} {known_key.get_name()} {known_key.get_base64()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_server.os.path, "expanduser", lambda path: str(known_hosts))
    # Loading/verifying pins must not depend on these external tools.
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("No OpenSSH probes")))
    bridge = TerminalBridgeServer()
    confirm = Mock(return_value=True)
    bridge.host_key_verify_callback = confirm
    fake_ssh_handshake.remote_key = changed_key
    conn = Connection("test", "example.test", "alice", port=port, password="fake")

    assert bridge.create_session_token("conn", conn) is None
    confirm.assert_not_called()
    assert fake_ssh_handshake.clients[0].get_host_keys().lookup(hostname)["ssh-rsa"] == known_key
    assert fake_ssh_handshake.transport.close.called
    assert not bridge._sessions
    assert changed_key.get_base64() not in known_hosts.read_text(encoding="utf-8")


def test_hashed_matching_pin_connects_without_tofu_callback(monkeypatch, tmp_path, fake_ssh_handshake):
    key = paramiko.RSAKey.generate(1024)
    fake_ssh_handshake.remote_key = key
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        f"{paramiko.HostKeys.hash_host('example.test')} ssh-rsa {key.get_base64()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_server.os.path, "expanduser", lambda path: str(known_hosts))
    bridge = TerminalBridgeServer()
    assert bridge.create_session_token("conn", Connection("test", "example.test", "alice", password="fake"))
    asyncio.run(bridge._close_session_async("conn"))


def test_unknown_host_without_callback_is_rejected(monkeypatch, tmp_path, fake_ssh_handshake):
    fake_ssh_handshake.remote_key = paramiko.RSAKey.generate(1024)
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(bridge_server.os.path, "expanduser", lambda path: str(known_hosts))
    bridge = TerminalBridgeServer()
    assert bridge.create_session_token("conn", Connection("test", "example.test", "alice", password="fake")) is None
    assert fake_ssh_handshake.transport.close.called
    assert not known_hosts.exists()
    assert not bridge._sessions


def test_tofu_confirms_and_persists_exact_handshake_key(monkeypatch, tmp_path, fake_ssh_handshake):
    key = paramiko.RSAKey.generate(1024)
    fake_ssh_handshake.remote_key = key
    known_hosts = tmp_path / ".ssh" / "known_hosts"
    monkeypatch.setattr(bridge_server.os.path, "expanduser", lambda path: str(known_hosts))
    bridge = TerminalBridgeServer()
    confirm = Mock(return_value=True)
    bridge.host_key_verify_callback = confirm
    token = bridge.create_session_token("conn", Connection("test", "example.test", "alice", password="fake"))
    assert token is not None
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    confirm.assert_called_once_with("example.test", 22, fingerprint)
    assert paramiko.HostKeys(str(known_hosts)).lookup("example.test")["ssh-rsa"] == key
    asyncio.run(bridge._close_session_async("conn"))


def test_late_ssh_completion_after_stop_is_closed(monkeypatch, tmp_path, fake_ssh_handshake):
    bridge = TerminalBridgeServer()
    known_hosts = tmp_path / "known_hosts"
    monkeypatch.setattr(bridge_server.os.path, "expanduser", lambda path: str(known_hosts))
    bridge.host_key_verify_callback = lambda *args: True
    fake_ssh_handshake.remote_key = paramiko.RSAKey.generate(1024)
    fake_ssh_handshake.on_connect = bridge.stop
    assert bridge.create_session_token("conn", Connection("test", "example.test", "alice", password="fake")) is None
    assert fake_ssh_handshake.channel.closed
    assert fake_ssh_handshake.transport.close.called
    assert not bridge._sessions
    assert not bridge._pending_tokens


def test_bridge_stop_is_nonblocking_and_awaits_mock_server_tasks_executor(monkeypatch):
    """Exercise the actual loop/thread lifecycle without binding any server."""
    bridge = TerminalBridgeServer()
    events = []
    executor_entered = threading.Event()
    executor_release = threading.Event()

    class Server:
        def close(self):
            events.append("server.close")

        async def wait_closed(self):
            await asyncio.sleep(0)
            events.append("server.wait_closed")

    async def cleanup():
        try:
            await asyncio.Future()
        finally:
            events.append("cleanup.cancelled")

    def blocking_recv(channel):
        executor_entered.set()
        assert executor_release.wait(2)
        events.append("executor.done")
        return None

    async def fake_serve(ready):
        bridge._server = Server()
        bridge._port = 12345  # Just state, no listening socket.
        bridge._cleanup_task = asyncio.create_task(cleanup())
        session = install_session(bridge)
        session.channel.close = executor_release.set
        session.forwarder_task = asyncio.create_task(bridge._ssh_to_ws(session))
        ready.set()

    monkeypatch.setattr(bridge, "_serve", fake_serve)
    monkeypatch.setattr(bridge, "_recv_channel", blocking_recv)
    assert bridge.start()
    try:
        assert executor_entered.wait(2)
        started = time.monotonic()
        bridge.stop()
        assert time.monotonic() - started < 0.5
        assert bridge.wait_stopped(timeout=3)
        assert bridge._loop.is_closed()
        assert bridge._shutdown_complete
        assert set(events) == {"server.close", "server.wait_closed", "cleanup.cancelled", "executor.done"}
        assert not bridge._sessions
        assert not bridge._pending_tokens
        assert bridge.stop(wait=True, timeout=0)
    finally:
        executor_release.set()
        bridge.stop(wait=True, timeout=3)


def test_bridge_start_failure_closes_loop_and_stop_remains_idempotent(monkeypatch):
    bridge = TerminalBridgeServer()

    async def broken_serve(ready):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(bridge, "_serve", broken_serve)
    assert bridge.start() is False
    assert bridge.wait_stopped(timeout=2)
    assert bridge._loop.is_closed()
    assert bridge.stop(wait=False) is True
    assert bridge.stop(wait=True, timeout=0) is True


def test_stop_wait_is_bounded_and_timeout_zero_does_not_join_live_worker():
    bridge = TerminalBridgeServer()
    release = threading.Event()
    bridge._stopped.clear()
    bridge._thread = threading.Thread(target=lambda: release.wait(2), daemon=True)
    bridge._thread.start()
    try:
        assert bridge.stop(wait=True, timeout=0) is False
    finally:
        release.set()
        bridge._thread.join(timeout=2)


def test_sshfs_key_path_with_spaces_reaches_ssh_config_parser(monkeypatch, tmp_path):
    key = tmp_path / "private keys" / "id test"
    key.parent.mkdir()
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    conn = Connection("test", "example.test", "alice", auth_method="key", key_path=str(key), drive_letter="X:")
    proc = Mock(pid=42)
    proc.poll.return_value = None
    popen = Mock(return_value=proc)
    drive_states = iter([False, False, True, True, True])
    monkeypatch.setattr(sshfs_controller, "_find_sshfs_exe", lambda: "sshfs.exe")
    monkeypatch.setattr(sshfs_controller, "_drive_letter_in_use", lambda letter: next(drive_states))
    monkeypatch.setattr(sshfs_controller.subprocess, "Popen", popen)
    monkeypatch.setattr(sshfs_controller.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(sshfs_controller.time, "sleep", lambda seconds: None)
    assert sshfs_controller.SSHFSController()._mount_direct(conn).success
    args = popen.call_args.args[0]
    identity = next(arg[2:] for arg in args if arg.startswith("-oIdentityFile="))
    known_hosts = next(arg[2:] for arg in args if arg.startswith("-oUserKnownHostsFile="))
    assert identity == 'IdentityFile="' + str(key).replace("\\", "/") + '"'
    assert known_hosts.startswith('UserKnownHostsFile="')


def test_literal_path_quotes_are_accepted_by_local_ssh_G(tmp_path):
    ssh = shutil.which("ssh")
    if ssh is None:
        pytest.skip("Local OpenSSH unavailable; quote construction tested separately")
    # -G only prints config; -F supplies an empty test-owned file, and canonical
    # lookup is disabled. Never reads the user's config or opens a connection.
    config = tmp_path / "empty_config"
    config.write_text("", encoding="utf-8")
    path = str(tmp_path / "private keys" / "id test").replace("\\", "/")
    quoted = sshfs_controller._ssh_config_path(path)
    result = subprocess.run(
        [ssh, "-G", "-F", str(config), "-oCanonicalizeHostname=no", "-oIdentityFile=" + quoted,
         "-oUserKnownHostsFile=" + quoted, "example.test"],
        capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    output = result.stdout.decode(errors="replace")
    assert f"identityfile {path}" in output
    assert f"userknownhostsfile {path}" in output or f'userknownhostsfile "{path}"' in output
