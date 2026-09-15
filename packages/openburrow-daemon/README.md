# openburrow-daemon

The long-running process that owns every piece of live state: the database, the
append-only bus log, the running harness processes, their per-lane A2A servers,
the file watchers, and the control socket the CLI talks to.

## Why a daemon at all

This is the question worth answering before any of the code, because "just run
it per command" is the obvious alternative and it fails for a specific reason.

**Lanes are long-lived.** A harness mid-task cannot survive its parent process
exiting. If `burrow lane start` spawned a subprocess and returned, the harness
would be orphaned — still running, with nobody holding its stdin, its exit
status, or the A2A server that makes it addressable. And two lanes cannot
message each other if there is no process holding both.

So the daemon is not an optimisation. It is what makes *asynchronous*
agent-to-agent collaboration possible at all, as opposed to "agents taking turns
inside a single command". A per-command model can only ever do the latter.

The cost is real and worth stating: a daemon is a thing that can be running when
you do not expect it, holding a stale socket, and refusing to start. Most of
`ipc.py` and the process-hygiene half of `server.py` exists to pay that cost
down.

## The control plane is a socket, not a port

The CLI talks to the daemon over a Unix domain socket on POSIX and a named pipe
on Windows. TCP was rejected deliberately.

The reason is that **filesystem permissions are the access control**. A socket
with mode `0600` inside the repo's own runtime directory is reachable only by
the user who owns it. There is no port to scan, no bind address to get wrong, no
firewall rule to misconfigure, and no accidental `0.0.0.0`. A loopback TCP port
would have been *nearly* as good and meaningfully easier to get wrong — every
"it bound to all interfaces" bug is one config line away.

The mode is applied explicitly rather than left to `umask`, because a permissive
umask would silently make the control plane reachable by every user on the
machine. `OPENBURROW_SOCKET_MODE` can override it; the default is `0600`.

Framing is newline-delimited JSON rather than length-prefixed binary, and that
is a debuggability decision:

```bash
nc -U .openburrow/burrow.sock
{"id":"x","method":"daemon.status","params":{}}
```

That property is worth more day to day than the marginal efficiency of binary
framing, and the frames here are small.

## A socket file is not a running daemon

The single most common daemon bug is treating a leftover socket file as proof of
a live process. A crashed daemon leaves exactly that, and the naive check
("does the path exist?") then produces a daemon that refuses to start forever
with no way to recover except `rm`.

`socket_is_live()` therefore checks the file *and* probes it:

```python
mode = path.stat().st_mode
if not stat.S_ISSOCK(mode):
    return False
probe.connect(str(path))  # refused => the file is a tombstone
```

Startup does the same for the pidfile: a PID that is not alive is logged as
`daemon.stale_pidfile` and removed, not treated as a conflict.

## Shutdown order is load-bearing

`stop()` does four things and the order is not arbitrary:

1. **Lanes stop first** — so that no new writes arrive while we are draining.
   Bounded by `daemon_shutdown_grace_s`; a timeout logs
   `daemon.session_drain_timeout` and proceeds rather than hanging.
2. **The bus flushes** — a final `daemon.stopped` event with the uptime. Wrapped
   in its own `try`, because a failed log write must never block shutdown.
3. **The IPC socket closes** — so a client gets a clean
   connection-refused rather than a half-dead socket that accepts and then
   hangs.
4. **The database closes last** — everything above may still be writing.

A daemon killed mid-write is recoverable; that is what the append-only log and
the checkpoints are for. But being killed is strictly worse than shutting down,
so SIGINT and SIGTERM both route into this path. On Windows,
`loop.add_signal_handler` is unavailable, so the fallback is a plain
`KeyboardInterrupt` — the same graceful stop, triggered one level up.

## What it does not cover

- **It is not a scheduler.** Nothing in the daemon decides *when* a lane should
  run or *what* it should work on. Sessions and lanes are started by the CLI or
  the relay; the daemon only holds them.
- **It does not mediate tool calls.** MCP is passthrough by design (see
  [ADR 0002](../../docs/architecture/adr/0002-mcp-passthrough.md)). The daemon
  supervises harness *processes*, not the tools they call.
- **It does not enforce governance by itself.** It constructs the
  `SessionManager` with the human identity from settings and hands governance
  the hooks it needs; the ledger and detectors live in `openburrow-governance`.
- **It is not horizontally scalable.** One daemon owns one repo's SQLite file.
  Multiple machines talking to one another is the relay's job, and the relay
  does not run lanes.

## Layout

| Module | Responsibility |
| --- | --- |
| `server.py` | `Daemon`, lifecycle, signal handling, control-plane handlers |
| `ipc.py` | Socket/pipe server and client, framing, socket hardening |
| `bus.py` | In-process pub/sub over the persisted bus log |
| `sessions.py` | `SessionManager`, lane supervision, harness process lifecycle |
| `filewatch.py` | `WorktreeWatcher` and the watcher pool that feeds Radar |

## See also

- [ADR 0004 — the append-only bus log](../../docs/architecture/adr/0004-append-only-log.md)
- [docs/protocols/a2a.md](../../docs/protocols/a2a.md) — why each lane runs its own server
- [openburrow-cli](../openburrow-cli/README.md) — the client half of this protocol
