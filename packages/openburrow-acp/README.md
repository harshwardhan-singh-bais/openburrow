# openburrow-acp

Negotiation semantics. Six performatives, a legality graph, and a driver that escalates
rather than repairing.

## Why this package exists

Two lanes want the same file. A reviewer disagrees with an implementer about whether a
signature can change. Each of those is a disagreement, and a disagreement between
agents needs a *shape*.

The alternative — agents sending each other polite prose — has no outcome a machine can
act on, no record a human can follow, and no way to detect that it went nowhere. ACP
performatives (IBM Research, derived from FIPA-ACL) provide that shape.

## The performatives

| Performative | Meaning | Terminal |
|---|---|---|
| `propose` | I want X. Here is my offer. | no |
| `counter` | I want X, modified. | no |
| `accept` | I agree to your last offer. | **yes** |
| `reject` | I refuse your last offer. | no |
| `inform` | Here is a fact you need. | no |
| `withdraw` | I am abandoning this. | **yes** |

Six, deliberately. More would need a semantics document nobody reads; fewer would force
unrelated situations into the same form.

## The legality graph is data

`PERFORMATIVE_REPLIES` maps each performative to the set of legal replies.
`validate_reply()` raises `ACPNegotiationError` on an illegal sequence.

It is a dict rather than a chain of `if` statements so it can be read, tested
exhaustively, and rendered as documentation. You cannot `accept` a `reject`, and an
exchange cannot open with anything but `propose` or `inform`.

## An illegal move escalates. It is never repaired.

The most important decision in this package, and worth stating with its rejected
alternative.

The tempting behaviour is to coerce: a `counter` arriving after an `accept` could be
treated as a new `propose`, and the exchange would continue. It would make the driver
more robust.

**It would also be wrong.** An illegal performative sequence means a peer's state
machine disagrees with ours. That is a real bug, and it is almost always in a harness
adapter's translation layer. Silently repairing the sequence hides the bug, and it then
manifests later as a negotiation that "resolved" without either side agreeing.

So `NegotiationDriver.run()` escalates. The cost is a noisy failure; the alternative is
a quiet one, and quiet failures in a coordination protocol are how you get two lanes
confidently doing incompatible things.

## Termination conditions

`run()` returns on the first of:

| Condition | Outcome |
|---|---|
| `accept` | `AGREED` |
| `withdraw` | `ABANDONED` |
| `reject` past `escalate_after_rejects` | `ESCALATED` |
| Two identical positions in a row | `ESCALATED` |
| `max_exchanges` reached | `ESCALATED` |
| Illegal performative | `ESCALATED` |
| No engagement from the responder | `ESCALATED` |

`max_exchanges` is a hard cap rather than a timeout because the failure mode of a long
negotiation is not slowness — it is two agents generating increasingly elaborate
positions.

Loop detection compares the last two moves against the two before them. Detecting a
*repeated position* rather than counting messages is what makes it work: a negotiation
can legitimately take four exchanges while making progress, and a naive round limit
would kill it.

## Positions are declared, not inferred

`NegotiationPosition` is a field on each move, not something derived from the
transcript. Inference would be cheaper and would mean a negotiation's outcome depended
on how a reader interpreted it — which is not a property a decision record should have.

## Priorities are derived

`build_performative_message()` computes priority from the performative; the caller
cannot pass one in. A `reject` is urgent by nature and an `inform` usually is not, and
letting a caller set it means a caller will get it wrong — a `reject` delivered at low
priority sits in a queue while the other lane keeps building on a rejected premise.

## Negotiation is not mediation

The driver does not decide anything. It enforces the shape of the exchange and records
the outcome. There is no arbitration logic and deliberately so: a protocol that decided
whose position was better would be making a technical judgement with less context than
either participant, and the decision would be unattributable.

Escalation is the honest outcome of an unresolved disagreement.

## Documentation

- [docs/protocols/acp.md](../../../docs/protocols/acp.md) — the full binding, including
  the metrics that tell you whether negotiation is worth its cost
