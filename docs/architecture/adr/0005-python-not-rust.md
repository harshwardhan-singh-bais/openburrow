# 0005 — Python for the core, not Rust or Go

**Status:** Accepted

## Context

OpenBurrow is a daemon that supervises subprocesses, watches files, speaks HTTP
and SSE, and holds a database. That workload profile is a reasonable fit for Rust or
Go, and both would ship as a single static binary — a real distribution advantage
over anything interpreted.

The counter-argument is specific rather than general: **A2A's reference SDK and
MCP's reference SDK are both Python-first.** Adopting those protocols and then
re-implementing their SDKs in another language means owning protocol conformance
forever, in a language where the upstream tests cannot be run.

## Decision

Python 3.12+ for everything except the frontend. `uv` for environment and package
management, including the `uvx`/`uv tool install` distribution path.

## Rejected

**Rust.** Rejected on the SDK argument. The performance profile does not demand it
— the daemon's work is waiting on subprocesses and sockets, not computing — and the
cost is owning A2A and MCP conformance independently. That cost is permanent and
grows with every spec revision.

**Go.** Same SDK argument, plus a weaker fit for the model layer: Pydantic is doing
real work here (validation, serialisation, and the `content_hash` computed field
that gives the bus log its idempotency), and the Go equivalents would mean writing
that machinery by hand.

**Python for the CLI, Rust for the daemon.** Rejected as the worst of both. It
splits the protocol code across two languages, which means either duplicating the
A2A implementation or putting a process boundary in the middle of every lifecycle
transition.

**A Node/TypeScript core with a Python sidecar for the SDKs.** Rejected because the
sidecar boundary would land exactly where the hot path is — every A2A call, every
governance check — and the failure modes of a sidecar (partial writes, restart
races, version skew) are the failure modes this system can least afford.

## Consequences

**Accepted costs — and this one is real:**

- **No single static binary.** This is the honest downside and it should be stated
  plainly rather than buried. `uv tool install openburrow` requires `uv`, and `uv`
  requires a download. For an air-gapped deployment that is a genuine obstacle.
  Mitigation: PyInstaller and Nuitka builds are on the roadmap as a documented
  fallback, and `docker/` provides an image for environments where a container is
  easier to ship than a binary.
- **Startup latency.** Importing Pydantic, SQLModel, and LiteLLM takes real time.
  Mitigated by deferring the two expensive imports (`openburrow.daemon` inside the
  daemon commands, `textual` inside `burrow tui`), which keeps `burrow --help`
  fast, and by making the daemon long-lived so the CLI's import cost is per
  invocation rather than per operation.
- **Type checking is opt-in enforcement.** `mypy --strict` is configured and run in
  CI, but it is a checker, not a compiler. The discipline has to be maintained.

**Benefits that made the trade worth it:**

- The reference SDKs are the implementation. "A2A-conformant" is a claim about
  someone else's tested code, not about ours.
- One language across the domain models, the daemon, the CLI, and the protocol
  layers. The same Pydantic model is validated at the boundary, persisted by
  SQLModel, and serialised to the wire — one declaration, three jobs.
- `uv` is genuinely excellent. An 11-package workspace with one lockfile, fast
  resolution, and `uvx` for the zero-install case covers most of what a static
  binary would have provided.
