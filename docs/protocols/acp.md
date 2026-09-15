# The ACP binding

Two lanes want the same file. A reviewer disagrees with an implementer about whether
a signature can change. A coordinator wants to split a step that is already owned.

Each of those is a disagreement, and a disagreement between agents needs a shape. The
alternative — agents sending each other polite prose and hoping for the best — has no
outcome a machine can act on, no record a human can follow, and no way to detect that
it went nowhere.

OpenBurrow uses ACP performatives (IBM Research, derived from FIPA-ACL) for that
shape.

## The performative set

Six, deliberately. A larger set would need a semantics document nobody reads; a
smaller one would force unrelated situations into the same form.

| Performative | Meaning | Ends the exchange? |
|---|---|---|
| `propose` | I want X. Here is my offer. | no |
| `counter` | I want X, modified. | no |
| `accept` | I agree to your last offer. | **yes** |
| `reject` | I refuse your last offer. | no (until escalation) |
| `inform` | Here is a fact you need. | no |
| `withdraw` | I am abandoning this. | **yes** |

`TERMINAL_PERFORMATIVES` is `{accept, withdraw}`. `CONTINUING_PERFORMATIVES` is the
rest.

## The legality graph

`PERFORMATIVE_REPLIES` defines what may follow what. `validate_reply(previous, reply)`
raises `ACPNegotiationError` on an illegal sequence.

The rules that matter:

- **An exchange must open with `propose` or `inform`.** You cannot open by accepting
  something nobody offered.
- **`accept` is terminal.** Nothing follows an agreement.
- **`withdraw` is terminal.** Nothing follows an abandonment.
- **You cannot `accept` a `reject`.** The obvious case, and the one that catches
  bugs in adapters that track state loosely.
- **`inform` does not replace a reply.** Sending a fact is not answering the offer;
  the offer is still outstanding.

The graph is a dict of `performative → frozenset of legal replies`. It is data rather
than a chain of `if` statements specifically so it can be read, tested exhaustively,
and rendered as documentation.

## An illegal move escalates. It is never repaired.

This is the most important design decision in the module, and it is worth being
explicit about the alternative.

The tempting behaviour is to coerce: a `counter` arriving after an `accept` could be
treated as a new `propose`, and the exchange could continue. It would make the driver
more robust, and it would be wrong.

**Why it is wrong:** an illegal performative sequence means a peer's state machine
disagrees with ours. That is a real bug, and it is almost always in a harness
adapter's translation layer — the code that turns a harness's output into a
performative. Silently repairing the sequence hides the bug, and the bug then
manifests somewhere else, later, as a negotiation that "resolved" without either side
actually agreeing.

So `NegotiationDriver.run()` escalates on an illegal move. The escalation is recorded,
the negotiation outcome is `ESCALATED`, and a human sees it. The cost is a noisy
failure; the alternative is a quiet one, and quiet failures in a coordination
protocol are how you get two lanes confidently doing incompatible things.

## The exchange

```python
driver = NegotiationDriver(transport, max_exchanges=6, escalate_after_rejects=2)
result = await driver.run(
    initiator=lane_alice,
    responder=lane_bob,
    opening=propose("alice keeps routes.py; bob takes middleware.py"),
)
```

`run()` opens with a `propose` from the initiator, then alternates strictly. Each
turn goes through `validate_reply` before it is accepted.

### Termination conditions

The driver returns on the first of:

| Condition | Outcome | Why |
|---|---|---|
| `accept` | `AGREED` | The intended end. |
| `withdraw` | `ABANDONED` | A lane gave up; not a failure, but not an agreement. |
| `reject` past `escalate_after_rejects` | `ESCALATED` | Two refusals means the lanes disagree about the premise, not the detail. |
| `_is_stuck()` | `ESCALATED` | Loop detection, see below. |
| `max_exchanges` | `ESCALATED` | Hard cap. |
| Illegal performative | `ESCALATED` | See above. |
| No engagement | `ESCALATED` | The responder replied with neither a move nor a refusal. |

`max_exchanges` is a hard cap rather than a timeout because the failure mode of a
long negotiation is not slowness, it is two agents generating increasingly elaborate
positions. Six exchanges is enough for a genuine disagreement to resolve and short
enough that a stuck pair is caught while a human still remembers the context.

### Loop detection

`_is_stuck()` compares the last two moves against the two before them. Two identical
counters in a row means neither side is moving, and a seventh round of the same
exchange will not help.

Detecting a *repeated position* rather than counting messages is what makes this work:
a negotiation can legitimately take four exchanges while making progress, and a naive
round limit would kill it.

### Both sides' stances are explicit

`NegotiationPosition` is a declared field on each move, not something inferred from
the transcript.

Inference would be cheaper and would mean the outcome of a negotiation depended on how
a reader interpreted it. For a decision that two lanes are going to act on, and that
lands in the audit log, "the reader's interpretation" is not an acceptable basis.
Making the stance explicit means a mismatch between a lane's stated position and its
actual behaviour is *detectable*, rather than being absorbed into the reading.

## Priorities come from the performative

```python
def build_performative_message(...) -> BusMessage:
    # priority is derived, never passed in
    priority = MessagePriority.HIGH if performative in TERMINAL_PERFORMATIVES else ...
```

A `reject` is urgent by nature: it means the other lane's current plan does not work.
An `inform` usually is not. Letting the caller pass a priority means a caller will get
it wrong, and getting it wrong is a real bug — a `reject` delivered at low priority
sits in a queue while the other lane keeps building on a rejected premise.

Deriving it removes the possibility.

## Recording

Every move is appended to the bus log with its performative, its position, and the
move index. `NegotiationExchange.add_move()` resolves the exchange automatically on
`accept`, and `transcript()` renders the whole thing as a readable sequence:

```
lane_alice propose → lane_bob counter → lane_alice counter → lane_bob accept
```

`summarise_exchange()` produces the one-line form used in `burrow report` and the
reel timeline.

## Negotiation is not mediation

Worth being clear about what this is not. The driver does not decide anything. It
enforces the shape of the exchange and records the outcome. If the lanes agree, they
agreed; if they do not, the driver escalates to a human.

There is no arbitration logic and there deliberately is not. A protocol that decided
whose position was better would be making a technical judgement with less context than
either participant, and the decision would be unattributable. Escalation is the honest
outcome of an unresolved disagreement.

## Metrics

`SessionMetrics.negotiation_precision` is `collisions_avoided / negotiations_issued`.
It answers the question that decides whether the negotiation machinery is worth its
cost:

> Of the negotiations we opened, how many actually avoided a collision?

A precision near zero means the Radar is crying wolf and the negotiations are
overhead. A precision near one with very few negotiations means the Radar is too
conservative. Neither number is meaningful alone, which is why
`false_positive_negotiations` is tracked separately and `burrow report` prints both.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `OPENBURROW_ACP_MAX_EXCHANGES` | `6` | Hard cap per negotiation. |
| `OPENBURROW_ACP_ESCALATE_AFTER_REJECTS` | `2` | Refusals before a human is asked. |
| `OPENBURROW_ACP_MOVE_TIMEOUT_S` | `120` | A lane that does not reply in time is not negotiating. |
| `OPENBURROW_ACP_AUTO_OPEN` | `true` | Whether a Radar prediction opens a negotiation automatically. |

## Reading the code

| Concern | Module |
|---|---|
| Performatives, the legality graph, message construction | `packages/openburrow-acp/src/openburrow/acp/performatives.py` |
| The driver and its termination conditions | `packages/openburrow-acp/src/openburrow/acp/negotiation.py` |
| The exchange model and its metrics | `packages/openburrow-core/src/openburrow/core/models/message.py` |
