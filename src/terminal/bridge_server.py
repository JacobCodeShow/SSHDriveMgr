"""Local WebSocket ↔ Paramiko bridge for the integrated terminal.

Binary WebSocket frames are raw terminal input (UTF-8 from xterm.js). Text
frames support only explicit resize controls; unrecognized text remains input.
Host keys are verified by Paramiko, including hashed known_hosts entries, and
TOFU asks about the exact key negotiated by the connection, never a probe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import paramiko

_MAX_WS_MSG_BYTES = 256 * 1024
_TOKEN_EXPIRY_SEC = 60.0


@dataclass
class TerminalSession:
    conn_id: str
    client: "paramiko.SSHClient"
    channel: "paramiko.Channel"
    websocket: object | None = None
    forwarder_task: asyncio.Task | None = None


@dataclass
class _PendingToken:
    conn_id: str
    created_at: float = field(default_factory=time.monotonic)
    session: TerminalSession | None = None


class TerminalBridgeServer:
    """An asyncio WebSocket server in a dedicated daemon thread.

    start() starts the local listener. create_session_token() performs blocking
    SSH setup and belongs on a worker, not the Qt thread. close_session() and
    stop() only schedule cleanup and are safe for GUI shutdown. stop() defaults
    to nonblocking; stop(wait=True, timeout=2.0) or wait_stopped(timeout=2.0)
    allows a worker to wait a bounded time. Their bool result means ALL server,
    task, executor and event-loop resources have finished closing, not merely
    that shutdown was requested. Never call an unbounded join from Qt.

    SSH setup already in progress cannot be forcibly cancelled by stop(); its
    late result is closed rather than installed. The owner must manage its
    connection worker's lifetime separately.
    """

    def __init__(self):
        self._sessions: dict[str, TerminalSession] = {}
        self._pending_tokens: dict[str, _PendingToken] = {}
        self._state_lock = threading.RLock()
        self._port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._cleanup_task: asyncio.Task | None = None
        self._stopping = False
        self._shutdown_complete = False
        self._stopped = threading.Event()
        self._stopped.set()
        self.host_key_verify_callback: object | None = None

    def start(self) -> bool:
        """Start once; return False on import, bind or startup failure."""
        with self._state_lock:
            if self._thread is not None or self._stopping:
                return False
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._stopped.clear()
        ready = threading.Event()
        start_error = []

        def _run():
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._serve(ready))
                loop.run_forever()
            except Exception as exc:
                start_error.append(exc)
                logger.exception("Terminal bridge failed")
            finally:
                # Prevent a concurrent stop() from scheduling a second shutdown
                # while startup failure cleanup is already taking ownership.
                with self._state_lock:
                    self._stopping = True
                ready.set()
                try:
                    loop.run_until_complete(self._shutdown())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    # Closing channels makes select/recv workers exit. Await them
                    # before closing the loop, rather than abandoning executor threads.
                    loop.run_until_complete(loop.shutdown_default_executor())
                finally:
                    loop.close()
                    self._port = 0
                    self._stopped.set()

        self._thread = threading.Thread(target=_run, daemon=True, name="TerminalBridge")
        self._thread.start()
        ready.wait(timeout=10)
        if start_error or not self._port:
            logger.error("Terminal bridge failed to start: %s", start_error or "timed out")
            self.stop()
            return False
        return True

    def stop(self, wait: bool = False, timeout: float = 2.0) -> bool:
        """Request complete cleanup without blocking by default.

        With wait=True, wait at most timeout seconds; False means cleanup is
        still running. Repeated calls are safe. No SSH/network work runs on the
        calling thread. See wait_stopped() for a later bounded completion wait.
        """
        with self._state_lock:
            first_request = not self._stopping
            self._stopping = True
            loop = self._loop
        if first_request and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._shutdown_and_stop()))
            except RuntimeError:
                pass  # The worker already completed its finally cleanup.
        if wait:
            return self.wait_stopped(timeout)
        thread = self._thread
        return self._stopped.is_set() and (thread is None or not thread.is_alive())

    def wait_stopped(self, timeout: float = 2.0) -> bool:
        """Wait at most timeout seconds for shutdown; never joins itself."""
        thread = self._thread
        if thread is threading.current_thread():
            return False
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
            return not thread.is_alive() and self._stopped.is_set()
        return self._stopped.is_set()

    @property
    def port(self) -> int:
        return self._port

    def create_session_token(self, conn_id: str, conn) -> str | None:
        """Blocking SSH setup; return a single-use token or None.

        Safe to run on a worker. If shutdown wins the race, the newly connected
        client/channel is closed here and no usable token is returned.
        """
        import paramiko

        with self._state_lock:
            if self._stopping:
                return None
        host = conn.host or ""
        port = int(conn.port or 22)
        user = getattr(conn, "user", "") or getattr(conn, "ssh_user", "") or ""
        auth_method = getattr(conn, "auth_method", "password")
        password = getattr(conn, "password", "") or ""
        key_path = getattr(conn, "key_path", "") or ""
        known_hosts_path = os.path.expanduser(r"~\.ssh\known_hosts")
        client = paramiko.SSHClient()
        channel = None
        try:
            # Always load the whole file. Pre-detection with ssh-keygen/plain
            # scans misses hashed pins when OpenSSH isn't installed, enabling
            # an unsafe TOFU prompt for an already-known changed key.
            if os.path.exists(known_hosts_path):
                client.load_host_keys(known_hosts_path)
            if callable(self.host_key_verify_callback):
                client.set_missing_host_key_policy(
                    _TOFUAcceptPolicy(known_hosts_path, host, port, self.host_key_verify_callback)
                )
            else:
                client.set_missing_host_key_policy(paramiko.RejectPolicy())
            if auth_method == "key" and key_path:
                client.connect(host, port=port, username=user, key_filename=key_path, timeout=15)
            elif auth_method == "key" and getattr(conn, "putty_key_path", ""):
                client.connect(host, port=port, username=user,
                               key_filename=conn.putty_key_path, timeout=15)
            elif auth_method in ("password", "ask") and password:
                client.connect(host, port=port, username=user, password=password, timeout=15)
            else:
                raise ValueError("unsupported authentication method or missing credentials")
            channel = client.invoke_shell(term="xterm-256color", width=80, height=24)
            channel.setblocking(False)
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)
        except Exception as exc:
            logger.error("Terminal: SSH setup error for %s:%d – %s", host, port, exc)
            if channel is not None:
                channel.close()
            client.close()
            return None
        finally:
            password = ""

        session = TerminalSession(conn_id=conn_id, client=client, channel=channel)
        token = str(uuid.uuid4())
        with self._state_lock:
            if self._stopping:
                channel.close()
                client.close()
                return None
            previous = self._sessions.get(conn_id)
            self._sessions[conn_id] = session
            self._pending_tokens[token] = _PendingToken(conn_id=conn_id, session=session)
        if previous is not None:
            self._schedule_close(conn_id, previous)
        logger.debug("Terminal: session created for %s", conn_id)
        return token

    def _schedule_close(self, conn_id, expected=None):
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._close_session_async(conn_id, expected))
                )
            except RuntimeError:
                pass

    def close_session(self, conn_id: str):
        """Schedule session/channel/client/WebSocket cleanup; does not wait."""
        self._schedule_close(conn_id)

    def resize_session(self, conn_id: str, cols: int, rows: int):
        with self._state_lock:
            session = self._sessions.get(conn_id)
        if session and not session.channel.closed and cols > 0 and rows > 0:
            try:
                session.channel.resize_pty(width=cols, height=rows)
            except Exception as exc:
                logger.debug("Terminal: resize error for %s: %s", conn_id, exc)

    def is_session_alive(self, conn_id: str) -> bool:
        with self._state_lock:
            session = self._sessions.get(conn_id)
        return session is not None and not session.channel.closed

    async def _serve(self, ready_event: threading.Event):
        # Explicit legacy API: websockets 13+ also exposes a different one-arg
        # handler API. The frontend URL remains /ws/{single-use-token}.
        from websockets.legacy.server import serve

        self._server = await serve(
            self._ws_handler, "127.0.0.1", 0, max_size=_MAX_WS_MSG_BYTES,
            ping_interval=30, ping_timeout=10, close_timeout=1,
        )
        self._port = self._server.sockets[0].getsockname()[1]
        self._cleanup_task = asyncio.create_task(self._cleanup_expired_tokens())
        ready_event.set()

    async def _shutdown_and_stop(self):
        try:
            await self._shutdown()
        finally:
            asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)

    async def _shutdown(self):
        if self._shutdown_complete:
            return
        with self._state_lock:
            self._stopping = True
            sessions = list(self._sessions.values())
        if self._server is not None:
            self._server.close()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        await asyncio.gather(
            *(self._close_session_async(s.conn_id, s) for s in sessions),
            return_exceptions=True,
        )
        if self._server is not None:
            await self._server.wait_closed()
        # Include in-flight handlers/forwarders and scheduled close operations.
        remaining = [t for t in asyncio.all_tasks()
                     if t is not asyncio.current_task() and not t.done()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        with self._state_lock:
            self._pending_tokens.clear()
        self._shutdown_complete = True

    async def _expire_pending_tokens(self):
        now = time.monotonic()
        with self._state_lock:
            expired = [self._pending_tokens.pop(token)
                       for token, pt in list(self._pending_tokens.items())
                       if now - pt.created_at > _TOKEN_EXPIRY_SEC]
        for pending in expired:
            await self._close_session_async(pending.conn_id, pending.session)

    async def _cleanup_expired_tokens(self):
        while True:
            await asyncio.sleep(30)
            await self._expire_pending_tokens()

    async def _ws_handler(self, websocket, path: str):
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "ws":
            await websocket.close(1008, "Invalid path")
            return
        with self._state_lock:
            pending = self._pending_tokens.pop(parts[1], None)
        if pending is None:
            await websocket.close(1008, "Invalid token")
            return
        if time.monotonic() - pending.created_at > _TOKEN_EXPIRY_SEC:
            # Consuming an expired token must not bypass periodic cleanup.
            await self._close_session_async(pending.conn_id, pending.session)
            await websocket.close(1008, "Token expired")
            return
        conn_id = pending.conn_id
        with self._state_lock:
            session = self._sessions.get(conn_id)
        if session is None or (pending.session is not None and pending.session is not session):
            await websocket.close(1011, "Session not found")
            return
        session.websocket = websocket
        session.forwarder_task = asyncio.create_task(self._ssh_to_ws(session))
        try:
            async for message in websocket:
                if session.channel.closed:
                    break
                if isinstance(message, str):
                    # Binary frames NEVER enter the control parser. For text,
                    # consume only this explicitly recognized resize schema.
                    try:
                        ctrl = json.loads(message)
                    except (ValueError, TypeError):
                        ctrl = None
                    if (isinstance(ctrl, dict) and set(ctrl) == {"type", "cols", "rows"}
                            and ctrl["type"] == "resize"
                            and type(ctrl["cols"]) is int and type(ctrl["rows"]) is int
                            and 0 < ctrl["cols"] <= 65535 and 0 < ctrl["rows"] <= 65535):
                        self.resize_session(conn_id, ctrl["cols"], ctrl["rows"])
                        continue
                    message = message.encode("utf-8")
                session.channel.sendall(message)
        except Exception as exc:
            logger.debug("Terminal: WebSocket input ended: %s", exc)
        finally:
            await self._close_session_async(conn_id, session)

    async def _ssh_to_ws(self, session: TerminalSession):
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, self._recv_channel, session.channel)
                if data is None:
                    break
                if session.websocket is not None:
                    await session.websocket.send(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Terminal: SSH output ended: %s", exc)
        finally:
            # EOF/error must close WS so xterm shows its reconnect overlay, and
            # release SSH resources even when no more browser input arrives.
            await self._close_session_async(session.conn_id, session)

    @staticmethod
    def _recv_channel(channel) -> bytes | None:
        import select

        while True:
            # Drain both buffers BEFORE observing EOF/closed. Paramiko's
            # select fd represents stdout AND stderr: readiness alone never
            # means that a nonblocking stdout recv() will succeed.
            for ready, receive in ((channel.recv_ready, channel.recv),
                                   (channel.recv_stderr_ready, channel.recv_stderr)):
                if ready():
                    try:
                        data = receive(4096)
                    except (TimeoutError, BlockingIOError):
                        # Readiness is only a snapshot. Still try the other
                        # stream, then retry after select rather than closing WS.
                        continue
                    if data:
                        return data
            # Exit-status readiness is NOT stream EOF: a server may send the
            # status before its final stdout/stderr packets.
            if channel.closed or channel.eof_received:
                # The transport may have fed the final packet between our
                # initial readiness checks and setting EOF/closed.
                if channel.recv_ready() or channel.recv_stderr_ready():
                    continue
                return None
            # A buffer may have become ready since the checks above. Recheck
            # BOTH streams at the top even when select reports readability.
            select.select([channel], [], [], 0.2)

    async def _close_session_async(self, conn_id: str, expected=None):
        with self._state_lock:
            current = self._sessions.get(conn_id)
            session = expected if expected is not None else current
            if session is None:
                return
            if current is session:
                self._sessions.pop(conn_id, None)
            for token, pt in list(self._pending_tokens.items()):
                if pt.session is session or (pt.session is None and pt.conn_id == conn_id and current is session):
                    self._pending_tokens.pop(token, None)
        # Detach fields before await: concurrent handler/forwarder cleanup is
        # idempotent and an old generation never closes a replacement session.
        forwarder, session.forwarder_task = session.forwarder_task, None
        websocket, session.websocket = session.websocket, None
        for resource in (session.channel, session.client):
            try:
                resource.close()
            except Exception:
                logger.debug("Terminal: SSH resource close failed", exc_info=True)
        if forwarder is not None and forwarder is not asyncio.current_task():
            forwarder.cancel()
            await asyncio.gather(forwarder, return_exceptions=True)
        if websocket is not None:
            try:
                await websocket.close(1000, "SSH session closed")
            except Exception:
                logger.debug("Terminal: WebSocket close failed", exc_info=True)


from src.ui.host_key_utils import TOFUAcceptPolicy as _TOFUAcceptPolicy
