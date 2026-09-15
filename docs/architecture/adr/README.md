# Architecture decision records

Each record states a decision, the context that forced it, what was rejected, and
what it cost. The cost section is the one that matters — a decision record without
a stated downside is a press release.

| # | Decision | Cost accepted |
|---|---|---|
| [0001](0001-protocol-grounding.md) | Speak A2A rather than a bespoke bus format | Conformance is not ours to define; we inherit protocol churn |
| [0002](0002-mcp-passthrough.md) | Pass MCP through untouched | We cannot enforce tool-level policy |
| [0003](0003-governance-layer.md) | Add a governance layer the protocols do not have | A whole stage of work no protocol requires |
| [0004](0004-append-only-bus-log.md) | Append-only log as the single source of truth | No in-place correction; mistakes are permanent records |
| [0005](0005-python-not-rust.md) | Python for the core | No single static binary |
| [0006](0006-sqlite-and-postgres.md) | SQLite locally, Postgres for the relay | Two database paths to keep working |
| [0007](0007-crdt-as-projection.md) | CRDT as a projection, not a record | No offline-first editing of the plan |
| [0008](0008-claims-not-locks.md) | Claims are advisory, not locks | Two lanes *can* write the same file |
| [0009](0009-detection-vs-enforcement.md) | Separate detection from enforcement | Two modules, two threshold sets, more surface |
| [0010](0010-adapter-protocol.md) | A seven-operation adapter protocol | Harnesses with unusual models fit awkwardly |

## Format

Each record has four sections:

- **Context** — what made this a decision rather than a default.
- **Decision** — what we do, stated as an instruction.
- **Rejected** — the alternatives, and specifically why each was worse. This is the
  section that stops a decision being relitigated every six months.
- **Consequences** — including the parts we do not like. A record that only lists
  benefits will be trusted until the first problem, and then discarded entirely.
