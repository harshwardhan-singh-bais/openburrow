# 0009 — Detection and enforcement are separate components

**Status:** Accepted

## Context

Governance needs to both *prevent* bad actions and *notice* suspicious ones. The
natural implementation is one component that does both: a policy engine that scores
an action, blocks it above a threshold, and flags it below.

That design has one tunable, and the tunable has to serve two incompatible goals.

## Decision

Two modules with separate thresholds and opposite defaults:

| | `governance/ledger.py` | `governance/detectors.py` |
|---|---|---|
| Role | enforcement | detection |
| False positive costs | a stalled session, lost work | one dismissible flag |
| Default on uncertainty | refuse | flag, do not block |
| Threshold set for | zero false positives | high recall |

A flagged-but-allowed action is a **correct outcome**, not a contradiction.

## Rejected

**One policy engine with a single threshold.** Rejected because the tuning
requirements are opposite. To catch a subtle prompt injection, detection needs to
be sensitive enough that it fires on legitimate-but-unusual phrasing. To be usable
in the hot path, enforcement needs to never fire on a legitimate action. A single
threshold has to pick one, and whichever it picks, the other role is broken.

**Enforcement only.** Rejected because enforcement can only refuse what it has a
rule for. Detectors catch the categories we did not anticipate — that is the entire
point of having them. A system with only enforcement is a system that is safe
against yesterday's attacks.

**Detection only.** Rejected because a flag nobody acts on is not governance. There
must be a component that *refuses*, and the refusal has to be in the path of the
action rather than in a report.

**A machine-learned classifier shared by both.** Rejected because enforcement has to
be explicable. When the ledger refuses a delegation, the audit record must say
*which* capability was ungranted and *why* — a score between 0 and 1 is not an
answer an auditor accepts, and it is not an answer an operator can act on.

## Consequences

**Accepted costs:**

- **Two modules, two threshold sets, more surface.** The thresholds interact:
  turning detection up produces more flags, which produces more human review, which
  produces pressure to turn enforcement down. This is a real organisational dynamic
  and the mitigation is that the two are configured independently so the pressure
  can be resisted.
- **A flag does not mean something was prevented.** `governance.flag` events and
  `governance.blocked` events are different, and the metrics keep them separate
  (`governance_flags_raised` versus the ledger's refusals). Conflating them in a
  report would make the system look like it prevented more than it did.
- **Detectors produce noise by design.** `INJECTION_MARKERS` is 23 phrases, and
  some legitimate technical discussion will match. That is the intended trade, and
  the reason `detect_injection` returns a structured `Detection` with a category and
  matched phrase rather than a bare boolean — a reviewer needs to see *why* it
  fired in order to dismiss it quickly.

**Benefits:**

- The enforcement path can be reasoned about completely: four checks, all
  explicable, all with tests. It is small enough to audit by reading it.
- Detection can be aggressive without making the system unusable.
- The two can evolve independently. Adding an injection marker is a detection
  change with no risk to enforcement; changing a scope rule is an enforcement change
  that detection does not need to know about.
