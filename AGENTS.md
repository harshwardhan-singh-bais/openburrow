# AGENTS.md

Conventions for AI agents working in this repository. Read this before making
changes.

This file is also a demonstration: OpenBurrow reads `AGENTS.md` into its Brain on
`burrow init`, and exports back to a fenced block at the bottom. Everything above that
block is hand-written and never touched by a tool.

## What this project is

A multi-harness agent collaboration platform. Lanes (each running a coding agent in
its own git worktree) talk to each other over A2A, use tools through MCP, negotiate
with ACP performatives, and are governed by a delegation ledger that A2A, MCP, and ACP
do not provide.

## Repository shape

```
packages/           # 11 uv workspace members
  openburrow-core/      models, config, SQLite, the bus log
  openburrow-a2a/       Agent Cards, lifecycle, JSON-RPC/SSE, peer client
  openburrow-acp/       performatives, negotiation driver
  openburrow-adapters/  the ten harness adapters + the protocol
  openburrow-governance/ delegation ledger, detectors, audit
  openburrow-brain/     anchored knowledge, lessons, AGENTS.md, CRDT
  openburrow-radar/     intent extraction, conflict prediction
  openburrow-reel/      casts, causal timeline, export, share links
  openburrow-daemon/    asyncio daemon: IPC, bus, filewatch, sessions
  openburrow-cli/       burrow CLI and Textual TUI
  openburrow-relay/     FastAPI + WebSocket + Postgres (not yet built)
apps/web/           # Next.js frontend (not yet built)
docs/               # architecture, protocols, governance, roadmap, guides
scripts/            # smoke_test.py, probe_lane_server.py
```

## Commands

```bash
uv sync                                   # install everything
uv run python scripts/smoke_test.py       # end-to-end check — run this first
uv run pytest                             # tests
uv run pytest -m governance               # the governance invariants
uv run ruff check . --fix && uv run ruff format .
uv run mypy packages
./.venv/Scripts/burrow.exe --help         # Windows
uv run burrow --help                      # POSIX
```

`scripts/smoke_test.py` must print `3/3 suites passed`. If it does not, nothing else
matters.

## Rules that are not negotiable

These exist because breaking each one caused a real problem. A change that violates
one will be rejected regardless of quality elsewhere.

1. **Persist before you publish.** Append to the bus log first, fan out second.
   Reversing it makes a crash produce a subscriber that acted on an event the log does
   not contain, and the log is the only recovery mechanism.
2. **A state transition and its log entry are one operation.** Never mutate lifecycle
   state without appending the event.
3. **Governance failures raise; they do not trim.** If a delegation requests more than
   the delegator holds, raise `SilentAuthorityCreepError`. Trimming destroys the signal
   that something upstream is wrong.
4. **Detection flags; enforcement refuses.** Opposite failure modes, separate modules.
   Do not merge them.
5. **`build_spawn_spec` stays pure.** No writes, no subprocesses. The policy gate
   inspects it before execution.
6. **If you cannot tell, say so.** Return `{}` or `UNKNOWN`, never a plausible
   estimate. A number wrong in a plausible direction is worse than a missing one.
7. **Docstrings state the tradeoff.** What you rejected and why, not what the module
   contains.
8. **Comments explain why.** The reason the code looks unusual is the thing that stops
   someone simplifying it later.

## Things that will surprise you

**Two package pins are load-bearing.** `uvicorn[standard]` is required — without the
`[standard]` extra, uvicorn uses a pure-Python HTTP parser with a keep-alive bug that
presents as route-matching 404s. `a2a-sdk` is the reference implementation of the
protocol we claim to speak. Both have comments at their site; read them before
changing them.

**`uvicorn[standard]` and starlette are NOT version-pinned on purpose.** An earlier
attempt to fix the 404s with upper bounds was based on a wrong diagnosis and has been
removed. The bug was the HTTP parser, not the framework version.

**`seq` is an integer, not a ULID.** ULIDs sort at millisecond granularity, which is
too coarse for a bus that emits several events in one millisecond. ULIDs are used for
event *identity*; `seq` is for order.

**Claims are advisory.** They are not locks and they do not prevent writes. This is
deliberate — hard locks deadlock on crash. Do not "fix" it by adding locking.

**The CRDT is a projection, not a record.** `BrainDoc.rebuild()` from the bus log is
supported and correct. Nothing in the daemon's decision path reads it.

## Style

- Python 3.12+, `from __future__ import annotations` in every module.
- Line length 100. Ruff with `S`, `ASYNC`, `PTH`, `PL`, `T20`. `T20` bans `print`
  outside `scripts/` — output goes through `output.emit()` so `--json` cannot drift.
- mypy strict. `# type: ignore` needs a reason.
- Conventional commits scoped by package: `fix(daemon): flush the bus before closing
  the socket`. The body says **why**, not what.

## Before you finish

- `uv run python scripts/smoke_test.py` prints `3/3 suites passed`
- `uv run pytest` passes
- `uv run ruff check .` and `uv run mypy packages` are clean
- If you changed governance or the authority model, the governance tests still hold
- If you added a feature, it has a way to verify it works
- If you made an architectural decision, it has an ADR in `docs/architecture/adr/`
- If you added, removed, or reordered roadmap items, `docs/roadmap/README.md` is updated

<!-- openburrow:brain:begin -->

<!-- Generated by OpenBurrow. Edit outside these markers. -->

<!-- openburrow:brain:end -->
