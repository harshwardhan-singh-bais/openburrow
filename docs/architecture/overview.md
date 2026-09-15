# Architecture overview

The components are the easy part. This document leads with the invariants, because
those are what the design actually is — the components are close to obvious once
you know what must never break.

## The invariants

Six rules. Every one has a test that fails if it stops holding, and every one has
a comment at the site that enforces it explaining why it is there.

### 1. The bus log is the only source of truth

`bus_events` is append-only, keyed by a monotonic `seq`, and stores the full
serialised object in a `payload` column so the log is self-contained — you can
read a decision out of the log without joining to any other table.

The publish order is **persist, then publish**, never the reverse:

```python
async def emit(self, ...):
    event = await self.log.append(...)   # durable first
    self._fanout(event)                  # subscribers second
```

If this were reversed, a crash between the two steps would leave a subscriber that
acted on an event which the log does not contain. That is unrecoverable, because
the whole recovery story is "replay the log". Getting the order wrong means the
system cannot be made consistent again after a crash — which is why the comment at
that call site is unusually blunt.

Consequences worth knowing:

- The TUI, `burrow report`, the audit export, the replay, and the metrics all read
  the same log. A replay cannot disagree with what happened, because there is
  nothing for it to disagree *with*.
- The CRDT document in `openburrow-brain` is a **projection**, not a record. If it
  and the log disagree, the log wins and the document is rebuilt. The tempting
  alternative — making the CRDT authoritative — gives you lovely merge semantics
  and a second source of truth, and "the CRDT said so" is not an answer an auditor
  can check.
- `seq` is an integer, not a ULID. ULIDs sort at millisecond granularity, which is
  not fine-grained enough for a bus that can emit several events in the same
  millisecond. Event *ids* are ULIDs; event *order* is `seq`.

### 2. Authority originates with a human and can only narrow

Enforced in `DelegationLedger.authorize`, as a chain of four checks that all have
to pass:

1. **A human anchor is required.** `Delegation` has a validator that refuses to
   construct without an `authorized_by` naming a human. There is no code path that
   creates a delegation without one.
2. **Chain depth is bounded.** Default 3, hard ceiling 10. A ceiling of 10 exists
   because a value above it is a typo, and the error message says so.
3. **Re-delegation requires consent.** If the delegator did not hold
   `may_redelegate`, the sub-delegation is refused even if the scope would be
   narrower.
4. **Scope can only narrow.** The requested scope is intersected with what the
   delegator actually held:

```python
ungranted = [cap for cap in requested if not granted.allows(cap)]
if ungranted:
    raise SilentAuthorityCreepError(...)  # and raise a governance flag
```

That fourth check is the one that catches the interesting attack. A sub-delegation
that asks for *more* than the delegator held is not a bug in the caller; it is
either a confused agent or a prompt injection trying to escalate. Either way it
must fail loudly, which is why it raises rather than trimming the scope silently —
trimming would let the escalation attempt leave no trace.

### 3. Denial beats grant

`AuthorityScope.allows()` consults denials first:

```python
if self._matches(self.denied, capability):
    return False  # a prohibition is absolute
return self._matches(self.granted, capability) or self._wildcard
```

The ordering is the point. If grants were checked first, a wildcard grant
(`fs:*`) would satisfy everything and a specific denial (`fs:write:.env`) could
never take effect. Any system where a broad permission can be used to bypass a
narrow prohibition has a permission model that does not work.

### 4. Detection and enforcement are separate, because they fail in opposite
directions

| | False positive costs | Therefore it must | Lives in |
|---|---|---|---|
| **Enforcement** | a stalled session, a blocked human, lost work | never fire on a legitimate action | `governance/ledger.py` |
| **Detection** | one dismissible flag | be noisy; silence is the failure | `governance/detectors.py` |

Tuning one component for both failure modes is impossible — the settings that make
detection sensitive enough to catch a subtle injection are the settings that make
enforcement unusable. So they are separate modules with separate thresholds, and a
flagged-but-allowed action is a correct outcome rather than a contradiction.

The ledger never guesses. When it cannot decide, it refuses. The detectors never
block. When they are unsure, they flag and let a human look.

### 5. Claims are advisory, not locks

A lane claims a file. The claim is a record in the database and a message on the
bus. It is not a filesystem lock, and nothing prevents a lane from writing to a
file another lane has claimed.

This is deliberate. Hard locks across heterogeneous harnesses deadlock on crash:
a lane that dies holding a lock leaves the file locked forever, and the recovery
path becomes "find the lock file and delete it", which is not a recovery path
anyone should have to run. Enforcement is negotiation — the second lane to want the
file gets told it is claimed, and an ACP exchange settles it. First claim wins, but
"wins" means "has the stronger default position in a negotiation", not "has an
exclusive lock".

The cost is real: two lanes *can* both write the same file if they ignore the
protocol. The mitigation is that the collision becomes visible (the Radar predicts
it, the audit records it) rather than impossible.

### 6. Credential isolation

Each lane gets its own environment slice, built by `HarnessAdapter.base_env()`.
A lane sees its own harness's keys and nothing else. There is no shared "agent
token" that all lanes use, because a shared credential makes every audit record
ambiguous — you cannot tell which lane did the thing.

## Components

### `openburrow-core`

Domain models, configuration, the SQLite layer, and the bus log.

The models are the interesting part, and they are Pydantic models rather than
plain dataclasses because the same objects are persisted, serialised to the wire,
and validated at the boundary. One declaration, three jobs.

Two model-level details that carry a lot of weight:

**`BurrowModel.content_hash`** is a computed field hashing the *semantic* content
of a model — excluding ids and timestamps. It is what lets the bus log reject
duplicate events: replaying a log that was already applied produces identical
hashes and is dropped. Without it, replay would double-apply everything.

**`LaneStatus.to_task_state()`** projects lane status onto the A2A task state
space. A lane has more states than a task does (it can be `stale`, or `starting`,
or `crashed`), and the projection is explicit so the mapping is visible in one
place instead of being re-derived at every call site.

### `openburrow-a2a`

Agent Cards, the task lifecycle manager, JSON-RPC/SSE transport, the peer client,
and the per-lane server.

**Lifecycle:** `TaskLifecycleManager` does two things atomically on every
transition — mutate the model (validating against the transition graph) and append
a bus event. Doing them separately would allow a state change with no log entry,
which breaks invariant 1.

`retry()` creates a **new** task with a `parent_task_id` rather than resurrecting
the terminal one. Terminal states are absorbing; a system where `completed` can
become `working` again has no reliable way to answer "is this task done".

**Card extensions:** OpenBurrow's additions to the Agent Card live under
`openburrow:` prefixed keys in `metadata`, so a conformant A2A consumer that knows
nothing about us can still read the card and ignore the extensions.

### `openburrow-acp`

The performative set and the negotiation driver.

`PERFORMATIVE_REPLIES` is a legality graph. `validate_reply` raises
`ACPNegotiationError` on an illegal sequence. The driver treats an illegal move as
a signal to escalate, not to repair — see
[../protocols/acp.md](../protocols/acp.md) for why.

`NegotiationPosition` makes each side's stance explicit rather than inferring it
from the transcript. Inference would mean a negotiation's outcome depended on how
the reader interpreted it, which is not a property a decision record should have.

### `openburrow-adapters`

Ten harness adapters behind one protocol: five operations (`start`,
`send_prompt`, `read_output`, `stop`, `status`) plus two translation hooks
(`translate_output`, `inject_message`).

Two design points:

**`build_spawn_spec` is pure.** It returns a `SpawnSpec` — the exact command,
arguments, environment, and working directory — without executing anything. The
policy gate calls it to inspect a command *before* it runs. A builder with side
effects could not be inspected, which would make the gate advisory.

**Capability declarations report the truth, including the degraded case.** The
OpenCode adapter has a structured server mode and a PTY fallback. Its
`effective_capabilities()` reports the capabilities of whichever mode is actually
in use, not the union of what the harness can do in principle. Reporting the
optimistic set would make the capability-mismatch detector fire on a lane that
never claimed the capability in the first place — noise that trains people to
ignore the detector.

### `openburrow-governance`

The delegation ledger, the detectors, and the audit log. Covered above.

One more detail: `scan_for_secrets` returns **truncated** matches. A secret scanner
that prints the secret it found has become a leak vector, which is the failure mode
of nearly every naive implementation.

### `openburrow-brain`

Anchored knowledge, lesson propagation, AGENTS.md interop, and the CRDT
projection.

**Anchoring** is what makes staleness answerable. An entry records the file and
commit it was written against. A sweep asks git whether that file has changed. The
three states are `fresh`, `stale`, and `unknown` — and `unknown` is deliberately
not folded into `fresh`, because an entry we cannot verify is an entry we cannot
vouch for.

**Corroboration is asymmetric.** A session-scoped observation is trusted on first
sight (the point is to reach the next lane within seconds; the blast radius is one
session). A repository-scoped entry needs a second independent witness, because it
outlives the session and will be read by people who were not there. Corroboration
from the *same* lane does not count — a harness repeating itself is not a second
opinion.

### `openburrow-radar`

Intent extraction, an optional LLM judge, conflict prediction, and the scan loop.

**Files come from records; descriptions may come from a model.** A model can never
add a file to an intent. The moment it can, the deterministic half of the Radar
stops being deterministic and every claim about "we detected this collision" stops
being auditable.

**Certainty is a first-class field.** `ConflictPrediction.certain` is `True` only
when the conflict rests on records. A shared file is reported regardless of any
confidence threshold, because suppressing a known collision behind a score is how
you get a merge conflict you had already detected.

**The judge's confidence is clamped** to `[0.5, 0.95]`, and it distinguishes "no
conflict" from "could not tell". Collapsing those is the most common way an
LLM-backed component becomes untrustworthy: the model goes down and the system
records "no conflicts found" for the outage window.

### `openburrow-reel`

Casts, the causal timeline, the recorder, the exporter, and share links.

**Two halves, one clock.** Casts are asciinema v2 files, playable in tools people
already have. The timeline is JSONL with a `caused_by` field. Both measure time as
seconds since session start so they can be overlaid on one axis.

**The recorder never interprets.** It observes and writes. The moment it starts
summarising, a reel stops being a record and becomes a document — and a document
can be wrong in ways a record cannot.

**Redact, then sign.** Never the reverse. Sign-then-redact either breaks
verification or, worse, produces a signature that attests to content the recipient
cannot see.

### `openburrow-daemon`

IPC, the event bus, file watching, session/lane supervision, and the control
plane.

**IPC hardening is explicit.** `_harden_socket_permissions` applies mode `0600`
rather than relying on the umask, because a permissive umask would otherwise make
the daemon reachable by every user on the machine.

**Shutdown order is deliberate:** lanes stop first so no new writes arrive, then
the bus flushes, then the IPC socket closes, and only then does the database
close. Reversing any two of those produces either lost events or a client that
hangs on a closed socket.

**The liveness check is a real ping**, not a PID file read. A stale PID file is
exactly the case where the naive check lies — the file exists, the process does
not, and every subsequent command fails confusingly.

### `openburrow-cli`

The `burrow` command surface and the Textual TUI.

**One output funnel.** Commands build a payload and hand it to `output.emit()`,
which decides between human and JSON rendering. That is why `--json` cannot drift
out of sync with the normal output: there is exactly one place where the decision
is made.

**Exit codes are semantic.** A governance refusal and a configuration typo both
exit non-zero, but they are different events — the first is the system working,
the second is the system being set up wrong. Collapsing them into `1` would make a
CI job that asserts "no governance violations occurred" impossible to write.

**The TUI is not the primary surface.** `burrow watch` renders the same data as
plain ANSI, so it works over any terminal and degrades gracefully when piped.

### `openburrow-relay`

FastAPI + WebSocket + Postgres, for teams that want remote members in a session.

The relay is **optional**. A single-developer session never touches it, and the
daemon does not depend on it. When enabled, it requires a JWT secret and refuses to
run in production without one — a relay with a default secret is not a relay with
weak auth, it is a relay with no auth.

## The honesty rule

This is a consistent position across the codebase, and it is worth naming because
it is unusual enough that it looks like inconsistency if you do not see the pattern.

Wherever a component cannot determine something, it says so rather than returning a
plausible default:

| Component | Cannot determine | Returns |
|---|---|---|
| `AnchorChecker` | whether an entry is still true | `AnchorState.UNKNOWN`, never `FRESH` |
| `parse_usage` | token counts for an unparsed harness | `{}`, never an estimate |
| `ConflictJudge` | whether two intents collide | `known=False`, never "no conflict" |
| `RadarStats` | judge coverage when the judge is down | `0.0` coverage, plus `semantic_available=False` |
| `HarnessCapabilities` | a PTY fallback's structured output | the degraded truth, never the optimistic set |

The alternative is always tempting and always worse. An estimated token count makes
a cost report look complete while being wrong; an optimistic capability set makes a
mismatch detector fire on a lane that never claimed the capability; a "no conflict"
from a dead judge makes a coverage metric claim a period it did not cover.

The rule: **if you cannot tell, say you cannot tell, and make sure the reporting
layer carries that through.** A number that is wrong in a plausible direction is
worse than a number that is missing, because nobody goes looking for it.

## Where to go next

- [../governance/model.md](../governance/model.md) — the delegation model in full
- [../protocols/a2a.md](../protocols/a2a.md) — the A2A binding
- [adr/](adr/) — why each significant decision was made, and what it cost
- [../roadmap/README.md](../roadmap/README.md) — what is built and what is next
