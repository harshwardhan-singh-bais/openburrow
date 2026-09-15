# Contributing

The full guide is at [../../CONTRIBUTING.md](../../CONTRIBUTING.md). This page is the short
version for the two things people look up most.

## Setup and the one check that matters

```bash
uv sync
uv run python scripts/smoke_test.py     # must print: 3/3 suites passed
```

That script stands up two real A2A servers on ephemeral ports, fetches an Agent Card
over HTTP, delivers a JSON-RPC message, runs a full ACP negotiation, and asserts the
governance invariants. It is faster than the test suite and it covers the seams
between packages, which unit tests deliberately do not. If it does not pass, nothing
else matters.

## The eight rules

Each exists because breaking it caused a real problem.

1. **Persist before you publish.** Append to the bus log first, fan out second.
2. **A state transition and its log entry are one operation.**
3. **Governance failures raise; they do not trim.** `SilentAuthorityCreepError` is the
   signal that something upstream is wrong.
4. **Detection flags; enforcement refuses.** Opposite failure modes, separate modules.
5. **`build_spawn_spec` stays pure.** The policy gate inspects it before execution.
6. **If you cannot tell, say so.** `{}` or `UNKNOWN`, never a plausible estimate.
7. **Docstrings state the tradeoff.** What you rejected and why.
8. **Comments explain why.** Not what.

## Where things live

| You want to change | Look in |
|---|---|
| Domain models, config, the bus log | `packages/openburrow-core/` |
| A2A cards, lifecycle, transport | `packages/openburrow-a2a/` |
| Negotiation semantics | `packages/openburrow-acp/` |
| A harness | `packages/openburrow-adapters/` — see [../adapters/authoring.md](../adapters/authoring.md) |
| The authority model | `packages/openburrow-governance/` — read [../governance/model.md](../governance/model.md) first |
| Memory and lessons | `packages/openburrow-brain/` |
| Conflict prediction | `packages/openburrow-radar/` |
| Replays and sharing | `packages/openburrow-reel/` |
| The daemon | `packages/openburrow-daemon/` |
| The CLI | `packages/openburrow-cli/` |

## Decisions and their reasons

Before proposing a change to something structural, check
[../architecture/adr/](../architecture/adr/). Most of the surprising choices in this
codebase were deliberate, and the ADR explains what was rejected and why. If your
change is genuinely better, the ADR is where you make that argument — and where you
record the new decision once it is made.

The roadmap is also worth checking: [../roadmap/README.md](../roadmap/README.md) has 22
stages in dependency order, and the thing you want probably has a place in the
sequence already.
