# 0002 — Pass MCP through untouched

**Status:** Accepted

## Context

Lanes need tools: file access, shell, search, and whatever MCP servers the project
has configured. The harnesses already know how to talk to MCP — that is what MCP is
for, and several of the harnesses OpenBurrow wraps have native MCP support.

The tempting design is to mediate: have OpenBurrow sit between the harness and its
MCP servers, so that every tool call passes through a policy check. It would give
the governance layer visibility into exactly what each lane is doing at the tool
level, and it would make "deny `fs:write:.env`" enforceable rather than aspirational.

## Decision

MCP is passed through untouched. Each lane's harness connects to MCP servers
directly, using its own configuration. OpenBurrow does not proxy, does not inspect
tool calls, and does not sit in the MCP path.

Governance operates on **delegation and action scope**, not on tool invocations.

## Rejected

**Proxying MCP to enforce per-tool policy.** This was the closest call in the
project, and it was rejected for three reasons that compound:

1. *It breaks the harnesses.* MCP clients are configured per-harness, and several
   support transports and features (sampling, roots, notifications) that a
   naive proxy does not implement. A proxy that works for one harness and breaks
   another is worse than no proxy, because the failure is silent and
   harness-specific.
2. *The policy would be in the wrong place.* "Deny writes to `.env`" is a
   *filesystem* policy. Enforcing it at the MCP layer catches the harnesses that
   route file writes through MCP and misses the ones that shell out to `sed`. A
   policy that is enforced on one of two paths is a policy that produces false
   confidence.
3. *It would make OpenBurrow a single point of failure for every tool call.* Every
   tool call in every lane would depend on the daemon being up and correct. The
   blast radius of a daemon bug becomes "all work stops", and the daemon is the
   least-tested component because it is the hardest to test.

**Mediating only for auditing.** Rejected because a pass-through that records is
still a pass-through that can fail, and the audit value is available more cheaply:
the bus log already records delegation and scope, which is the level at which the
accountability question is actually asked.

## Consequences

**Accepted costs:**

- OpenBurrow cannot enforce tool-level policy. `policy.yaml` can express
  filesystem and command rules that the *adapter* applies before spawning, and the
  daemon can refuse a lane's spawn spec — but a running lane's tool calls are its
  own business. This is a real limitation and it is documented in
  [../../governance/model.md](../../governance/model.md#what-governance-does-not-cover).
- Sandboxing is the harness's job. Codex has `--sandbox`, Claude Code has its own
  permission model, and OpenBurrow layers on top rather than replacing either. A
  lane that runs unsandboxed because its harness defaults to that is a
  configuration problem the operator has to fix.
- The capability-mismatch detector has to rely on declarations rather than
  observation. A lane can use a capability it did not declare, and the detector
  only notices if the harness reports it in its output.

**Benefits:**

- Zero latency and zero failure modes added to the tool path.
- Harnesses keep their native MCP behaviour, including features we would not have
  implemented.
- "MCP is USB-C for tools, A2A is HTTP for agents" stays true. A platform that
  proxies one through the other has added a failure mode to get a feature nobody
  asked for.
