# The governance model

This is the reason OpenBurrow exists. A2A, MCP, and ACP each explicitly leave
delegation authority, accountability, and cross-boundary audit obligations
unspecified — correctly, because those are not transport concerns. But they are the
questions that decide whether a multi-agent system can be deployed where there are
auditors, and somebody has to answer them.

The whole model reduces to one sentence, and everything below is that sentence made
mechanical.

> **Authority originates with a human and can only ever narrow.**

## Why that sentence

Every alternative formulation fails somewhere:

| Formulation | Fails because |
|---|---|
| "Agents have permissions" | Permissions come from somewhere. This defers the question. |
| "Authority is delegated transitively" | Unbounded transitivity means a chain can drift arbitrarily far from its origin, and nobody can find the origin. |
| "Agents inherit their parent's authority" | Inheritance is widening. A child that inherits everything its parent held can do everything its parent could, which makes depth meaningless. |
| "Authority is granted per action" | Then a long task needs hundreds of grants, and the grants become noise nobody reads. |

The chosen sentence is narrow enough to enforce and strong enough to answer the
auditor's question. It has two halves, and both are load-bearing.

**Originates with a human.** Not with a coordinator, not with a root agent, not
with the daemon. `Delegation` has a Pydantic validator that refuses to construct
without an `authorized_by` naming a human. There is no code path that creates one
without it.

**Can only narrow.** A sub-delegation is intersected with what the delegator
actually held. It cannot ask for more, and it cannot grant more even if it wanted
to.

## The four invariants

### Invariant 1 — a human anchor is required

```python
class Delegation(BurrowModel):
    @model_validator(mode="after")
    def _human_required(self) -> Delegation:
        if not self.authorized_by or self.authorized_by.startswith("lane_"):
            raise AuthorityScopeError(
                "a delegation must name the human who authorised it",
                hint="Agent-initiated delegations are not permitted; escalate to a human.",
            )
        return self
```

The check rejects anything that looks like a lane id. This is stricter than
"non-empty" on purpose: the failure it guards against is not a missing field, it is
a coordinator passing its own lane id because that was the easy thing to do.

**Cost:** several legitimate-looking shortcuts become impossible, including "the
coordinator delegates to itself for a sub-task". That is intentional. A coordinator
that needs authority for a sub-task should be given it by the human who gave it the
session, explicitly, once.

### Invariant 2 — chain depth is bounded

Default 3, hard ceiling 10. Exceeding the ceiling raises `ConfigError` with the
hint *"that is probably a typo"* — because a depth limit above 10 is never a
considered choice, it is someone working around an error message.

Depth is recorded on the delegation, not recomputed by walking the chain. Recomputing
would be more robust in principle and would mean the bound could be evaded by
deleting an intermediate record — which the append-only log makes impossible anyway,
so the cheaper representation is also the safer one.

### Invariant 3 — re-delegation requires consent

If the delegator did not hold `may_redelegate`, a sub-delegation is refused even
when the scope would be narrower.

This is the invariant that surprises people. Narrowing is safe, so why refuse? Because
*who decides* is a separate question from *how much*. A human who delegates read
access to a lane is often making a statement about the shape of the work, not just
its extent, and letting that lane hand the capability to a third party changes the
shape. Consent is what makes the chain legible: every hop is a decision someone made,
not a mechanical consequence.

### Invariant 4 — scope can only narrow

```python
ungranted = [cap for cap in requested_scope if not granted.allows(cap)]
if ungranted:
    raise SilentAuthorityCreepError(
        f"delegation requests {len(ungranted)} capability(ies) the delegator does not hold",
        hint="Narrow the request, or ask the original authoriser for a wider scope.",
        context={"ungranted": ungranted, "held": sorted(granted.capabilities)},
    )
```

This is the check that catches the interesting attack. A sub-delegation asking for
*more* than the delegator held is either a confused agent or a prompt injection
trying to escalate. Both must fail loudly.

**It raises rather than trimming the scope.** Trimming is the tempting choice — the
delegation could proceed with the intersection, which is "obviously" what was meant.
But trimming means the escalation attempt leaves no trace, and the escalation attempt
is the single most interesting thing that can happen in this system. It is also the
signal that an upstream component is compromised, which is exactly the information
you cannot afford to discard.

The error name says it: *silent* authority creep is the failure mode being prevented.

### The `narrow()` primitive

```python
def narrow(self, requested: AuthorityScope) -> AuthorityScope:
    """The only sanctioned way to hand off authority."""
    return AuthorityScope(
        capabilities=sorted(self.capabilities & requested.capabilities),
        denied=sorted(set(self.denied) | set(requested.denied)),
        ...
    )
```

Capabilities intersect — that is the narrowing. Denials **union** — that is the part
people get wrong. If the delegator was denied `fs:write:.env` and the requestor was
denied `net:*`, the result must be denied both. Unioning denials is how a chain
accumulates prohibitions rather than losing them.

### Denial beats grant

```python
def allows(self, capability: str) -> bool:
    if self._matches(self.denied, capability):
        return False  # a prohibition is absolute
    return self._matches(self.granted, capability) or self._wildcard
```

Denials are consulted first. If grants were checked first, a wildcard grant (`fs:*`)
would satisfy everything and a specific denial could never take effect. Any system
where a broad permission bypasses a narrow prohibition has a permission model that
does not work.

`AuthorityScope.allows()` also **fails closed on an empty scope**: an unrestricted
scope is constructed explicitly via `AuthorityScope.unrestricted()`, never by
omission. An empty scope permits nothing.

## What governance does not cover

Stated plainly, because a governance layer that overstates its reach is worse than
one with a documented boundary.

**Tool calls inside a running lane.** MCP is passed through untouched
([ADR 0002](../architecture/adr/0002-mcp-passthrough.md)). A lane that shells out to
`sed` to edit a denied file is not stopped by the ledger. Enforcement at the
filesystem or MCP layer is the harness's sandbox, and OpenBurrow layers on top of
it rather than replacing it.

**What a lane actually does with its authority.** The ledger gates the *grant* of
authority and gates a concrete *action* via `check_action()`, which is called
immediately before execution. It cannot know whether a lane that was granted
`fs:write:src/**` used it well.

**Prompt injection.** Detection exists (`detect_injection`, 23 markers) and is
deliberately noisy. It flags; it does not block. A detected injection produces a
governance flag and an audit record, and a human decides.

**The content of what agents say to each other.** Cross-boundary messages are
*classified* (`detect_cross_boundary_message`) and the classification drives audit
strictness. It does not censor.

## Defense in depth on the prompt path

A lesson is a prompt fragment injected into every lane's context. That makes the
lesson path the highest-value injection target in the entire system: a poisoned
lesson does not affect one lane, it affects every lane that ever loads it, and it
persists across sessions.

So `detect_poisoned_lesson` runs **independently of the Brain's own classifier**.
The Brain's classifier decides what becomes a lesson; the detector decides whether a
lesson that became one is safe to inject. They share no code, no thresholds, and no
configuration. A single compromised component does not defeat both checks.

This is duplication by design, and it is the one place in the codebase where
duplication is the point rather than a smell.

## The audit trail

Every delegation, refusal, and blocked action produces an `AuditRecord` in an
append-only table. Records carry:

- the delegation id and the full chain
- `authorized_by`, the human anchor
- what was requested versus what was held
- the outcome, and for a refusal, which check failed
- the trust boundary the call crossed

`burrow audit --delegation <id>` renders the chain. `burrow audit --export` produces
JSONL, CSV, or HTML.

Two properties make it usable as evidence:

**It is derived from the same log the TUI reads.** A compliance report cannot contain
anything the operators could not see, because there is only one log.

**Refusals are first-class events.** A governance layer that is working looks like a
governance layer that is working, not like an absence of events. `governance.blocked`
is surfaced in `burrow watch`, counted in `burrow report`, and exported in the audit.
The worst outcome for a system like this is that it silently prevents things and
nobody notices, because then it gets switched off as "not doing anything".

## Configuration

Governance configuration has an asymmetry worth knowing about, because it is
deliberate:

**`openburrow.yaml`** holds the team's governance policy. It is committed, reviewed,
and the same for everyone.

**Environment variables** can make governance *stricter* but never weaker:

```python
# max_delegation_depth: env can only lower it
"max_delegation_depth": min(repo_value, env_value)

# verify_capability_cards: env can escalate, and "off" only warns
# (a machine cannot disable a check the team committed to)
```

A machine cannot silently disable a control the team agreed on. It can tighten one.
That direction is the only one that is safe, because a developer who wants to work
around a governance rule should have to change the committed configuration and get
it reviewed — not set an environment variable.

## Where to read the code

| Concern | Module |
|---|---|
| The delegation chain and its four checks | `packages/openburrow-governance/src/openburrow/governance/ledger.py` |
| Detection heuristics | `packages/openburrow-governance/src/openburrow/governance/detectors.py` |
| `AuthorityScope`, `Delegation`, `AuditRecord` | `packages/openburrow-core/src/openburrow/core/models/governance.py` |
| Approval requests | `packages/openburrow-core/src/openburrow/core/models/approval.py` |
| Configuration merge | `packages/openburrow-core/src/openburrow/core/config/load.py` |
| CLI surface | `packages/openburrow-cli/src/openburrow/cli/commands/governance.py` |
