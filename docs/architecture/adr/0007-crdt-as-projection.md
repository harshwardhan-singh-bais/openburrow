# 0007 — The CRDT is a projection, not a record

**Status:** Accepted

## Context

The plan and the Brain are shared artifacts. A human in the web dashboard should be
able to reorder plan steps, and a lane should see that immediately. Two users
editing the same plan offline should not have one silently overwrite the other.

A CRDT solves exactly this: concurrent edits merge without a central
coordinator, and the browser already speaks Yjs. The question is what the CRDT
*is* — a cache of the database, a peer of it, or the authority.

## Decision

The CRDT is a **projection**. The append-only bus log and the SQLite database
remain authoritative. The document in `openburrow-brain` is rebuilt from them when
needed, and if the two disagree, the log wins.

`BrainDoc.rebuild()` is a supported operation, not a repair hack.

## Rejected

**CRDT as the source of truth, database as a cache.** This is the design that makes
the merge semantics shine, and it was rejected for one reason: a governance
decision has to be justifiable to someone who was not there. "The CRDT said so" is
not an answer an auditor can check. A CRDT's state is the product of a merge
history that no single participant necessarily observed in full, which makes it an
excellent collaboration substrate and a poor evidence record.

**No CRDT — poll the daemon and let the server resolve conflicts.** Rejected
because the plan is edited by humans as well as lanes, and last-write-wins on a
plan step means a human's reordering can vanish because a lane touched the same
step. The CRDT's actual value here is not offline support; it is that concurrent
edits to *different fields of the same step* both survive.

**Automerge instead of Yjs.** Rejected after the frontend was chosen. Automerge is
arguably a better fit for a document with rich history, but its JavaScript story is
less mature than Yjs's, and a CRDT whose client library is awkward in the browser
means writing a translation layer — which is where the bugs would live. `pycrdt`
gives Python a Yjs-compatible document, so the update bytes go straight into
`Y.applyUpdate` on the other side.

## Consequences

**Accepted costs:**

- **No offline-first editing of the plan.** A browser that has never loaded the
  document cannot start editing it offline, because the document is built from a
  log it has not seen. This is a real limitation and it is the price of the
  projection model.
- **Two representations of the plan.** `Plan`/`PlanStep` in the core models and the
  flattened projection in the CRDT. They can drift, and the mitigation is that
  `rebuild()` makes drift recoverable rather than permanent.
- **The projection is lossy.** Embeddings are dropped, timestamps are stringified,
  and only the fields the viewer renders are carried. Shipping a float vector to a
  browser to render a bulleted list would be absurd, but the lossiness does mean
  the document cannot be used to reconstruct the database.
- **`pycrdt` is an optional dependency.** `BrainDoc` raises `CrdtUnavailableError`
  with a fix-it hint when it is missing. A session with no web frontend is a
  perfectly normal session, so the rest of the Brain must not depend on it.

**Benefits:**

- A malicious or buggy browser client can make its own view wrong and cannot make
  the system act on it. The daemon's decision path never reads the document.
- Rebuilding is cheap and always correct, so there is no reconciliation logic to get
  wrong. `rebuild()` deletes and repopulates rather than merging.
- The mutation methods return whether anything actually changed, which is what
  makes replay idempotent — applying the same bus event twice is a no-op, and
  replay applies events twice by design.
