"""Lessons: operational knowledge that is worth propagating right now.

A Brain entry answers "what is true about this repository". A lesson answers
"what should I do differently next time" — and the difference matters, because
lessons are *perishable*. A Brain entry about how the auth middleware reads
tokens is still useful next quarter. A lesson about the CI runner being slow on
Tuesdays is noise by Friday.

That perishability is why lessons are handled separately rather than as Brain
entries with a short TTL bolted on. Three consequences fall out of it:

**Expiry is a feature, not cleanup.** Every lesson can carry an ``expires_at``.
A lesson that outlives its cause is worse than no lesson, because it teaches an
agent to work around a problem that no longer exists.

**Hit rate drives eviction.** A lesson injected twenty times that never changed
anyone's behaviour is not a lesson; it is a paragraph. The store retires those
automatically once it has enough samples to be confident, and it reports the
rate rather than hiding the decision. This is the mechanism that stops the
injected context from growing monotonically over a long session.

**The classifier is not trusted alone.** :func:`openburrow.governance.detect_poisoned_lesson`
runs independently over anything the classifier produces. A lesson is a prompt
fragment that gets injected into every lane's context, which makes the lesson
path the single highest-value target in the system for an injection attack. Two
independent checks on that path is not paranoia; one check on that path would be
negligence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from openburrow.core.db.engine import Database
from openburrow.core.db.repository import Repository
from openburrow.core.errors import OpenBurrowError
from openburrow.core.logging import get_logger
from openburrow.core.models import Lesson, LessonScope
from openburrow.core.models.base import now

log = get_logger(__name__)

#: A session-scoped lesson is injectable on first sight: the point is to reach
#: the next lane within seconds, and a wrong lesson costs one session.
SESSION_CONFIDENCE = 0.65

#: A repository- or org-scoped lesson outlives the session and needs a second
#: independent witness before it is injected. Same asymmetry as the Brain, same
#: reason: the cost of being wrong is paid by people who were not there.
WIDE_CONFIDENCE_FLOOR = 0.8

#: Below this, a lesson is stored and listed but not injected.
INJECTION_FLOOR = 0.6

#: Injected lessons are cheap but not free. A dozen competing lessons in one
#: prompt is not guidance, it is a wall.
DEFAULT_INJECTION_BUDGET = 5

#: Eviction needs evidence. Retiring a lesson after one unhelpful injection would
#: throw away a good lesson that simply did not apply to that task.
EVICTION_MIN_SAMPLES = 5
EVICTION_HIT_RATE = 0.15


class LessonError(OpenBurrowError):
    """A lesson could not be recorded or retrieved."""

    code = "openburrow.lesson_error"
    hint = "Check `burrow lessons list` for the current set."


@dataclass(frozen=True, slots=True)
class LessonCandidate:
    """A proposed lesson, before the store decides whether to keep it."""

    title: str
    body: str
    trigger: str = ""
    remedy: str = ""
    scope: LessonScope = LessonScope.SESSION
    ttl_days: int | None = None
    source_lane: str = ""
    source_harness: str = ""
    source_task_id: str = ""
    source_message_id: str = ""
    promoted_by: str = "classifier"
    confidence: float = SESSION_CONFIDENCE
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EvictionReport:
    retired: list[str]
    retained: int
    reason: str = ""


class LessonStore:
    """Persistence, selection, and eviction for lessons."""

    def __init__(self, database: Database, *, repo_id: str = "") -> None:
        self.database = database
        self.repo_id = repo_id

    # --- writing -----------------------------------------------------------
    async def promote(self, candidate: LessonCandidate, *, session_id: str) -> Lesson:
        """Record a lesson, deduplicating against live lessons with the same title.

        Duplicates are merged rather than stored twice: two lanes hitting the same
        problem is the strongest signal a lesson is real, and storing it twice
        would inject it twice and dilute the prompt.
        """
        existing = await self._find_live(candidate.title, session_id=session_id)
        if existing is not None:
            if candidate.source_lane and candidate.source_lane != existing.source_lane:
                existing.confidence = max(existing.confidence, WIDE_CONFIDENCE_FLOOR)
                existing.tags = sorted(set(existing.tags) | set(candidate.tags))
            existing.record_injection(candidate.source_lane or "")
            await self._save(existing)
            log.info("lesson.merged", lesson_id=existing.id, title=existing.title)
            return existing

        lesson = Lesson(
            session_id=session_id,
            repo_id=self.repo_id if candidate.scope != LessonScope.SESSION else "",
            scope=candidate.scope,
            title=candidate.title,
            body=candidate.body,
            trigger=candidate.trigger,
            remedy=candidate.remedy,
            source_lane=candidate.source_lane,
            source_harness=candidate.source_harness,
            source_task_id=candidate.source_task_id,
            source_message_id=candidate.source_message_id,
            promoted_by=candidate.promoted_by,
            confidence=(
                candidate.confidence
                if candidate.scope == LessonScope.SESSION
                else min(candidate.confidence, 0.6)
            ),
            tags=list(candidate.tags),
        )
        if candidate.ttl_days is not None:
            lesson.expires_at = now() + timedelta(days=candidate.ttl_days)
        await self._save(lesson)
        log.info(
            "lesson.promoted",
            lesson_id=lesson.id,
            scope=str(lesson.scope),
            ttl_days=candidate.ttl_days,
        )
        return lesson

    async def record_outcome(self, lesson_id: str, *, lane_id: str, helped: bool) -> Lesson:
        """Record whether a lesson actually changed a lane's behaviour.

        "Helped" is reported by the lane or inferred from a subsequent success,
        not assumed. A lesson that was injected and never acknowledged is counted
        as ignored, which is the conservative direction: it makes noise look like
        noise instead of manufacturing a positive signal.
        """
        async with self.database.session() as session:
            lesson = await Repository(session).get(Lesson, lesson_id)
        if lesson is None:
            raise LessonError(
                f"no lesson {lesson_id!r}",
                hint="The lesson may have been retired or expired.",
                context={"lesson_id": lesson_id},
            )
        if helped:
            lesson.record_helped(lane_id)
        else:
            lesson.record_ignored(lane_id)
        await self._save(lesson)
        return lesson

    async def retire(self, lesson_id: str, *, reason: str = "") -> Lesson:
        async with self.database.session() as session:
            lesson = await Repository(session).get(Lesson, lesson_id)
        if lesson is None:
            raise LessonError(f"no lesson {lesson_id!r}", context={"lesson_id": lesson_id})
        lesson.retire(reason=reason)
        await self._save(lesson)
        return lesson

    # --- reading -----------------------------------------------------------
    async def live(self, *, session_id: str) -> list[Lesson]:
        async with self.database.session() as session:
            return await Repository(session).live_lessons(
                session_id=session_id, repo_id=self.repo_id
            )

    async def select_for_injection(
        self, *, session_id: str, budget: int = DEFAULT_INJECTION_BUDGET
    ) -> list[Lesson]:
        """Rank live lessons and return the ones worth spending context on.

        Ranking is hit rate first, then confidence, then recency. Hit rate leads
        because it is the only signal that comes from *outcomes* — confidence is
        what we believed when we wrote the lesson down, and a lesson can be
        confidently written and consistently useless.
        """
        lessons = await self.live(session_id=session_id)
        eligible = [
            lesson
            for lesson in lessons
            if lesson.is_live
            and lesson.confidence >= INJECTION_FLOOR
            and (lesson.scope == LessonScope.SESSION or lesson.confidence >= WIDE_CONFIDENCE_FLOOR)
        ]
        eligible.sort(
            key=lambda lesson: (lesson.hit_rate, lesson.confidence, lesson.created_at),
            reverse=True,
        )
        chosen = eligible[:budget]
        for lesson in chosen:
            lesson.record_injection(session_id)
        return chosen

    async def evict_noise(self, *, session_id: str) -> EvictionReport:
        """Retire lessons that have been injected enough times to be judged.

        The threshold is a hit *rate* with a minimum sample count, not an absolute
        count. Retiring on a single bad outcome would discard a good lesson that
        happened not to apply, and keeping on a low rate forever would let the
        prompt fill with advice nobody follows.
        """
        lessons = await self.live(session_id=session_id)
        retired: list[str] = []
        for lesson in lessons:
            samples = lesson.injection_count
            if samples < EVICTION_MIN_SAMPLES:
                continue
            if lesson.hit_rate >= EVICTION_HIT_RATE:
                continue
            lesson.retire(
                reason=(
                    f"injected {samples}x, helped {len(lesson.helped_lanes)} lane(s) "
                    f"(hit rate {lesson.hit_rate:.2f} below {EVICTION_HIT_RATE})"
                )
            )
            await self._save(lesson)
            retired.append(lesson.id)

        if retired:
            log.info("lesson.evicted", retired=len(retired), remaining=len(lessons) - len(retired))
        return EvictionReport(
            retired=retired,
            retained=len(lessons) - len(retired),
            reason="hit rate below threshold with sufficient samples",
        )

    # --- internals ---------------------------------------------------------
    async def _find_live(self, title: str, *, session_id: str) -> Lesson | None:
        for lesson in await self.live(session_id=session_id):
            if lesson.title.strip().casefold() == title.strip().casefold():
                return lesson
        return None

    async def _save(self, lesson: Lesson) -> None:
        async with self.database.session() as session:
            await Repository(session).save(lesson)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
#: Phrases that mark a message as carrying a lesson. Deliberately blunt: the
#: cost of a false positive is one extra candidate that the store then has to
#: corroborate, and the cost of a false negative is a lesson nobody learns.
_SIGNAL_PHRASES: tuple[str, ...] = (
    "the problem was",
    "turns out",
    "the fix is",
    "next time",
    "the trick is",
    "watch out",
    "this broke because",
    "root cause",
    "workaround",
)


@dataclass(frozen=True, slots=True)
class Classification:
    """The classifier's verdict on one piece of text."""

    is_lesson: bool
    candidate: LessonCandidate | None = None
    reason: str = ""
    method: str = "heuristic"


class LessonClassifier:
    """Turns bus traffic into lesson candidates.

    Heuristic by default and LLM-optional, in that order, on purpose. The
    heuristic runs on every message and costs nothing, so the common case — a
    lane explicitly stating a root cause — is caught without a model call. The
    LLM refines only the cases the heuristic flagged as *possible*, which keeps
    the expensive path proportional to the interesting traffic rather than to all
    of it.
    """

    def __init__(self, *, model: str = "", min_length: int = 60) -> None:
        self.model = model
        self.min_length = min_length

    def classify(
        self, text: str, *, source_lane: str = "", source_harness: str = ""
    ) -> Classification:
        """Heuristic pass. Never raises; a classifier failure must not stop a lane."""
        cleaned = (text or "").strip()
        if len(cleaned) < self.min_length:
            return Classification(False, reason="too short to carry a lesson")

        lowered = cleaned.casefold()
        hit = next((phrase for phrase in _SIGNAL_PHRASES if phrase in lowered), "")
        if not hit:
            return Classification(False, reason="no lesson signal phrase")

        title, body = _condense(cleaned)
        return Classification(
            True,
            candidate=LessonCandidate(
                title=title,
                body=body,
                trigger=hit,
                scope=LessonScope.SESSION,
                ttl_days=7,
                source_lane=source_lane,
                source_harness=source_harness,
                promoted_by="classifier",
                confidence=SESSION_CONFIDENCE,
                tags=["classifier", hit.replace(" ", "-")],
            ),
            reason=f"matched {hit!r}",
        )

    async def refine(self, classification: Classification) -> Classification:
        """Optional LLM pass over a heuristic hit.

        Returns the input unchanged when no model is configured or the call
        fails. Degrading to the heuristic is correct here: the heuristic already
        decided this text is *probably* a lesson, so a failed refinement costs
        precision, not coverage.
        """
        if not classification.is_lesson or not self.model or classification.candidate is None:
            return classification

        try:
            import litellm
        except ImportError:
            return Classification(
                classification.is_lesson,
                classification.candidate,
                classification.reason + "; litellm not installed, kept heuristic",
                "heuristic",
            )

        prompt = (
            "Rewrite this engineering lesson as strict JSON with keys "
            "'title' (<=90 chars, the claim itself), 'trigger' (what situation "
            "triggers it) and 'remedy' (what to do instead). "
            'If it is not a transferable lesson, return {"is_lesson": false}. '
            f"Text:\n{classification.candidate.body}"
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            raw = response["choices"][0]["message"]["content"]
            parsed = _parse_json_object(raw)
        except Exception as exc:
            log.warning("lesson.refine_failed", error=str(exc), model=self.model)
            return Classification(
                classification.is_lesson,
                classification.candidate,
                classification.reason + f"; refine failed ({type(exc).__name__})",
                "heuristic",
            )

        if not parsed or parsed.get("is_lesson") is False:
            return Classification(False, reason="refinement rejected the candidate", method="llm")

        candidate = classification.candidate
        refined = LessonCandidate(
            title=str(parsed.get("title") or candidate.title)[:200],
            body=candidate.body,
            trigger=str(parsed.get("trigger") or candidate.trigger),
            remedy=str(parsed.get("remedy") or candidate.remedy),
            scope=candidate.scope,
            ttl_days=candidate.ttl_days,
            source_lane=candidate.source_lane,
            source_harness=candidate.source_harness,
            source_task_id=candidate.source_task_id,
            source_message_id=candidate.source_message_id,
            promoted_by="classifier+llm",
            confidence=candidate.confidence,
            tags=[*candidate.tags, "llm-refined"],
        )
        return Classification(True, refined, "refined by " + self.model, "llm")


def _condense(text: str, *, limit: int = 90) -> tuple[str, str]:
    """Split free text into a title and a body.

    The title is the first sentence, because that is where people put the claim
    when they write "turns out X: therefore Y".
    """
    first, separator, _ = text.partition(".")
    if separator and 0 < len(first) <= limit:
        return first.strip(), text.strip()
    if len(text) <= limit:
        return text, text
    return text[:limit].rstrip() + "…", text


def _parse_json_object(raw: str) -> dict | None:
    """Extract the first JSON object from a model response.

    Models wrap JSON in prose and fences no matter how firmly the prompt asks
    them not to, so this tolerates that rather than failing.
    """
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
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
    "DEFAULT_INJECTION_BUDGET",
    "EVICTION_HIT_RATE",
    "EVICTION_MIN_SAMPLES",
    "INJECTION_FLOOR",
    "Classification",
    "EvictionReport",
    "LessonCandidate",
    "LessonClassifier",
    "LessonError",
    "LessonStore",
]
