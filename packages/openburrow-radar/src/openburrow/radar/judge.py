"""The LLM-as-judge, and an honest account of what its confidence means.

A model asked "will these two changes conflict" returns a number. It is very
tempting to treat that number as a probability. It is not one. It is a token
sampled from a distribution over plausible-looking text, and it moves with the
phrasing of the prompt, the order of the two items, and the model's mood.

So this module does three things that a naive wrapper would not:

**It clamps.** Raw judge output is mapped into ``[0.5, 0.95]`` rather than
trusted across ``[0, 1]``. A judge saying 0.99 is expressing enthusiasm, not
near-certainty, and letting it drive an automatic negotiation at full
confidence is how the Radar earns a reputation for crying wolf.

**It distinguishes "no conflict" from "I could not tell".** These are completely
different answers and collapsing them is the most common way an LLM-backed
component becomes untrustworthy: the model is unavailable, returns malformed
JSON, or times out, and the system silently records "no conflict found". Six
weeks later the metrics claim full coverage of a period where the judge was
down. :attr:`JudgeVerdict.known` carries that distinction, and the Radar reports
coverage honestly because of it.

**It fails toward silence, not toward action.** An unknown verdict does not
trigger a negotiation. Spawning a negotiation on a guess would waste two lanes'
time and, worse, train people to ignore negotiations. Missing a conflict costs a
merge resolution; a Radar that is wrong often enough gets muted, and a muted
Radar is worth nothing at all.

A judge is not required. With no model configured the Radar still runs on
deterministic signals alone, which is the majority of its value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from openburrow.core.logging import get_logger
from openburrow.radar.intent import Intent

log = get_logger(__name__)

#: Confidence is clamped into this band. The floor exists because a judge that
#: said "conflict" at all is asserting something, and reporting 0.05 would make
#: the verdict indistinguishable from noise. The ceiling exists because a model
#: cannot be more certain than the evidence it was given.
CONFIDENCE_FLOOR = 0.5
CONFIDENCE_CEILING = 0.95

#: What the judge is allowed to say the conflict is about. A closed vocabulary
#: keeps downstream rendering from having to handle arbitrary model prose.
KINDS: tuple[str, ...] = (
    "same_file",
    "same_symbol",
    "interface_change",
    "dependency_order",
    "resource_contention",
    "duplicate_work",
    "none",
)

_PROMPT = """You predict whether two coding agents are about to conflict.

You are given what each agent says it intends to change. Decide whether
proceeding in parallel would cause one to invalidate, duplicate, or block the
other's work.

Rules:
- A shared file is not automatically a conflict. Two agents editing disjoint
  regions of one file are usually fine; say so.
- An interface change that the other agent depends on IS a conflict.
- Duplicated effort with different implementations IS a conflict.
- If you cannot tell from the information given, set "conflict" to false and
  "confidence" to 0. Do not guess to be helpful.

Reply with JSON only, no prose:
{"conflict": bool, "confidence": 0.0-1.0, "kind": one of %s,
 "reason": "one sentence", "action": "one short imperative"}

AGENT A
%s

AGENT B
%s
"""


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """What the judge concluded, and how much of that is actually known."""

    conflict: bool
    confidence: float = 0.0
    kind: str = "none"
    reason: str = ""
    action: str = ""
    #: "llm" | "unavailable" | "malformed" | "rejected"
    method: str = "llm"
    #: False when the judge could not render a usable verdict at all. Callers
    #: must treat ``known=False`` as "no information", never as "no conflict".
    known: bool = True

    @property
    def is_unknown(self) -> bool:
        return not self.known

    def render(self) -> str:
        if not self.known:
            return f"[dim]judge unavailable ({self.method})[/dim]"
        if not self.conflict:
            return f"[dim]no conflict[/dim] ({self.confidence:.2f})"
        return f"[bold yellow]{self.kind}[/bold yellow] ({self.confidence:.2f}) — {self.reason}"


class ConflictJudge:
    """Asks a model whether two intents collide."""

    def __init__(self, *, model: str = "", timeout: float = 20.0) -> None:
        self.model = model
        self.timeout = timeout
        self._unavailable_reason = ""

    @property
    def available(self) -> bool:
        return bool(self.model)

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason or ("no model configured" if not self.model else "")

    async def judge(self, a: Intent, b: Intent) -> JudgeVerdict:
        """Compare two intents. Never raises — a judge failure is data, not an error."""
        if not self.model:
            return JudgeVerdict(
                conflict=False,
                method="unavailable",
                known=False,
                reason="no judge model configured",
            )

        try:
            import litellm
        except ImportError:
            self._unavailable_reason = "litellm is not installed"
            return JudgeVerdict(
                conflict=False, method="unavailable", known=False, reason=self._unavailable_reason
            )

        prompt = _PROMPT % (list(KINDS), _describe(a), _describe(b))
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout=self.timeout,
            )
            raw = response["choices"][0]["message"]["content"]
        except Exception as exc:
            self._unavailable_reason = f"{type(exc).__name__}: {exc}"
            log.warning("radar.judge.failed", error=self._unavailable_reason, model=self.model)
            return JudgeVerdict(
                conflict=False, method="unavailable", known=False, reason=self._unavailable_reason
            )

        parsed = _parse_json_object(raw)
        if parsed is None:
            log.warning("radar.judge.malformed", model=self.model, raw=raw[:200])
            return JudgeVerdict(
                conflict=False,
                method="malformed",
                known=False,
                reason="judge returned unparseable output",
            )

        return _verdict_from(parsed)


def _verdict_from(parsed: dict) -> JudgeVerdict:
    """Turn parsed judge output into a verdict, defensively."""
    conflict = bool(parsed.get("conflict"))
    raw_confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    kind = str(parsed.get("kind") or "none")
    if kind not in KINDS:
        # An out-of-vocabulary kind is not fatal, but it must not reach the
        # renderer as if it were meaningful.
        kind = "none" if not conflict else "same_file"

    if not conflict:
        return JudgeVerdict(
            conflict=False,
            confidence=0.0,
            kind="none",
            reason=str(parsed.get("reason") or ""),
            method="llm",
        )

    clamped = max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, confidence))
    return JudgeVerdict(
        conflict=True,
        confidence=clamped,
        kind=kind,
        reason=str(parsed.get("reason") or "")[:300],
        action=str(parsed.get("action") or "")[:200],
        method="llm",
    )


def _describe(intent: Intent) -> str:
    """Render an intent for the prompt.

    Only deterministic fields are included. If the judge were shown a
    model-written description of the *other* intent it could anchor on its own
    earlier guess, and a mistake would compound instead of staying contained.
    """
    lines = [f"lane: {intent.lane_id}", f"risk: {intent.risk_tier}"]
    if intent.description:
        lines.append(f"working on: {intent.description}")
    if intent.files:
        lines.append("files: " + ", ".join(sorted(intent.files)[:20]))
    if intent.directories:
        lines.append("directories: " + ", ".join(sorted(intent.directories)[:10]))
    if intent.symbols:
        lines.append("symbols: " + ", ".join(sorted(intent.symbols)[:10]))
    if intent.step_ids:
        lines.append(f"plan steps owned: {len(intent.step_ids)}")
    return "\n".join(lines)


def _parse_json_object(raw: str) -> dict | None:
    """Extract the first JSON object, tolerating fences and surrounding prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "CONFIDENCE_CEILING",
    "CONFIDENCE_FLOOR",
    "KINDS",
    "ConflictJudge",
    "JudgeVerdict",
]
