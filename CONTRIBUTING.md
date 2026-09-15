# Contributing

Thanks for considering it. This document covers the setup and, more importantly, the
rules that are not negotiable — knowing those up front saves a review round.

## Setup

```bash
git clone https://github.com/<you>/openburrow && cd openburrow
uv sync
uv run python scripts/smoke_test.py
```

`uv sync` creates `.venv` and installs all 11 workspace packages in editable mode.
Nothing else is required — no database, no services, no harness. The mock adapter
means the whole lifecycle is exercisable with no real agent installed.

## Before you open a pull request

```bash
uv run ruff check . --fix
uv run ruff format .
uv run mypy packages
uv run pytest
uv run python scripts/smoke_test.py
```

The smoke test is the important one. It stands up two real A2A servers on ephemeral
ports, fetches an Agent Card over HTTP, delivers a JSON-RPC message, runs a full ACP
negotiation, and asserts the governance invariants. If it does not print
`3/3 suites passed`, the change is not ready regardless of what the unit tests say.

## The rules that are not negotiable

These are not style preferences. Each one exists because breaking it caused a real
problem, and a pull request that violates one will be asked to change regardless of
how good the rest of it is.

### 1. Persist before you publish

Anything that emits a bus event appends to the log **first**, then fans out to
subscribers.

```python
event = await self.log.append(...)  # durable first
self._fanout(event)  # subscribers second
```

Reversing this means a crash between the two steps leaves a subscriber that acted on
an event the log does not contain. The entire recovery story is "replay the log", so
that state is unrecoverable. See
[ADR 0004](docs/architecture/adr/0004-append-only-bus-log.md).

### 2. A state transition and its log entry are one operation

Never mutate a model's lifecycle state without appending the corresponding event. If
you find yourself wanting to, the operation you actually want is probably two
operations and the design is wrong.

### 3. Governance failures raise; they do not trim

If a delegation requests more authority than the delegator holds, raise
`SilentAuthorityCreepError`. Do not intersect the scope and proceed.

Trimming looks helpful and destroys the signal. An escalation attempt is the most
interesting thing that can happen in this system — it means either an agent is
confused or something upstream is compromised. See
[docs/governance/model.md](docs/governance/model.md).

### 4. Detection does not block; enforcement does not guess

`detectors.py` flags. `ledger.py` refuses. They have opposite failure modes on
purpose, and merging them means tuning for one and getting the other. See
[ADR 0009](docs/architecture/adr/0009-detection-vs-enforcement.md).

### 5. `build_spawn_spec` stays pure

No filesystem writes, no subprocesses, no side effects of any kind. The policy gate
calls it to inspect a command before execution. An impure implementation cannot be
inspected safely, which makes the gate advisory.

### 6. If you cannot tell, say you cannot tell

Never return a plausible default where the honest answer is "unknown". `parse_usage`
returns `{}`, not an estimate. `AnchorChecker` returns `UNKNOWN`, not `FRESH`. The
conflict judge distinguishes "no conflict" from "could not tell".

A number that is wrong in a plausible direction is worse than a missing number,
because nobody goes looking for it.

### 7. Docstrings state the tradeoff

Every module docstring explains what was **rejected** and why, not what the module
contains. A docstring that lists methods is a table of contents. The rejected
alternatives are the design.

### 8. Comments explain why

`# increment the counter` is noise. `# ULIDs sort at millisecond granularity, which
is not fine-grained enough for a bus sequence` is the reason the code looks unusual,
and it is what stops someone simplifying it later.

## Adding a feature

1. **Find its stage.** The roadmap has 22 stages and 258 items in dependency order.
   The thing you want probably has a place in the sequence, and knowing which stage it
   belongs to tells you what it may depend on. See
   [docs/roadmap/README.md](docs/roadmap/README.md).
2. **Write the module docstring first.** If you cannot state what you are rejecting,
   you have not decided what you are building.
3. **Add a verification step.** Every stage in the roadmap has something runnable.
   A feature with no way to check it works is a feature nobody will trust.
4. **Update the roadmap** if you add, remove, or reorder items.

## Adding an adapter

See [docs/adapters/authoring.md](docs/adapters/authoring.md). Short version: extend
`GenericCliAdapter`, keep `build_spawn_spec` pure, report capability declarations
honestly including the degraded case, and never estimate usage.

## Adding an ADR

Architecture decisions get a record in `docs/architecture/adr/`. The format is four
sections — Context, Decision, Rejected, Consequences — and the **Consequences section
must include the parts you do not like.** A record that lists only benefits will be
trusted until the first problem and then discarded entirely.

Number them sequentially. Add a row to the index table.

## Code style

Configured in `pyproject.toml`. Notable:

- **Ruff** with a wide rule selection including `S` (bandit), `ASYNC`, `PTH`, and
  `PL`. `T20` bans `print` outside `scripts/` — output goes through
  `output.emit()` so `--json` cannot drift from human output.
- **mypy strict.** Not negotiable, and `# type: ignore` needs a reason.
- **Line length 100.**
- **`from __future__ import annotations`** in every module.

## Tests

Markers: `unit`, `integration`, `e2e`, `chaos`, `governance`.

```bash
uv run pytest -m unit                     # fast
uv run pytest -m governance               # the invariants
uv run pytest -m "not e2e"                # skip anything needing a real harness
```

**Governance tests are mandatory for any change to `governance/` or the authority
model.** The four invariants have tests that fail if they stop holding, and a change
that removes one of those tests is a change that removes the guarantee.

Chaos tests use the mock adapter's scripting (`hang`, `crash`, `rate_limit`,
`malformed`). They are the only way the daemon's failure paths get exercised, so a new
failure path should come with a chaos case.

## Reporting a bug

Include:

- `burrow doctor --json` output
- `burrow version` output
- The smallest sequence of commands that reproduces it
- If it is protocol-related, `scripts/probe_lane_server.py` output — it prints the
  route table, every endpoint's status and body, and the ASGI scope uvicorn actually
  delivered. That last part is how a keep-alive parser bug gets told apart from a
  routing bug in under a minute, and it has saved hours.

For security issues, do not open a public issue. See [SECURITY.md](SECURITY.md).

## Commit messages

Conventional commits, scoped by package:

```
feat(a2a): add push notification config support
fix(daemon): flush the bus before closing the IPC socket
docs(governance): explain why narrowing raises instead of trimming
test(radar): cover the judge-unavailable path
```

The body should say **why**, not what. The diff says what.

## Code of conduct

Be decent. Assume good faith. Critique the code, not the person. If a discussion is
going badly, take a break and come back to it.
