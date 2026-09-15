# 0004 — An append-only bus log as the single source of truth

**Status:** Accepted

## Context

Everything in OpenBurrow needs to know what happened: the TUI, the audit report,
the metrics, the replay, the Radar's intent extraction, the Brain's classifier, and
the daemon's own recovery path after a crash.

The obvious implementation gives each consumer what it needs. The TUI subscribes to
live events. The audit report reads the audit table. The metrics read the metrics
table. The replay reads a recording. Each of those is simple, and together they
produce a system where five components have five different accounts of the same
session.

That divergence is not hypothetical. It is the normal outcome of per-consumer
event handling, and it is invisible until the day two reports disagree and nobody
can say which is right.

## Decision

One append-only table, `bus_events`, is the canonical record. It is keyed by a
monotonic integer `seq`, stores the full serialised event object in a `payload`
column, and is never updated or deleted.

Every other component reads from it. Persist-then-publish, always.

## Rejected

**Per-consumer event streams.** Rejected because it makes divergence a matter of
time rather than a matter of possibility. A replay that reads a recording cannot
disagree with the recording, but it *can* disagree with the audit table — and then
the replay is worthless precisely when you need it, which is during an incident.

**A message broker with at-least-once delivery.** Rejected because it solves the
wrong problem. Delivery guarantees matter when consumers act on messages; here the
consumers *render* them. What matters is that the record is complete and ordered,
which an append-only table gives directly and a broker gives only with more moving
parts.

**Mutable rows with an `updated_at` column.** Rejected. The bus log answers "what
happened", and the answer to that is never "what happened, as later amended". A
governance refusal that was later reversed should read as *a refusal and then a
reversal*, which is two events. Collapsing them into one row that ends up looking
like an approval destroys the only interesting part.

**ULIDs as the primary key.** Rejected, and this one is subtle. ULIDs are
lexicographically sortable and monotonic *within a millisecond*, but a bus that
emits several events in the same millisecond would have ties broken by the random
component — meaning the log's order would not be the order things happened. So
`seq` is an integer primary key and ULIDs are used for event *identity*.

## Consequences

**Accepted costs:**

- **No in-place correction.** A mistake is a permanent record. The remedy is a
  superseding event, not an edit. This is the right trade for an audit log and the
  wrong one for a scratchpad, which is why scratchpads do not live here.
- **The log grows monotonically.** `Repository.prune()` exists for the
  non-audit-relevant tail, but audit rows are exempt and the log is not
  self-limiting. A long-running daemon needs retention policy, and that policy
  cannot delete anything an audit depends on.
- **Every read is a query.** A subscriber that wants live events still has to tail
  the log rather than receiving a push. The `bus.tail` IPC method polls by
  sequence number, which is why the TUI costs one round-trip per second on a local
  socket instead of holding an SSE connection.

**Benefits:**

- A replay cannot disagree with what happened, because there is nothing else to
  disagree with.
- Crash recovery is "replay the log from the last checkpoint", which is a
  statement that fits in one sentence.
- `content_hash` gives free idempotency: replaying a log that was already applied
  produces identical hashes, and `BusEventLog.append()` rejects duplicates. Without
  this, every replay would double-apply, and crash recovery would be a corruption
  bug rather than a feature.
- The audit export is a filter over the same data the TUI shows, so a compliance
  report cannot contain anything the operators could not see.
