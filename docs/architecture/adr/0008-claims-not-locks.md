# 0008 — Claims are advisory, not locks

**Status:** Accepted

## Context

Two lanes must not both write the same file. The obvious mechanism is a lock: the
first lane to claim `src/api/routes.py` holds it, and the second lane is refused
until it is released.

## Decision

Claims are advisory. A claim is a record in the database and a message on the bus.
It is not a filesystem lock and it does not prevent a write. Enforcement is
negotiation: the second lane to want the file is told it is claimed, and an ACP
exchange settles who gets it.

First claim wins, but "wins" means "holds the stronger default position in a
negotiation".

## Rejected

**Hard locks.** Rejected because they deadlock on crash. A lane that dies holding a
lock leaves the file locked forever, and the recovery path becomes "find the lock
record and delete it" — which is not a recovery path anyone should have to run, and
which will be run by hand at 2am by someone who does not know what the lock was
protecting.

This is not a theoretical concern. Lanes crash: harnesses get rate-limited, hit
context limits, and are killed by the supervisor. A locking scheme whose failure
mode requires manual intervention on every crash is a locking scheme that will be
disabled.

**Filesystem locks (`flock`, lock files).** Rejected for the same reason plus two
more: they do not survive a worktree being reset, and they cannot express a claim
on a *directory* or on a *symbol*, which the plan needs.

**Lease-based locks with mandatory expiry.** The closest alternative and the most
tempting. Rejected because a lease that expires while a lane is still working
produces a silent collision — the worst possible outcome, since both lanes now
believe they hold the file and neither will be told otherwise. An advisory claim
that is *visible* is safer than a lease that is *wrong*.

## Consequences

**Accepted costs:**

- **Two lanes can write the same file.** The protocol is not enforced by the
  filesystem, so a lane that ignores its inbox will collide. The mitigation is
  visibility rather than prevention: the Radar predicts the collision before it
  happens, the bus log records both writes, and `burrow claims` shows the overlap.
- **Ownership requires a negotiation, which takes time.** A hard lock would refuse
  instantly. An ACP exchange takes a few messages. In practice this is faster than
  it sounds — the negotiation is one `propose` and one `accept` in the common case
  — but it is not instant.
- **`overlaps()` has to handle the directory case.** A lane claiming `src/` and a
  lane claiming `src/api/routes.py` is a conflict, and getting that comparison
  wrong in the permissive direction means a real collision goes unreported. The
  implementation treats a directory claim covering a file claim as an overlap,
  deliberately, and the Radar then reports it at a lower severity than a direct
  file collision.

**Benefits:**

- Crash recovery needs no intervention. A claim by a dead lane is simply a claim
  that a live lane can negotiate past.
- Claims compose with the governance model rather than sitting outside it. A claim
  is an *intent to act on a resource*, which is the same shape as a delegation
  scope, so the same reasoning applies to both.
- A claim can express things a lock cannot: a directory, a symbol, a plan step, or
  a shared resource like a database or a port. `ClaimKind` has four kinds precisely
  because "who is using the test database" is the same coordination problem as "who
  is editing this file".
