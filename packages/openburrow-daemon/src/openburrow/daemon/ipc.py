"""Daemon ↔ CLI IPC.

Newline-delimited JSON-RPC over a Unix domain socket (POSIX) or a named pipe
(Windows). Both are chosen over TCP for one reason: **the transport carries the
access control**. A socket with mode ``0600`` in the repo's own runtime directory
is reachable only by the user who owns it, with no port to scan and no firewall
rule to get wrong. A named pipe gets the same guarantee from its default security
descriptor, which grants the creating user and administrators and nobody else.

Framing is newline-delimited rather than length-prefixed because it is
debuggable: ``nc -U .openburrow/burrow.sock`` and type. That property is worth
more day to day than the marginal efficiency of a binary framing.

The two transports do **not** share an asyncio API, and this module used to
pretend they did::

    server = await asyncio.start_server(self._on_client, path=self.endpoint)

``start_server`` has no ``path`` parameter — that is ``start_unix_server`` only.
The keyword went straight through to ``loop.create_server``, which raised
``TypeError: BaseEventLoop.create_server() got an unexpected keyword argument
'path'`` on every Windows start. The daemon had never once come up on Windows, so
every daemon-backed command (``session``, ``coordination``, ``governance audit``,
``observability``) failed behind it.

The real APIs are different in kind, not just in name:

* a unix socket is served by ``asyncio.start_unix_server`` and hands you a
  ``StreamReader``/``StreamWriter`` pair;
* a named pipe is served by ``loop.start_serving_pipe`` and hands you a
  ``Protocol`` and a ``Transport``. There is no stream variant, and the pipe
  entry points exist only on the proactor loop.

:class:`_Connection` is the four-method surface that hides that difference from
the framing code below it, so the protocol logic exists once.

The protocol is deliberately request/response with an optional streaming mode,
not a general RPC framework. A CLI command sends one request, gets one response,
and exits.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import inspect
import json
import os
import stat
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from openburrow.core.errors import (
    BusError,
    DaemonAlreadyRunningError,
    DaemonNotRunningError,
    OpenBurrowError,
)
from openburrow.core.logging import get_logger
from openburrow.core.paths import BurrowPaths, is_windows

log = get_logger(__name__)

#: Cap on a single request line. A request bigger than this is a bug, not a use case.
MAX_FRAME_BYTES = 8 * 1024 * 1024

Handler = Callable[[dict[str, Any]], Awaitable[Any]]

#: A handler that streams its result back frame by frame, e.g. ``bus.stream``.
#:
#: A separate alias because such a handler is **called** differently, not merely
#: typed differently: an async generator function returns a generator object, and
#: awaiting that object raises ``TypeError``. Declaring both kinds as ``Handler``
#: is what let ``bus.stream`` be registered, look fully wired up, and fail on
#: every call — the dispatcher awaited a generator, and the resulting error was
#: swallowed by the same broad ``except Exception`` that exists to keep the
#: socket alive when one handler misbehaves.
StreamHandler = Callable[[dict[str, Any]], AsyncGenerator[dict[str, Any], None]]

#: What :meth:`IpcServer.register` accepts. The dispatcher branches on the same
#: distinction, so the type and the behaviour cannot drift apart.
AnyHandler = Handler | StreamHandler


@dataclass(slots=True)
class IpcRequest:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_line(self) -> bytes:
        return (json.dumps(self.to_dict(), separators=(",", ":")) + "\n").encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "method": self.method, "params": self.params}


@dataclass(slots=True)
class IpcResponse:
    id: str = ""
    result: Any = None
    error: dict[str, Any] | None = None
    #: True when the server is emitting a stream of results for one request.
    more: bool = False

    @classmethod
    def parse(cls, line: str | bytes) -> IpcResponse:
        payload = json.loads(line)
        return cls(
            id=str(payload.get("id", "")),
            result=payload.get("result"),
            error=payload.get("error"),
            more=bool(payload.get("more", False)),
        )

    @property
    def ok(self) -> bool:
        return self.error is None

    def raise_for_error(self) -> None:
        if self.error is None:
            return
        raise BusError(
            str(self.error.get("message", "daemon returned an error")),
            hint=self.error.get("hint"),
            context=self.error.get("context") or {},
        )


# --------------------------------------------------------------------------
# Transport abstraction
# --------------------------------------------------------------------------
class _Connection(abc.ABC):
    """The transport-shaped surface the framing protocol actually needs.

    Five methods is the whole of it. Everything above this class — request
    dispatch, streaming, error envelopes — is written once and runs unchanged on
    a unix socket and on a named pipe.
    """

    @abc.abstractmethod
    async def readline(self) -> bytes:
        """Return one newline-terminated frame, or ``b""`` at end of stream."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    async def drain(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    async def wait_closed(self) -> None: ...


class _StreamConnection(_Connection):
    """A unix socket connection. Thin by design — ``StreamReader`` already
    does the buffering, framing, and backpressure that ``_PipeConnection`` has
    to reimplement."""

    __slots__ = ("_reader", "_writer")

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def readline(self) -> bytes:
        return await self._reader.readline()

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        # Best-effort. The peer may already be gone, and a failed close on a
        # connection that is being torn down anyway is not worth a log line on
        # every disconnect.
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


class _PipeConnection(_Connection):
    """A Windows named pipe presented as a line-framed stream.

    The proactor hands us bytes as they arrive, through ``data_received`` on the
    protocol, and there is no reader object to await. So the buffering that
    ``StreamReader`` provides for free has to happen here: bytes land in a
    ``bytearray``, and ``readline`` parks on a future that ``feed_data`` wakes.
    """

    __slots__ = ("_buffer", "_closed", "_eof", "_transport", "_waiter")

    def __init__(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport
        self._buffer = bytearray()
        self._waiter: asyncio.Future[None] | None = None
        self._eof = False
        self._closed = False

    # --- called by the protocol, not by the framing code -------------------
    def feed_data(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._wake()

    def feed_eof(self) -> None:
        self._eof = True
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    # --- _Connection -------------------------------------------------------
    async def readline(self) -> bytes:
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line

            if self._eof:
                # A frame that never got its newline is malformed, and the stream
                # transport discards it too — ``StreamReader.readline`` raises
                # ``IncompleteReadError``, which the read loop treats as a
                # disconnect. Dropping the partial buffer here keeps the two
                # transports behaviourally identical instead of parsing half a
                # request and replying to a socket that has already gone.
                self._buffer.clear()
                return b""

            if self._closed:
                return b""

            if len(self._buffer) > MAX_FRAME_BYTES:
                # Hand the oversized frame back so the caller's length check can
                # produce its proper ``frame_too_large`` error. Without this the
                # buffer would grow until the process died, which turns a
                # malformed client into a denial of service on the daemon.
                line = bytes(self._buffer)
                self._buffer.clear()
                return line

            # There is no ``await`` between the checks above and the assignment
            # below, so a ``data_received`` callback cannot slip in and wake a
            # waiter that does not exist yet. That is the entire reason this is
            # safe without a lock.
            self._waiter = asyncio.get_running_loop().create_future()
            await self._waiter
            self._waiter = None

    def write(self, data: bytes) -> None:
        if not self._closed:
            # The concrete transport handed to us by ``create_connection`` is a
            # writable one; mypy only sees the declared ``BaseTransport`` supertype.
            self._transport.write(data)  # type: ignore[attr-defined]

    async def drain(self) -> None:
        # Nothing to await: ``Transport.write`` hands the bytes to the proactor,
        # which owns the kernel buffer. The method exists because the framing
        # code calls it on both transports, and having one of them silently lack
        # it would be a bug waiting for the platform that is not the developer's.
        return None

    def close(self) -> None:
        self._closed = True
        self._wake()
        with contextlib.suppress(Exception):
            self._transport.close()

    async def wait_closed(self) -> None:
        return None


class _PipeProtocol(asyncio.Protocol):
    """Bridges proactor callbacks to a :class:`_PipeConnection`.

    ``loop.start_serving_pipe`` calls the protocol factory once per accepted
    client and ``loop.create_pipe_connection`` calls it once for the outbound
    side, so per-connection state belongs on the instance.

    The connection object is built in ``connection_made``, and that is deferred:
    the proactor schedules it with ``call_soon``, so it has **not** run by the
    time ``create_pipe_connection`` returns. Reading ``.connection`` straight
    after that await is a race the outbound side always loses. Callers that need
    the connection must await :meth:`expect_connection` instead.
    """

    def __init__(self, on_connection: Callable[[_PipeConnection], None] | None = None) -> None:
        self._on_connection = on_connection
        self.connection: _PipeConnection | None = None
        self._connected: asyncio.Future[_PipeConnection] | None = None

    def expect_connection(self) -> asyncio.Future[_PipeConnection]:
        """A future resolved once ``connection_made`` has run."""
        self._connected = asyncio.get_running_loop().create_future()
        return self._connected

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        connection = _PipeConnection(transport)
        self.connection = connection
        if self._connected is not None and not self._connected.done():
            self._connected.set_result(connection)
        if self._on_connection is not None:
            self._on_connection(connection)

    def data_received(self, data: bytes) -> None:
        if self.connection is not None:
            self.connection.feed_data(data)

    def eof_received(self) -> bool:
        if self.connection is not None:
            self.connection.feed_eof()
        # Returning False lets the transport close, which is what we want: the
        # client is done and there is nothing to write back to it.
        return False

    def connection_lost(self, exc: BaseException | None) -> None:
        if self.connection is not None:
            self.connection.feed_eof()
        elif self._connected is not None and not self._connected.done():
            # The transport died before it was ever usable. Without this the
            # caller awaits a future that nothing will resolve.
            self._connected.set_exception(
                BusError("the named pipe closed before the connection was established")
            )


def _require_pipe_api() -> Any:
    """Return the running loop, checked for the named-pipe entry points.

    Named pipes are a proactor feature. Under
    ``WindowsSelectorEventLoopPolicy`` the methods simply are not there, and an
    ``AttributeError`` mentioning ``start_serving_pipe`` is not a useful thing to
    hand someone whose daemon will not start — so it is converted into an error
    that names the actual cause.
    """
    loop = asyncio.get_running_loop()
    missing = [
        name for name in ("start_serving_pipe", "create_pipe_connection") if not hasattr(loop, name)
    ]
    if missing:
        raise BusError(
            "this event loop cannot serve Windows named pipes",
            hint=(
                "Named pipes need the proactor loop, which is the default on "
                "Windows. Something set a different event loop policy — remove "
                "the WindowsSelectorEventLoopPolicy override."
            ),
            context={"loop": type(loop).__name__, "missing": missing},
        )
    return loop


class IpcServer:
    """Serves the daemon's control plane."""

    def __init__(self, paths: BurrowPaths) -> None:
        self.paths = paths
        self._handlers: dict[str, AnyHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._pipe_servers: list[Any] = []
        self._connections: set[_Connection] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self, method: str, handler: AnyHandler) -> None:
        """Register a method. Names are ``namespace.action`` by convention."""
        self._handlers[method] = handler

    @property
    def endpoint(self) -> str:
        return self.paths.ipc_endpoint

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> str:
        self._prepare_socket_path()

        if is_windows():
            loop = _require_pipe_api()
            try:
                self._pipe_servers = list(
                    await loop.start_serving_pipe(
                        partial(_PipeProtocol, self._accept), self.endpoint
                    )
                )
            except PermissionError as exc:
                # Windows refuses a second server on a name that is already bound
                # (``FILE_FLAG_FIRST_PIPE_INSTANCE``). Measured, not assumed: a
                # second ``start_serving_pipe`` for a live name raises
                # ``PermissionError [WinError 5]``, so this is a reliable
                # already-running signal rather than a guess about errno.
                raise DaemonAlreadyRunningError(
                    "a burrow daemon is already listening on the control pipe",
                    hint="Use `burrow daemon status`, or `burrow daemon stop`.",
                    context={"endpoint": self.endpoint},
                    cause=exc,
                ) from exc
        else:
            # POSIX-only API: mypy on Windows resolves the platform-stubbed
            # asyncio module, which hides it even though this branch never runs
            # on Windows (the ``if is_windows()`` guard above takes the pipe path).
            self._server = await asyncio.start_unix_server(  # type: ignore[attr-defined]
                self._on_client,
                path=self.endpoint,
                # Without this the reader's own 64 KiB default limit would fire
                # long before the declared 8 MiB cap, and a large request would
                # surface as a ``ValueError`` from deep inside ``readline``
                # instead of the ``frame_too_large`` error the protocol
                # documents.
                limit=MAX_FRAME_BYTES,
            )
            self._harden_socket_permissions()

        log.info("ipc.listening", endpoint=self.endpoint, methods=len(self._handlers))
        return self.endpoint

    async def stop(self) -> None:
        for connection in list(self._connections):
            connection.close()
        self._connections.clear()

        # Closing the connections wakes every parked ``readline``, so these tasks
        # would finish on their own — but only once whatever handler they are
        # inside returns, and a teardown path's whole job is to finish
        # deterministically. Cancel, then reap.
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        for pipe_server in self._pipe_servers:
            with contextlib.suppress(Exception):
                pipe_server.close()
        self._pipe_servers.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if not is_windows():
            # The socket file may already be gone, or may never have been created
            # if startup failed before bind. Neither is worth an error on the way
            # out, which makes this a suppression rather than a handler.
            with contextlib.suppress(OSError):
                # Blocking on purpose. This is a single unlink of a local socket
                # path during shutdown, which is microseconds of syscall; routing it
                # through a thread would add an await point to a teardown path whose
                # whole job is to finish deterministically.
                Path(self.endpoint).unlink(missing_ok=True)  # noqa: ASYNC240

    def _prepare_socket_path(self) -> None:
        if is_windows():
            # Nothing to prepare. A named pipe is a kernel object, not a file, so
            # there is no stale inode to clear and no parent directory to create.
            return
        path = Path(self.endpoint)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from a crashed daemon would make bind() fail.
        if path.exists():
            try:
                path.unlink()
                log.info("ipc.stale_socket_removed", path=str(path))
            except OSError as exc:
                raise BusError(
                    f"could not remove a stale socket at {path}: {exc}",
                    hint="Another daemon may still be running. Try `burrow daemon status`.",
                    context={"path": str(path)},
                    cause=exc,
                ) from exc

    def _harden_socket_permissions(self) -> None:
        """Restrict the socket to its owner.

        This is the access control for the whole control plane, so it is applied
        explicitly rather than relying on umask — a permissive umask would
        otherwise make the daemon reachable by every user on the machine.

        Windows needs no equivalent: a named pipe is created with a default
        security descriptor that grants the creating user and administrators, and
        that default is already the policy we want.
        """
        mode = int(os.environ.get("OPENBURROW_SOCKET_MODE", "0600"), 8)
        try:
            Path(self.endpoint).chmod(mode)
        except OSError as exc:  # pragma: no cover - platform dependent
            log.warning("ipc.chmod_failed", path=self.endpoint, error=str(exc))

    # --- connection handling ----------------------------------------------
    def _accept(self, connection: _PipeConnection) -> None:
        """Start serving a freshly accepted named-pipe connection."""
        self._spawn(self._serve(connection))

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._serve(_StreamConnection(reader, writer))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a per-connection coroutine, holding a strong reference to it.

        A bare ``asyncio.create_task`` result is only weakly referenced by the
        loop, so a task that has not been scheduled yet can be garbage-collected
        before it ever runs — and then the client waits forever for a reply that
        no code is left alive to produce. Keeping the task in a set until it
        completes is the whole fix.
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.warning("ipc.connection_task_failed", error=str(error))

    async def _serve(self, connection: _Connection) -> None:
        self._connections.add(connection)
        try:
            while True:
                try:
                    line = await connection.readline()
                except (ConnectionResetError, asyncio.IncompleteReadError):
                    break
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES:
                    await self._write(
                        connection,
                        IpcResponse(
                            error={"code": "frame_too_large", "message": "request exceeds 8 MiB"}
                        ),
                    )
                    break
                await self._dispatch_line(connection, line)
        except Exception as exc:
            log.warning("ipc.client_error", error=str(exc))
        finally:
            self._connections.discard(connection)
            connection.close()
            await connection.wait_closed()

    async def _dispatch_line(self, connection: _Connection, line: bytes) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            await self._write(
                connection,
                IpcResponse(error={"code": "bad_json", "message": f"request is not JSON: {exc}"}),
            )
            return

        request_id = str(payload.get("id", ""))
        method = str(payload.get("method", ""))
        params = payload.get("params") or {}

        handler = self._handlers.get(method)
        if handler is None:
            await self._write(
                connection,
                IpcResponse(
                    id=request_id,
                    error={
                        "code": "unknown_method",
                        "message": f"no handler for {method!r}",
                        "context": {"known": sorted(self._handlers)},
                    },
                ),
            )
            return

        try:
            # A streaming handler is called, never awaited: `await` on the
            # generator it returns raises TypeError, which is how a registered
            # and apparently wired-up `bus.stream` failed on every request.
            if inspect.isasyncgenfunction(handler):
                async for chunk in handler(params):
                    await self._write(
                        connection, IpcResponse(id=request_id, result=chunk, more=True)
                    )
                await self._write(connection, IpcResponse(id=request_id, result=None, more=False))
                return
            # The non-streaming branch: the generator case returned above, so the
            # narrowed handler is awaitable here — mypy cannot see through the
            # ``inspect.isasyncgenfunction`` guard, hence the ignore.
            result = await handler(params)  # type: ignore[misc]
        except OpenBurrowError as exc:
            await self._write(connection, IpcResponse(id=request_id, error=exc.to_dict()))
            return
        except Exception as exc:
            log.exception("ipc.handler_crashed", method=method)
            await self._write(
                connection,
                IpcResponse(
                    id=request_id,
                    error={"code": "internal_error", "message": str(exc)},
                ),
            )
            return

        await self._write(connection, IpcResponse(id=request_id, result=result))

    @staticmethod
    async def _write(connection: _Connection, response: IpcResponse) -> None:
        payload = {"id": response.id, "more": response.more}
        if response.error is not None:
            payload["error"] = response.error
        else:
            payload["result"] = response.result
        connection.write((json.dumps(payload, default=str, separators=(",", ":")) + "\n").encode())
        await connection.drain()


class IpcClient:
    """Dials the daemon's control plane."""

    def __init__(self, paths: BurrowPaths, *, timeout: float = 30.0) -> None:
        self.paths = paths
        self.timeout = timeout

    async def call(self, method: str, **params: Any) -> Any:
        """Send one request, return its result."""
        response = await self._send(method, params)
        response.raise_for_error()
        return response.result

    async def stream(self, method: str, **params: Any) -> AsyncIterator[Any]:
        """Send one request and yield each streamed chunk.

        Used by ``burrow observability watch`` and ``burrow observability logs
        -f``, where one request produces a long-lived sequence rather than a
        single reply.
        """
        connection = await self._dial()
        try:
            request = IpcRequest(method=method, params=params, id="stream")
            connection.write(request.to_line())
            await connection.drain()
            while True:
                line = await connection.readline()
                if not line:
                    break
                response = IpcResponse.parse(line)
                if not response.ok:
                    response.raise_for_error()
                if response.result is not None:
                    yield response.result
                if not response.more:
                    break
        finally:
            connection.close()
            await connection.wait_closed()

    async def ping(self) -> bool:
        try:
            await asyncio.wait_for(self.call("daemon.ping"), timeout=2.0)
            return True
        except Exception:
            return False

    # --- internals ---------------------------------------------------------
    async def _send(self, method: str, params: dict[str, Any]) -> IpcResponse:
        connection = await self._dial()
        try:
            request = IpcRequest(method=method, params=params, id="cli")
            connection.write(request.to_line())
            await connection.drain()
            line = await asyncio.wait_for(connection.readline(), timeout=self.timeout)
            if not line:
                raise BusError("daemon closed the connection without responding")
            return IpcResponse.parse(line)
        except TimeoutError as exc:
            raise BusError(
                f"daemon did not respond to {method!r} within {self.timeout}s",
                hint="The daemon may be busy. Check `burrow daemon status` and its logs.",
                context={"method": method},
                cause=exc,
            ) from exc
        finally:
            connection.close()
            await connection.wait_closed()

    async def _dial(self) -> _Connection:
        """Open a connection to the daemon, or explain why it is not there."""
        endpoint = self.paths.ipc_endpoint
        try:
            return await self._connect(endpoint)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
            # ``FileNotFoundError`` is what Windows raises for a pipe with no
            # server, and ``ConnectionRefusedError``/``FileNotFoundError`` are
            # both ``OSError`` subclasses — the tuple is documentation, not
            # redundancy, because those two are the cases that actually occur.
            raise self._not_running(exc) from exc

    @staticmethod
    async def _connect(endpoint: str) -> _Connection:
        if is_windows():
            loop = _require_pipe_api()
            protocol = _PipeProtocol()
            # ``create_pipe_connection`` takes a factory, but the protocol has to
            # exist first so this side can reach the connection it builds. The
            # lambda hands back the instance we already made, and the future is
            # what waits for the deferred ``connection_made``.
            connected = protocol.expect_connection()
            transport, _ = await loop.create_pipe_connection(lambda: protocol, endpoint)
            try:
                return await connected
            except BaseException:
                # A transport that is dropped without being closed makes the
                # proactor's ``__del__`` raise ``ValueError: I/O operation on
                # closed pipe`` on an unrelated later line, which is a miserable
                # thing to debug.
                with contextlib.suppress(Exception):
                    transport.close()
                raise

        # POSIX-only API under a platform-stubbed asyncio; see the server-side
        # ``start_unix_server`` note above.
        reader, writer = await asyncio.open_unix_connection(  # type: ignore[attr-defined]
            path=endpoint, limit=MAX_FRAME_BYTES
        )
        return _StreamConnection(reader, writer)

    def _not_running(self, exc: BaseException) -> DaemonNotRunningError:
        return DaemonNotRunningError(
            "the burrow daemon is not running",
            hint="Start it with `burrow daemon start`, or run the command with --no-daemon.",
            context={"endpoint": self.paths.ipc_endpoint},
            cause=exc,
        )


def socket_is_live(path: Path) -> bool:
    """Check whether a unix socket file exists *and* has a listener.

    A socket file with no listener is what a crashed daemon leaves behind, and
    treating it as "running" is how you get a daemon that refuses to start.

    POSIX only. Windows has no socket file to inspect and does not need this:
    the kernel refuses a second server on a bound pipe name, so the daemon
    discovers an already-running peer when it tries to bind rather than by
    probing for one.
    """
    if is_windows():
        return False
    if not path.exists():
        return False
    mode = path.stat().st_mode
    if not stat.S_ISSOCK(mode):
        return False
    import socket

    # ``AF_UNIX`` does not exist on Windows, and this function returns False
    # there two checks above; mypy still resolves the Windows socket stub.
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        probe.close()


__all__ = [
    "MAX_FRAME_BYTES",
    "AnyHandler",
    "Handler",
    "IpcClient",
    "IpcRequest",
    "IpcResponse",
    "IpcServer",
    "StreamHandler",
    "socket_is_live",
]
