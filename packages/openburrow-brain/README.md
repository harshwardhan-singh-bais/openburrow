# openburrow-brain

Durable knowledge, perishable lessons, AGENTS.md interop, and a CRDT projection.

## Two kinds of memory, kept separate

They have different lifetimes and different failure modes, and merging them produces
something that is bad at both.

| | Brain entries | Lessons |
|---|---|---|
| Answers | "what is true about this repo" | "what should I do differently" |
| Lifetime | until the code changes | until the cause goes away |
| Bounded by | anchoring | TTL and hit rate |
| Trusted on | corroboration | first sight, within a session |

A Brain entry about how the auth middleware reads tokens is still useful next quarter.
A lesson about the CI runner being slow on Tuesdays is noise by Friday.

## Anchoring makes staleness answerable

Every durable entry records the **file** and **commit** it was written against. A sweep
asks git whether that file has changed.

```
fresh    the anchored path has not changed; the entry is probably still true
stale    the path has changed; the entry's evidence has expired
unknown  we cannot tell — no anchor, no git, or the commit is not in this clone
```

**`unknown` is deliberately not folded into `fresh`.** An entry we cannot verify is an
entry we cannot vouch for, and pretending otherwise is exactly the failure mode
anchoring exists to prevent.

The alternative — storing a fact with a timestamp and hoping — means that six weeks
later nobody can tell whether it is still true, so it either gets trusted past its
expiry (ships a bug) or ignored (makes the memory layer worthless because nobody
believes it).

`path_changed_since` uses `--follow`, so a renamed file still counts as changed.
Without it a rename looks like "no changes to this path", which would keep an entry
fresh after the file moved — precisely the case where a stale entry is most dangerous,
because the code it describes is now somewhere else.

## Corroboration is asymmetric

**Session-scoped**: trusted on first sight. The point is that lane A learns something
at 10:04 and lane B stops making the same mistake at 10:05. Requiring a second witness
would make it too slow to be useful, and the blast radius of a wrong session lesson is
one session.

**Repository-scoped**: needs a second independent witness. It outlives the session and
will be read by people who were not there to see where it came from. A single harness's
confident assertion about project conventions is exactly the kind of thing that becomes
folklore.

**Corroboration from the same lane does not count.** A harness repeating itself is not
a second opinion, and counting it would let one confused agent manufacture consensus on
its own.

The asymmetry is not a heuristic bolted on. It falls out of asking "what is the cost of
being wrong, and who pays it?" — the same question the governance layer asks about
authority, applied here to belief.

## Lessons are perishable by design

**Expiry is a feature.** A lesson that outlives its cause is worse than no lesson,
because it teaches an agent to work around a problem that no longer exists.

**Hit rate drives eviction.** A lesson injected twenty times that never changed
anyone's behaviour is not a lesson; it is a paragraph. `evict_noise()` retires those
once there are enough samples to be confident.

The threshold is a hit *rate* with a minimum sample count, not an absolute count.
Retiring after one unhelpful injection would discard a good lesson that simply did not
apply; keeping on a low rate forever would let the prompt fill with advice nobody
follows.

Ranking puts hit rate first, ahead of confidence. Hit rate is the only signal that
comes from *outcomes* — confidence is what we believed when we wrote the lesson down,
and a lesson can be confidently written and consistently useless.

## The lesson path is defended twice

A lesson is a prompt fragment injected into every lane's context, which makes it the
highest-value injection target in the system: a poisoned lesson affects every lane that
ever loads it, and it persists across sessions.

So `openburrow-governance`'s `detect_poisoned_lesson` runs **independently** of this
package's classifier. They share no code, no thresholds, and no configuration. A single
compromised component does not defeat both checks.

The classifier itself is heuristic-first and LLM-optional, in that order. The heuristic
runs on every message and costs nothing, so the common case — a lane explicitly stating
a root cause — is caught without a model call. A failed refinement costs precision, not
coverage.

## AGENTS.md is not ours

It is a convention Claude Code, Codex, Cursor, Crush and others already read, and the
fastest way to make OpenBurrow useless would be to invent a competing file.

So we read it (anything a human wrote enters the Brain as human-promoted, the highest
confidence the store hands out), and we write to a **fenced block** between two HTML
comment markers. Everything outside is left byte-for-byte alone, including trailing
whitespace — a sync that also reformats shows up as a huge diff and gets reverted.

The markers are HTML comments specifically because every Markdown renderer hides them,
so the managed block is invisible in a GitHub preview.

Bullets are the unit of extraction, not sections. A "## Conventions" section typically
holds several independent rules, and collapsing them into one entry would make
staleness useless — one rule going out of date would invalidate the whole section.

## The CRDT is a projection, not a record

`BrainDoc` exists so the web frontend can show the plan and the Brain with live sync.
It is **not** authoritative: the append-only bus log is, and `rebuild()` from the log
is a supported operation rather than a repair hack.

The tempting alternative — CRDT as source of truth — gives lovely merge semantics and a
second source of truth. But a governance decision has to be justifiable to someone who
was not there, and "the CRDT said so" is not an answer an auditor can check. See
[ADR 0007](../../../docs/architecture/adr/0007-crdt-as-projection.md).

Every mutation returns whether anything actually changed, which is what makes replay
idempotent — applying the same bus event twice is a no-op, and replay applies events
twice by design.

`pycrdt` is optional. A session with no web frontend is a perfectly normal session, so
`CrdtUnavailableError` carries a fix-it hint and the rest of the Brain works without it.

## Documentation

- [ADR 0007](../../../docs/architecture/adr/0007-crdt-as-projection.md) — the CRDT decision
- [docs/governance/model.md](../../../docs/governance/model.md) — the reasoning the corroboration asymmetry mirrors
