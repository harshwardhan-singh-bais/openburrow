# openburrow-core

> The substrate. Domain models, layered configuration, and the local append-only state log.

`openburrow-core` is the one package every other OpenBurrow package depends on,
and it depends on nothing else in the project. That one-way rule is what keeps
the dependency graph acyclic and lets tools install the schemas alone — a CI
checker, an audit script, a web viewer — without pulling in the daemon.

## What lives here

| Module | Responsibility |
|---|---|
| `core.config` | Two-layer configuration: machine settings (env) and repo policy (`openburrow.yaml`), merged with documented precedence |
| `core.models` | The domain vocabulary — sessions, lanes, A2A tasks, bus messages, claims, plans, delegations, audit records |
| `core.db` | SQLite schema v1, the async engine with its PRAGMAs, and the append-only bus + audit logs |
| `core.logging` | structlog setup with correlation-field propagation across async tasks |
| `core.errors` | Exception hierarchy with stable machine codes and human hints |
| `core.paths` | Every filesystem location, resolved in one place |

## Configuration model

Two files, two owners, deliberately separate:

```
process env  ─┐
              ├─▶ Settings        (machine: secrets, ports, provider keys)
.env         ─┘
openburrow.yaml ─▶ RepoConfig     (team: lanes, policy, risk tiers, budgets)
                          │
                          ▼
                   ResolvedConfig  ← everything downstream consumes this
```

**Precedence: process env > `.env` > `openburrow.yaml` > built-in defaults.**

The asymmetry is intentional: the environment is what a developer changes at
2am to debug something, so it must always win. `openburrow.yaml` is team policy
that went through code review, so it is the baseline.

One exception to the asymmetry, and it matters: **environment settings can make
governance stricter but never weaker.** A developer cannot silently disable the
delegation ledger for a repo whose committed policy demands it. See
`ResolvedConfig.governance`.

## The append-only bus log

`bus_events` is the canonical record. It is never updated in place. Everything
that displays, replays, or audits a session reads from it:

```
lane writes ──▶ BusEventLog.append()  ──▶  bus_events (seq, payload JSON)
                                             │
                        ┌────────────────────┼────────────────────┐
                        ▼                    ▼                    ▼
                  TUI live feed      replay exporter      metrics rollup
```

`seq` is a monotonic integer assigned on insert, not the ULID. ULIDs sort at
millisecond granularity, which is not fine enough when several lanes emit in the
same millisecond. Replay always reads `ORDER BY seq`.

## Usage

```python
from openburrow.core import load_config
from openburrow.core.db import BusEventLog, Repository, init_database

config = load_config()  # merges env + openburrow.yaml
db = await init_database(config.db_url)  # applies PRAGMAs, creates tables

async with db.session() as session:
    repo = Repository(session)
    log = BusEventLog(session)
    await log.append(event_type="lane.started", session_id=sid, lane_id=lid)
```

## Testing

```bash
uv run pytest packages/openburrow-core -q
uv run pytest packages/openburrow-core -q -m "not integration"
```

Tests use `sqlite+aiosqlite:///:memory:` by default, so they need no fixtures on
disk. Integration-marked tests exercise WAL mode and concurrent writers.
