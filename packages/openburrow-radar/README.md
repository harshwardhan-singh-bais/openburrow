# openburrow-radar

Merge Radar: predicting collisions before they cost anyone a day.

## Why it exists

The expensive failures in multi-agent work are not loud. A merge conflict is loud and
cheap — git tells you immediately. What is quiet and expensive is two lanes spending an
afternoon implementing incompatible versions of the same interface, discovering it at
integration, and then having to decide whose work to throw away.

## Three layers, in order of trustworthiness

| Module | Trust | Source |
|---|---|---|
| `intent.py` | certain | claims and plan steps — records of decisions |
| `conflicts.py` | mixed | deterministic overlap, plus judge verdicts |
| `judge.py` | probabilistic | a model's reading of two descriptions |

## Files come from records. Descriptions may come from a model.

The single most important rule in this package:

> **A model can never add a file to an intent.**

`IntentExtractor.with_model_hints()` has no `files` parameter, and that is not an
oversight. The moment a model can add a file, the deterministic half of the Radar stops
being deterministic, and every claim about "we detected this collision" becomes
unauditable.

A lane's file set is either claimed or unknown, and the Radar says which.

## Certainty is a first-class field

`ConflictPrediction.certain` is `True` only when the conflict rests on records.

| Signal | Severity | Thresholded? |
|---|---|---|
| `file` — both lanes claim the same concrete file | `warn`/`block` | **never** |
| `scope` — a directory claim covering another lane's files | `warn`/`info` | yes, or high-risk |
| `semantic` — a judge thinks one change invalidates the other | `info`/`warn` | yes |

A shared file is reported **regardless of any confidence threshold**. Suppressing a
known collision behind a score is how you get a merge conflict you had already detected
and chose not to mention.

Systems that blur the line between fact and prediction get switched off: if "possible
conflict" renders with the same weight as "both lanes are editing routes.py", the true
positives stop being believed within a week.

## The judge clamps its own confidence

Raw model output is mapped into `[0.5, 0.95]`, never trusted across `[0, 1]`. A judge
saying 0.99 is expressing enthusiasm, not near-certainty.

## The judge distinguishes "no conflict" from "I could not tell"

`JudgeVerdict.known` carries the difference, and it matters more than the confidence
does.

If the model is unavailable, returns malformed JSON, or times out, the verdict is
`known=False`. Collapsing that into "no conflict found" is the most common way an
LLM-backed component becomes untrustworthy: the model goes down, the system records
nothing for the outage window, and six weeks later the metrics claim full coverage of a
period where the judge was not running.

An unknown verdict **does not trigger a negotiation**. Spawning one on a guess wastes
two lanes' time and trains people to ignore negotiations. Missing a conflict costs a
merge resolution; a muted Radar is worth nothing at all.

## Coverage is reported, not assumed

```python
stats.judge_coverage  # usable verdicts / consultations
stats.semantic_available  # False when coverage is too low to mean anything
```

`burrow report` surfaces both. A report showing "0 semantic conflicts found" without
showing "the judge was unreachable for 100% of calls" would be actively misleading —
and it is exactly the number that ends up in a slide deck.

With no model configured, `judge_calls` is 0 and the report says *"judge: not used
(deterministic signals only)"* rather than claiming 100% coverage.

## Cost control

The naive implementation is quadratic in both latency and money: eight lanes is 28
pairs, and 28 model calls per scan is a cost that grows with the square of the team.

`_worth_judging()` is a cheap prefilter — shared plan step, shared symbol, shared
top-level directory, or nothing recorded at all. It is deliberately generous: the cost
of the filter being too loose is money, and the cost of it being too tight is silence.
Silence is the failure mode that gets people hurt.

## Announcements are deduplicated and re-armed

A scan every few seconds must not re-report the same collision until somebody mutes the
Radar. But naive deduplication means a lane that changes what it is working on never
gets its conflicts re-examined.

So the dedupe key includes the file set, and updating a lane's intent clears that
lane's entries:

> The same two lanes in the same conflict are announced once; the same two lanes in a
> different conflict are announced again.

A conflict that nobody acted on is still a conflict, so there is also a re-announcement
interval — the difference between "the Radar noticed" and "the Radar was ignored".

## A prediction is a prompt to negotiate, not a decision

When the Radar raises a conflict, the coordinator opens an ACP exchange between the two
lanes. They resolve it, or a human does. The Radar never assigns ownership, because the
lanes know more about their own work than a pairwise score does.

## Documentation

- [docs/protocols/acp.md](../../../docs/protocols/acp.md) — what happens after a prediction
- [docs/architecture/overview.md](../../../docs/architecture/overview.md#the-honesty-rule) — the pattern this package follows
