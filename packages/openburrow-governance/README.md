# openburrow-governance

The delegation ledger, the detection heuristics, and the audit log. **This is the
package the project exists for.**

## Why this package exists

A2A, MCP, and ACP each document what they deliberately do not specify, and the
omissions overlap on one point. None of them can express:

- who is allowed to delegate what
- what authority actually transferred
- who is accountable when an agent acts on another's behalf
- what must be audited when a call crosses a trust boundary

These are not implementation details. They are the questions that decide whether an
autonomous system is deployable where there are auditors.

The model reduces to one sentence:

> **Authority originates with a human and can only ever narrow.**

Everything in this package is that sentence made mechanical. See
[ADR 0003](../../../docs/architecture/adr/0003-governance-layer.md) and
[docs/governance/model.md](../../../docs/governance/model.md).

## The four invariants

### 1. A human anchor is required

`Delegation` refuses to construct without an `authorized_by` naming a human. The
validator rejects anything that looks like a lane id — stricter than "non-empty" on
purpose, because the failure it guards against is a coordinator passing its own lane id
because that was the easy thing to do.

### 2. Chain depth is bounded

Default 3, hard ceiling 10. A ceiling above 10 is never a considered choice; it is
someone working around an error message, and the error says so.

### 3. Re-delegation requires consent

If the delegator did not hold `may_redelegate`, a sub-delegation is refused even when
the scope would be narrower. Narrowing is safe; the question of *who decides* is
separate from *how much*, and consent is what makes every hop in the chain a decision
someone made.

### 4. Scope can only narrow

```python
ungranted = [cap for cap in requested if not granted.allows(cap)]
if ungranted:
    raise SilentAuthorityCreepError(...)
```

**It raises rather than trimming to the intersection.** Trimming is the tempting
choice — the delegation could proceed with what both allow, which is "obviously" what
was meant. But trimming means the escalation attempt leaves no trace, and an escalation
attempt is the most interesting thing that can happen in this system: it means either a
confused agent or a compromised upstream component. That is not information to discard.

The error name is the failure mode: *silent* authority creep.

## Denial beats grant

```python
def allows(self, capability):
    if self._matches(self.denied, capability):
        return False
    return self._matches(self.granted, capability) or self._wildcard
```

Denials are consulted first. If grants were checked first, a wildcard grant (`fs:*`)
would satisfy everything and a specific denial could never take effect. Any system
where a broad permission bypasses a narrow prohibition has a permission model that does
not work.

An empty scope permits **nothing**; unrestricted authority is constructed explicitly
via `AuthorityScope.unrestricted()`, never by omission.

## `narrow()` is the only sanctioned handoff

```python
def narrow(self, requested):
    return AuthorityScope(
        capabilities=sorted(self.capabilities & requested.capabilities),
        denied=sorted(set(self.denied) | set(requested.denied)),
    )
```

Capabilities **intersect**; denials **union**. The union is the part people get wrong.
If the delegator was denied `fs:write:.env` and the requestor was denied `net:*`, the
result must be denied both — unioning denials is how a chain accumulates prohibitions
rather than losing them.

## Detection is a separate module, on purpose

| | `ledger.py` | `detectors.py` |
|---|---|---|
| Role | enforcement | detection |
| False positive costs | a stalled session, lost work | one dismissible flag |
| On uncertainty | refuse | flag, do not block |
| Tuned for | zero false positives | high recall |

Tuning one component for both failure modes is impossible: the sensitivity that catches
a subtle injection is the sensitivity that makes enforcement unusable. A
flagged-but-allowed action is a **correct outcome**, not a contradiction. See
[ADR 0009](../../../docs/architecture/adr/0009-detection-vs-enforcement.md).

## What the detectors cover

`detect_injection` (23 markers) · `detect_capability_mismatch` · `detect_impersonation`
· `detect_poisoned_delegation` · `detect_poisoned_lesson` · `detect_adversarial_intent`
· `detect_authority_creep` · `detect_cross_boundary_message` · `scan_for_secrets`

Two details:

**`detect_poisoned_lesson` runs independently of the Brain's classifier.** A lesson is
a prompt fragment injected into every lane's context, which makes the lesson path the
highest-value injection target in the system. The Brain's classifier decides what
becomes a lesson; this decides whether a lesson is safe to inject. They share no code,
no thresholds, and no configuration — the one place in the codebase where duplication
is the point.

**`scan_for_secrets` returns truncated matches.** A scanner that prints what it found
has become a leak vector, which is the failure mode of nearly every naive
implementation.

## What governance does not cover

Stated plainly, because an overstated security posture is worse than a documented
boundary. Tool calls inside a running lane are not inspected — MCP is passed through
untouched. Enforcement that does not depend on a harness cooperating is a container.
See [docs/protocols/mcp.md](../../../docs/protocols/mcp.md#if-you-need-tool-level-control).

## The audit trail

Every delegation, refusal, and blocked action produces an `AuditRecord` in an
append-only table, carrying the chain, the human anchor, what was requested versus
held, the outcome, and which check failed.

**Refusals are first-class events.** `governance.blocked` appears in `burrow watch` and
is counted in `burrow report`. The worst outcome for a governance layer is that it
silently prevents things and nobody notices — because then it gets switched off as
"not doing anything".

## Configuration asymmetry

Environment variables can make governance **stricter** but never **weaker**. A machine
cannot silently disable a control the team committed to; it can tighten one. A
developer who wants to work around a governance rule has to change the committed
configuration and get it reviewed.
