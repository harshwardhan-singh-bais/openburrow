# 0006 — SQLite locally, Postgres for the relay

**Status:** Accepted

## Context

The daemon needs durable storage for the bus log, sessions, lanes, tasks, claims,
the plan, the Brain, lessons, checkpoints, and audit records. Two deployment shapes
have to be served:

- **A single developer** running one session on a laptop, who should not have to
  install, configure, and run a database server to try a tool.
- **A team** with a relay, where multiple daemons report to a shared service and
  remote members watch sessions.

## Decision

SQLite in WAL mode for local storage. Postgres for the relay. Both go through the
same SQLModel/SQLAlchemy layer, so the domain models are identical and only the
engine differs.

## Rejected

**Postgres everywhere.** Rejected because "install a database server" is a
meaningful barrier for the single-developer case, and the tool's value proposition
is best demonstrated in the first five minutes. A tool that requires a running
Postgres to show you what it does will not be tried.

**SQLite everywhere, including the relay.** Rejected because the relay's access
pattern — many writers, concurrent sessions, long-lived WebSocket connections — is
exactly what SQLite is worst at. WAL helps readers but there is still one writer,
and a relay with one writer is a relay that serialises every team member's session
behind every other's.

**An embedded key-value store.** Rejected because the queries are relational. "All
events for this session ordered by sequence, filtered by type, joined to the tasks
they reference" is a query, not a key lookup, and reimplementing it over a KV store
means reimplementing a query planner badly.

## Consequences

**Accepted costs:**

- **Two database paths to keep working.** A schema change has to be valid on both.
  The mitigation is that SQLModel's type system makes most portability problems
  compile-time-ish, and CI runs the integration suite against both.
- **SQLite PRAGMAs need explicit setting.** SQLite defaults are wrong for this
  workload in three ways, each handled with a comment at the site: `foreign_keys`
  is off by default, the default journal mode blocks readers during writes, and
  the default busy timeout is zero — meaning a concurrent write fails immediately
  instead of waiting.
- **Async SQLite has caveats.** `aiosqlite` runs the driver in a thread, so
  SQLite's serialised writer is still serialised. Fine for a daemon, worth knowing
  before someone assumes async means parallel writes.
- **The relay is not a mirror of the local database.** It has its own tenancy model
  and its own retention. Reconciling a relay's history with a daemon's local log is
  not supported, and pretending otherwise would create two sources of truth — see
  [0004](0004-append-only-bus-log.md).

**Benefits:**

- `burrow init` followed by `burrow session start` works with no infrastructure.
- The local database is one file in `.openburrow/`, which makes it trivial to
  inspect, back up, or attach to a bug report. `Database.backup()` uses SQLite's
  online backup API rather than copying the file, because copying a WAL-mode
  database without its `-wal` sidecar produces a database that is missing the most
  recent writes.
- The same models serve both, so a query written against SQLite is a query against
  Postgres.
