# 0001 — Speak A2A rather than a bespoke bus format

**Status:** Accepted

## Context

Multiple coding agents need to exchange messages, delegate tasks, and report
status to each other. The obvious implementation is a message envelope of our own
design: a `BusMessage` with `sender`, `recipients`, `subject`, `body`, and a few
flags. It would take a day to design and a week to implement, and it would be
exactly as good as we made it.

Meanwhile, the ecosystem is standardising. A2A (Agent2Agent) reached v1.0 under the
Linux Foundation with a published spec, a reference SDK, an Agent Card discovery
mechanism, a JSON-RPC transport, SSE streaming, and an eight-state task lifecycle.
MCP covers tool access. ACP covers negotiation performatives.

The question is not "is A2A perfect". It is "do we want to own protocol
conformance, or do we want to own the thing A2A does not do".

## Decision

The bus speaks A2A. Every lane runs a real A2A server exposing
`/.well-known/agent-card.json`, JSON-RPC at `/`, SSE at `/stream`, and a health
endpoint. Task delegation uses A2A's `message/send` and the standard eight-state
lifecycle.

OpenBurrow's own additions are carried as `openburrow:` prefixed keys in the Agent
Card's `metadata`, so a conformant A2A consumer that knows nothing about us can
still read the card and ignore the extensions.

## Rejected

**A bespoke envelope.** Rejected because it makes "interoperable" a claim about our
own code. The day someone wants to point Claude Code's native A2A support at an
OpenBurrow lane, a bespoke format means writing a bridge; with A2A it already
works. The cost of a bespoke format is not the format — it is every integration
that can never happen.

**A central broker that routes all messages.** Rejected because A2A is explicitly
peer-to-peer, and a broker would make the bus a single point of failure and a
throughput ceiling. The bus in OpenBurrow is a *log*, not a router: lanes talk to
each other directly.

**Adopting A2A but forking the lifecycle.** Rejected. The eight states exist and are
adequate. A private ninth state would mean our cards advertise something no
conformant peer can interpret.

## Consequences

**Accepted costs:**

- We inherit protocol churn. When A2A revises the spec, we follow. This is a real
  ongoing cost and the reason `A2A_PROTOCOL_VERSION` is a single constant rather
  than a scattering of literals.
- Some things we want are not expressible. A lane's `stale` status has no A2A task
  equivalent, so `LaneStatus.to_task_state()` projects it onto the nearest task
  state — a lossy mapping we maintain deliberately rather than by accident.
- `a2a-sdk` is a hard dependency. The reference implementation is the only honest
  way to claim conformance.

**Benefits that were not obvious up front:**

- Agent Cards turned out to be the right place for authority scope. Because every
  lane already publishes a card, "what is this lane allowed to do" is discoverable
  by any peer before it delegates — which is precisely the behaviour a governance
  layer wants and which a bespoke protocol would have had to add later.
- The eight-state lifecycle gave us `rejected` for free. That state is doing real
  work: it distinguishes "the assignee refused this" from "somebody withdrew it",
  which is the only signal that a lane is being asked to do something it considers
  out of scope.
