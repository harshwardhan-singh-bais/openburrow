# 0003 — Add the governance layer the protocols do not have

**Status:** Accepted

## Context

A2A, MCP, and ACP each document what they deliberately do not specify, and the
omissions overlap on one point. None of them can express:

- who is allowed to delegate what
- what authority actually transferred in a delegation
- who is accountable when an agent acts on another's behalf
- what must be audited when a call crosses a trust boundary

This is not an oversight in those specs; it is a scoping decision. A2A is a
transport and lifecycle protocol. It will carry a delegation from Agent A to Agent
B and has no opinion on whether A was entitled to delegate it.

But these are exactly the questions that decide whether a system is deployable in
an organisation with auditors. A multi-agent platform that cannot answer "who
authorised this" is a demo.

## Decision

A dedicated governance layer, developed as **Stage 14** of the roadmap, answering
the gap directly. Four invariants:

1. **Authority originates with a human and can only ever narrow.**
2. **Denial beats grant.**
3. **Detection and enforcement are separate components**, because they have
   opposite failure modes.
4. **Defense in depth on the prompt-injection path.**

Every invariant is enforced in code with a test that fails if it stops holding.

## Rejected

**Documenting the gap instead of closing it.** Rejected because a documented gap
that the platform itself does not close is a gap the platform inherits. If
OpenBurrow's answer to "who authorised this" is "see the A2A spec, section on
limitations", then OpenBurrow is a transport wrapper with extra steps.

**Building governance as a plugin or an optional extra.** Rejected. A governance
layer that can be absent is a governance layer that will be absent in the
deployment that needed it. The ledger is a core dependency of the daemon and the
A2A package; there is no configuration that removes it.

**Making governance advisory — flag everything, block nothing.** Rejected because
it collapses the distinction the whole design rests on. There must be a component
that *refuses*, and it must be separate from the component that *suspects*, because
their thresholds want to be tuned in opposite directions.

**Extending the A2A spec with governance fields and calling that the answer.**
Rejected for two reasons: our extensions would not be interpreted by any other
implementation, and the interesting part of governance is not the data model but
the enforcement chain (depth limits, consent to re-delegate, scope intersection,
human anchor). A schema cannot enforce any of that.

## Consequences

**Accepted costs:**

- A whole stage of work that no protocol required. This is the largest single
  investment in the project and it produces no user-visible feature in the happy
  path — a session where nobody tries anything illegitimate looks identical with
  and without it.
- Governance is in the hot path for delegations. Every `authorize()` call does
  four checks and writes an audit record. The cost is milliseconds and a row, and
  it is worth it, but it is not free.
- `Delegation` cannot be constructed without a human. This makes several
  legitimate-looking shortcuts impossible, including "the coordinator delegates to
  itself for a sub-task". That is intentional and it does occasionally mean
  expressing a human decision explicitly where the code would prefer to infer it.

**Benefits:**

- The accountability question has an answer that a non-engineer can follow:
  `burrow audit --delegation <id>` prints the chain, who authorised it, what was
  requested, what was held, and what was refused.
- The refusal path is a first-class outcome. `governance.blocked` events are
  recorded, surfaced in `burrow watch`, and counted in `burrow report` — so a
  governance layer that is doing its job looks like it is doing its job rather
  than like an absence of events.
- The gap is closed for *any* A2A peer, not just OpenBurrow lanes. The ledger's
  invariants apply to a delegation that arrives from outside the burrow, because
  they are enforced on the delegation record rather than on the sender.
