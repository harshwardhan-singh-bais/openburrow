# Deployment

Three shapes, in increasing order of infrastructure.

| Shape | Needs | Use when |
|---|---|---|
| **Local** | Python 3.12+, git, `uv` | one developer, one machine |
| **Team** | the above plus a relay and Postgres | remote members, shared session history |
| **Air-gapped** | a pre-built environment | no network egress |

## Local

```bash
uv tool install openburrow

cd ~/code/my-project
burrow init
burrow doctor
burrow session start --template pair
```

State lives in `.openburrow/` inside the repository:

```
.openburrow/
├── burrow.sqlite        # bus log, sessions, lanes, tasks, audit
├── burrow.sock          # IPC endpoint (a named pipe on Windows)
├── burrow.pid
├── worktrees/           # one git worktree per lane
├── casts/               # session reel recordings
└── reels/               # exported reel bundles
```

Add `.openburrow/` to `.gitignore` — `burrow init` does this for you.

**Worktrees are the isolation boundary.** Each lane gets its own git worktree, so two
lanes editing the same file do not corrupt each other's checkout. They are created
under `.openburrow/worktrees/` and removed when the session closes.

**The database is a single file**, which makes it trivial to inspect, back up, or
attach to a bug report. Use `Database.backup()` rather than copying the file — copying
a WAL-mode database without its `-wal` sidecar produces a database missing the most
recent writes.

### Running the daemon as a service

The daemon is designed to be long-lived, so the CLI's import cost is per invocation
rather than per operation.

**systemd (Linux):**

```ini
[Unit]
Description=OpenBurrow daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/USER/code/my-project
ExecStart=/home/USER/.local/bin/burrow daemon start --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

**launchd (macOS):** a `LaunchAgent` plist with `KeepAlive` and the same
`--foreground` command.

**Windows:** a scheduled task at logon running `burrow daemon start --foreground`.
Detached spawn is supported, but a supervisor that restarts it is better.

`--foreground` in all three cases: the daemon should not detach when something else is
already supervising it, or you get two processes fighting over the PID file.

## Team

Adds the relay: a FastAPI service with Postgres, for remote members and shared session
history.

```bash
# the relay needs Postgres and a signing secret
export OPENBURROW_RELAY_JWT_SECRET="$(openssl rand -base64 48)"
export OPENBURROW_DATABASE_URL="postgresql+asyncpg://user:pw@host/openburrow"

burrow relay serve --host 0.0.0.0 --port 8787
```

### Production invariants are enforced, not documented

`Settings` refuses to start in production if any of these hold:

| Condition | Why it is a hard error |
|---|---|
| `OPENBURROW_RELAY_JWT_SECRET` unset | A relay with a default secret is not a relay with weak auth; it is a relay with no auth. |
| `OPENBURROW_ALLOW_INSECURE_HTTP=true` | Credentials and session content in plaintext. |
| `OPENBURROW_SANDBOX_NETWORK=allow` | Lanes unrestricted by default in a shared deployment. |
| `OPENBURROW_CHAOS_ENABLED=true` | Chaos flags inject failures. Never in production. |

These are startup errors rather than warnings because a warning in a log is a warning
nobody reads, and each of them is a security control rather than a preference.

### Reverse proxy

The relay speaks HTTP and WebSocket on one port. WebSocket upgrade must be forwarded.

**nginx:**

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;   # long-lived session sockets
}
```

`proxy_read_timeout` matters: the default 60s will drop a WebSocket that is idle while
a lane is thinking, and the client will reconnect — which looks like flapping.

### Database

`burrow relay migrate` applies the schema. Alembic migrations live in
`packages/openburrow-core/src/openburrow/core/db/migrations/`.

The relay's database is **separate** from any daemon's local SQLite. It has its own
tenancy model and its own retention, and reconciling a relay's history with a
daemon's local log is not supported — that would create two sources of truth, which
[ADR 0004](../architecture/adr/0004-append-only-bus-log.md) exists to prevent.

## Containers

```bash
docker compose up            # relay + Postgres, for development
docker compose -f docker-compose.yml -f docker/compose.prod.yml up -d
```

Lanes can be run in containers for real isolation. This is the **only** mechanism that
gives genuine tool-level enforcement — see
[../protocols/mcp.md](../protocols/mcp.md#if-you-need-tool-level-control). The
governance layer constrains what a lane is *authorised* to do; a container constrains
what it *can* do. They are complementary, and the container is the one that holds when
a harness does not cooperate.

## Air-gapped

No network egress means three things need solving.

**1. Python and dependencies.** Build a wheelhouse on a connected machine:

```bash
uv export --format requirements-txt > requirements.txt
uv pip download -r requirements.txt -d ./wheelhouse
uv build --all -o ./wheelhouse
```

Then `uv pip install --no-index --find-links ./wheelhouse ...` on the target.

**2. LLM access.** The conflict judge and lesson classifier need a model. Point them
at a local runtime:

```bash
OPENBURROW_LLM_MODEL=ollama/qwen2.5-coder:32b
OPENBURROW_LLM_BASE_URL=http://localhost:11434
```

**Both features degrade without a model**, and the degradation is reported rather than
hidden. The Radar still detects deterministic file collisions — which is the majority
of its value — and `RadarStats.judge_coverage` reports `0.0` with
`semantic_available=False`, so a report cannot claim semantic coverage it did not
have.

**3. Distribution.** Python does not produce a single static binary
([ADR 0005](../architecture/adr/0005-python-not-rust.md)). Options, in order:

- `uv tool install` from a local wheelhouse — the supported path.
- A container image, if the target can run containers.
- PyInstaller or Nuitka — on the roadmap, with the caveat that a frozen build of an
  application that spawns arbitrary subprocesses needs real testing.

## Observability

OpenTelemetry is wired through the daemon. Set an OTLP endpoint and traces, metrics,
and logs export automatically.

```bash
OPENBURROW_OTEL_ENABLED=true
OPENBURROW_OTEL_ENDPOINT=http://localhost:4317
OPENBURROW_OTEL_SERVICE_NAME=openburrow
```

| Signal | What to watch |
|---|---|
| `bus_events` rate by type | A spike in `governance.flag` means something changed. |
| `lane.restarts` | Repeated restarts mean a harness is failing, not that supervision works. |
| `negotiation_precision` | Below ~0.5 means the Radar is crying wolf. |
| `message_effectiveness` | High volume with low effectiveness is expensive noise. |
| `judge_coverage` | Below 1.0 means the semantic Radar signal was partly absent. |

Prometheus scraping is available from the relay at `/metrics`.

## Notifications

`OPENBURROW_NOTIFY_*` configures Slack, Discord, Teams, SMTP, and GitHub. Notifications
fire on approvals requested, governance blocks, and session completion.

Keep them on the events a human must act on. A notification stream that includes
routine events gets muted, and a muted channel is worse than no channel because
everyone believes it is being watched.

## Health checks

| Check | Command | Expected |
|---|---|---|
| Daemon up | `burrow daemon status` | exit 0 |
| Environment | `burrow doctor` | exit 0 |
| Adapters | `burrow adapters health` | per-harness status |
| Protocols | `python scripts/smoke_test.py` | `3/3 suites passed` |
| Relay | `curl -f http://relay:8787/healthz` | 200 |

`scripts/smoke_test.py` is the one that matters. It stands up two real A2A servers,
does a real HTTP round trip, runs a real negotiation, and asserts the governance
invariants. It is fast enough to run as a deployment gate.
