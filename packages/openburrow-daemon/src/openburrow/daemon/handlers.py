"""Control-plane handlers for the methods the CLI calls.

Before this module existed, the daemon registered 17 methods and the CLI called
40. The other 23 had complete engine code behind them and no way to reach it —
`burrow governance audit` computed the accountability report correctly and then
failed with `no handler for 'governance.audit'`.

The split is deliberate: :mod:`openburrow.daemon.server` owns the process
lifecycle (start, supervise, drain, stop), and this module owns the request
surface. A handler that grows a background task belongs in the server; a handler
that reads a table and returns a dict belongs here.

Three rules are applied uniformly:

**Every read goes through :class:`~openburrow.core.db.repository.Repository` or
one of the two append-only logs.** No handler opens a session and writes SQL.

**Every state change emits a bus event in the same operation.** A step that
changes owner without an event is a step whose change cannot be replayed, and the
append-only log is the only recovery mechanism the daemon has.

**A handler returns JSON-serialisable data, never a model.** The IPC layer
serialises with ``default=str``, which would turn a Pydantic model into a repr
string rather than a structure. ``model_dump(mode="json")`` is called at the
boundary for exactly that reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from openburrow.brain import BrainStore, Candidate, LessonStore
from openburrow.core.db.repository import AuditLog, BusEventLog, Repository
from openburrow.core.errors import BusError, SessionNotFoundError
from openburrow.core.logging import get_logger
from openburrow.core.models import (
    ApprovalRequest,
    BrainEntryType,
    BusMessage,
    Claim,
    ClaimKind,
    Lane,
    Lesson,
    LessonScope,
    MessagePriority,
    Performative,
    Plan,
    PlanStep,
    Session,
    Urgency,
    now,
)
from openburrow.core.paths import repo_id_for
from openburrow.governance import detect_adversarial_intent, detect_poisoned_lesson
from openburrow.radar import (
    ConflictJudge,
    ConflictPrediction,
    ConflictPredictor,
    IntentExtractor,
    Radar,
)
from openburrow.reel import ReelRecorder, export
from openburrow.reel.recorder import LaneCoverage, ReelRecording
from openburrow.reel.timeline import TimelineWriter

if TYPE_CHECKING:
    # Imported for annotations only: `handlers.py` is reachable from the IPC
    # registration path, and a runtime import of the engine or the bus here
    # would drag the whole daemon object graph in behind it.
    from openburrow.core.config.load import ResolvedConfig
    from openburrow.core.db.engine import Database
    from openburrow.core.models import AuditRecord, Delegation
    from openburrow.daemon.bus import EventBus
    from openburrow.daemon.server import Daemon
    from openburrow.daemon.sessions import SessionManager

log = get_logger(__name__)

#: Bus event types that carry a negotiation, counted for the session report.
_NEGOTIATION_PREFIX = "negotiation."
#: Bus event types that count as governance activity in the report.
_GOVERNANCE_PREFIX = "governance."


class Handlers:
    """The daemon's request surface, bound to one running daemon."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    # --- wiring helpers ----------------------------------------------------
    @property
    def database(self) -> Database:
        if self.daemon.database is None:
            raise BusError("daemon is not fully started", context={"phase": "database"})
        return self.daemon.database

    @property
    def bus(self) -> EventBus:
        if self.daemon.bus is None:
            raise BusError("daemon is not fully started", context={"phase": "bus"})
        return self.daemon.bus

    @property
    def sessions(self) -> SessionManager:
        if self.daemon.sessions is None:
            raise BusError("daemon is not fully started", context={"phase": "sessions"})
        return self.daemon.sessions

    @property
    def config(self) -> ResolvedConfig:
        return self.daemon.config

    def _repo_id(self) -> str:
        return repo_id_for(self.config.paths.repo_root)

    async def _session(self, reference: str) -> Session:
        if not reference:
            raise SessionNotFoundError(
                "no session given",
                hint="Pass --session, or start one with `burrow session start`.",
            )
        return await self.sessions.get_session(reference)

    async def _lane_name(self, lane_id: str) -> str:
        if not lane_id:
            return ""
        try:
            async with self.database.session() as db_session:
                lanes = await Repository(db_session).list_by(
                    __import__("openburrow.core.models", fromlist=["Lane"]).Lane, id=lane_id
                )
            return lanes[0].name if lanes else lane_id
        except Exception:
            return lane_id

    # ======================================================================
    # Claims
    # ======================================================================
    async def claim_create(self, params: dict) -> dict:
        """Post a claim, reporting a conflict rather than refusing.

        First claim wins, but a loser is told who holds it and why — the roadmap's
        item 90. A bare rejection would leave the second lane to guess whether to
        wait or negotiate; naming the holder and their stated intent is what makes
        `burrow offer` the obvious next move.

        ``force`` is the admin override (item 102): the existing claim is released
        and this one recorded, with the override carried on the bus event so the
        audit trail shows who took what from whom and why. Admin-only, checked
        against the configured human identity — a force-claim any lane could
        issue would reduce the advisory model to last-write-wins.
        """
        session = await self._session(str(params.get("session", "")))
        lane = await self.sessions.get_lane(session.id, str(params.get("lane", "")))
        resource = str(params.get("resource", "")).strip()
        if not resource:
            raise BusError("claim requires a resource", hint="Pass a file path or glob.")

        force = bool(params.get("force"))
        by = str(params.get("by") or "")
        admin = self.config.settings.governance_human_id or "human"

        kind = ClaimKind(str(params.get("kind") or ClaimKind.FILE))
        claim = (
            Claim.for_step(
                session_id=session.id,
                lane_id=lane.id,
                owner=lane.owner or session.owner,
                step_id=resource,
                intent=str(params.get("intent", "")),
            )
            if kind == ClaimKind.STEP
            else Claim.for_file(
                session_id=session.id,
                lane_id=lane.id,
                owner=lane.owner or session.owner,
                path=resource,
                intent=str(params.get("intent", "")),
                ttl_seconds=int(params.get("ttl_seconds") or 1800),
            )
        )

        conflicts: list[Claim] = []
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            conflicts = await repo.conflicting_claims(session.id, claim)
            if not conflicts:
                await repo.save(claim)
            elif force:
                if by != admin:
                    raise BusError(
                        "only the human owner can force-claim a resource",
                        hint=(
                            f"Force-claiming is admin-only (configured as {admin!r}). "
                            "Use `burrow offer` to negotiate a normal transfer."
                        ),
                        context={"by": by or None, "resource": resource},
                    )
                holder = conflicts[0]
                holder.release(reason=f"force-claimed by {by}: {params.get('reason', '')}")
                await repo.save(holder)
                await repo.save(claim)

        result: dict[str, Any] = {"claim_id": claim.id, "resource": resource}
        if conflicts and force:
            await self.bus.emit(
                event_type="claim.forced",
                session_id=session.id,
                thread_id=session.thread_id,
                lane_id=lane.id,
                priority="blocking",
                summary=(
                    f"{by} force-claimed {resource}, overriding "
                    f"{await self._lane_name(conflicts[0].lane_id)}"
                ),
                payload={
                    "claim_id": claim.id,
                    "resource": resource,
                    "by": by,
                    "reason": str(params.get("reason", "")),
                    "previous_holder": conflicts[0].lane_id,
                },
            )
            result.update({"forced": True, "previous_holder": conflicts[0].lane_id})
        elif conflicts:
            holder = conflicts[0]
            result.update(
                {
                    "conflict": True,
                    "conflict_lane": await self._lane_name(holder.lane_id),
                    "conflict_lane_id": holder.lane_id,
                    "conflict_intent": holder.intent,
                    "conflict_claim_id": holder.id,
                }
            )
        else:
            await self.bus.emit(
                event_type="claim.created",
                session_id=session.id,
                thread_id=session.thread_id,
                lane_id=lane.id,
                summary=f"{lane.name} claimed {resource}",
                payload=claim.model_dump(mode="json"),
            )
        return result

    async def claim_release(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        resource = str(params.get("resource", "")).strip()
        reason = str(params.get("reason") or "released")

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            released = False
            for claim in await repo.active_claims(session.id):
                if claim.resource == resource:
                    claim.release(reason=reason)
                    await repo.save(claim)
                    released = True
                    break

        if released:
            await self.bus.emit(
                event_type="claim.released",
                session_id=session.id,
                thread_id=session.thread_id,
                summary=f"released {resource}: {reason}",
                payload={"resource": resource, "reason": reason},
            )
        return {"released": released, "resource": resource}

    async def claim_list(self, params: dict) -> list[dict]:
        session = await self._session(str(params.get("session", "")))
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            await repo.expire_claims()
            claims = await repo.active_claims(session.id)
        return [c.model_dump(mode="json") for c in claims]

    # ======================================================================
    # Plan
    # ======================================================================
    async def _plan(self, session: Session) -> Plan | None:
        async with self.database.session() as db_session:
            return await Repository(db_session).plan_for_session(session.id)

    async def plan_get(self, params: dict) -> dict | None:
        session = await self._session(str(params.get("session", "")))
        plan = await self._plan(session)
        return plan.model_dump(mode="json") if plan is not None else None

    async def plan_diff(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        version = int(params.get("version") or 1)
        plan = await self._plan(session)
        if plan is None:
            return {"added": [], "removed": [], "changed": []}

        snapshot = next(
            (entry for entry in reversed(plan.history) if entry.get("version") == version),
            None,
        )
        if snapshot is None:
            raise BusError(
                f"plan version {version} is not in this plan's history",
                hint=f"Versions kept: {', '.join(str(e.get('version')) for e in plan.history) or 'none'}.",
                context={"requested": version, "current": plan.version},
            )
        prior = Plan.model_validate({**plan.model_dump(mode="python"), **snapshot})
        prior.steps = [PlanStep.model_validate(s) for s in snapshot.get("steps", [])]
        return plan.diff_against(prior)

    async def plan_generate(self, params: dict) -> dict:
        """LLM fallback plan when the harness gave none (item 120).

        Refuses when a plan already exists — regenerating over a harness's own
        plan would discard work the claiming/handoff machinery already tracks.
        """
        from openburrow.daemon.planner import FallbackPlanner, apply_to_plan

        session = await self._session(str(params.get("session", "")))
        existing = await self._plan(session)
        if existing is not None and existing.steps and not params.get("force"):
            return {
                "generated": False,
                "reason": "session already has a plan",
                "plan_id": existing.id,
                "steps": len(existing.steps),
            }

        settings = self.config.settings
        planner = FallbackPlanner(
            model=settings.llm_planner_model if settings.llm_enabled else "",
            timeout=float(settings.llm_timeout_s),
            max_tokens=settings.llm_max_tokens,
        )
        description = str(params.get("description") or session.description or session.name)
        result = await planner.plan(description=description)
        if result.is_unknown:
            return {"generated": False, "method": result.method, "reason": result.note}

        plan = existing
        if plan is None:
            plan = Plan(session_id=session.id, title=f"{session.name} plan", source="human")
            session.plan_id = plan.id

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            created = apply_to_plan(plan, result, step_factory=PlanStep)
            plan.source = "llm-fallback"
            await repo.save(plan)
            await repo.save(session)
            await BusEventLog(db_session).append(
                event_type="plan.generated",
                session_id=session.id,
                summary=f"fallback plan generated: {len(created)} steps",
                payload={"method": result.method, "note": result.note, "steps": len(created)},
            )
            await db_session.commit()
        return {
            "generated": True,
            "plan_id": plan.id,
            "steps": [s.model_dump(mode="json") for s in created],
            "note": result.note,
        }

    async def plan_add_step(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        title = str(params.get("title", "")).strip()
        depends_on = [str(d) for d in (params.get("depends_on") or []) if str(d).strip()]

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
            if plan is None:
                plan = Plan(
                    session_id=session.id,
                    title=f"{session.name} plan",
                    source="human",
                    source_lane="",
                )
                session.plan_id = plan.id
                await repo.save(session)
            step = PlanStep(title=title, depends_on=depends_on, target_paths=[])
            plan.add_step(step)
            problems = plan.validate_references()
            if problems:
                raise BusError(
                    "that step would make the plan unrunnable",
                    hint="; ".join(problems),
                    context={"problems": problems},
                )
            await repo.save_plan(plan)

        await self.bus.emit(
            event_type="plan.step_added",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=f"step added: {title}",
            payload=step.model_dump(mode="json"),
        )
        return {"id": step.id, "title": step.title, "plan_id": plan.id, "version": plan.version}

    async def plan_remove_step(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        reference = str(params.get("step", ""))
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
            if plan is None:
                return {"removed": False}
            step = _find_step(plan, reference)
            if step is None:
                return {"removed": False}
            removed = plan.remove_step(step.id)
            await repo.save_plan(plan)

        if removed:
            await self.bus.emit(
                event_type="plan.step_removed",
                session_id=session.id,
                thread_id=session.thread_id,
                summary=f"step removed: {step.title}",
                payload={"step_id": step.id, "title": step.title},
            )
        return {"removed": removed, "step_id": step.id if removed else ""}

    async def plan_take(self, params: dict) -> dict:
        """Claim a ready step. First claim wins; an owned step is refused.

        The refusal names the owner and points at `burrow offer`, because the
        distinction between "unowned" and "contested" is the whole reason `take`
        and `offer` are separate commands.
        """
        session = await self._session(str(params.get("session", "")))
        lane = await self.sessions.get_lane(session.id, str(params.get("lane", "")))
        reference = str(params.get("step", ""))

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
            if plan is None:
                raise BusError(
                    "this session has no plan yet",
                    hint="Add a step with `burrow coordination plan edit --add <title>`.",
                    context={"session_id": session.id},
                )
            step = _find_step(plan, reference)
            if step is None:
                raise BusError(
                    f"no plan step matching {reference!r}",
                    hint="Run `burrow coordination plan show` to list steps.",
                    context={"reference": reference},
                )
            if step.owner_lane and step.owner_lane != lane.id:
                owner = await self._lane_name(step.owner_lane)
                raise BusError(
                    f"step {step.title!r} is already owned by {owner}",
                    hint="Use `burrow coordination offer <step> <lane>` to negotiate a transfer.",
                    context={"step_id": step.id, "owner_lane": step.owner_lane},
                )
            step.claim(lane.id)
            step.start()
            await repo.save_plan(plan)

        await self.bus.emit(
            event_type="plan.step_claimed",
            session_id=session.id,
            thread_id=session.thread_id,
            lane_id=lane.id,
            summary=f"{lane.name} took step {step.title!r}",
            payload=step.model_dump(mode="json"),
        )
        return {
            "message": f"claimed {step.title!r}",
            "step_id": step.id,
            "owner_lane": lane.id,
            "status": str(step.status),
        }

    # ======================================================================
    # Handoff
    # ======================================================================
    async def handoff_offer(self, params: dict) -> dict:
        """Run an ACP exchange asking the owning lane to release a step.

        The responder is a heuristic, and that is stated rather than hidden: a
        running lane can answer for itself, and the answer depends on whether it
        is mid-task. An idle lane accepts, a working lane counters with a request
        to wait, a stopped lane declines because it cannot hand over what it is no
        longer doing. A future revision delivers the proposal into the lane's own
        harness; the transcript shape and the escalation path are already correct.
        """
        session = await self._session(str(params.get("session", "")))
        target = await self.sessions.get_lane(session.id, str(params.get("to_lane", "")))
        reference = str(params.get("step", ""))
        note = str(params.get("note", ""))

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
        if plan is None:
            raise BusError("this session has no plan yet", context={"session": session.id})
        step = _find_step(plan, reference)
        if step is None:
            raise BusError(f"no plan step matching {reference!r}", context={"reference": reference})

        from openburrow.acp import NegotiationDriver
        from openburrow.acp.negotiation import ResponderReply

        driver = NegotiationDriver(
            max_exchanges=self.config.bus.max_exchanges_before_escalation,
            escalate_after=max(2, self.config.bus.max_exchanges_before_escalation // 2),
        )

        async def respond(lane_id: str, _message: BusMessage) -> ResponderReply:
            running = next(
                (r for r in self.sessions.running_lanes(session.id) if r.lane.id == lane_id), None
            )
            if running is None:
                return ResponderReply(
                    performative=Performative.REJECT,
                    summary=f"{lane_id} is not running; it cannot release the step",
                    refs=[step.id],
                    engaged=True,
                )
            if running.lane.status.value in {"idle", "watching"}:
                return ResponderReply(
                    performative=Performative.ACCEPT,
                    summary=f"{running.lane.name} hands over {step.title!r}",
                    refs=[step.id],
                    engaged=True,
                )
            return ResponderReply(
                performative=Performative.COUNTER,
                summary=f"{running.lane.name} is mid-task; finish then hand over",
                refs=[step.id],
                requested_change="wait for the current task to complete",
                engaged=True,
            )

        async def deliver(message: BusMessage) -> None:
            await self.bus.emit(
                event_type="negotiation.move",
                session_id=session.id,
                thread_id=session.thread_id,
                lane_id=message.sender_lane,
                summary=f"{message.intent}: {message.body[:120]}",
                payload=message.model_dump(mode="json"),
            )

        async def escalate(message: BusMessage) -> None:
            # Item 138: the exchange cap, a decline, or a rejection-after-N
            # becomes a broadcast the whole session sees, not a private failure
            # between the two lanes. Persisted through the bus like every other
            # negotiation event so replay and the standup see it identically.
            await self.bus.emit(
                event_type="negotiation.escalated",
                session_id=session.id,
                thread_id=session.thread_id,
                summary=message.body[:200],
                payload=message.model_dump(mode="json"),
            )

        result = await driver.run(
            session_id=session.id,
            thread_id=session.thread_id or session.id,
            lane_a=str(params.get("from_lane") or step.owner_lane or target.id),
            lane_b=target.id,
            topic=f"transfer of step {step.title!r}",
            description=note or f"offer of {reference} to {target.name}",
            contested_refs=[step.id],
            opening=note or f"I would like to take over {step.title!r}.",
            respond=respond,
            deliver=deliver,
            escalate=escalate,
        )

        async with self.database.session() as db_session:
            await Repository(db_session).save(result.exchange)

        await self.bus.emit(
            event_type="negotiation.finished",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=f"handoff offer for {step.title!r}: {result.exchange.outcome}",
            payload=result.exchange.model_dump(mode="json"),
        )
        return {
            "agreed": result.agreed,
            "escalated": result.escalated,
            "reason": result.resolution or result.exchange.description,
            "exchanges": result.exchange.exchange_count,
            "exchange_id": result.exchange.id,
        }

    async def handoff_execute(self, params: dict) -> dict:
        """Reassign a step deliberately, with the payload the new owner needs.

        The payload is assembled from records, not from the outgoing lane's
        memory: the plan's status, the lessons live in the session, and how many
        A2A messages touched this step. A lane that starts informed re-derives
        less, which is the point of a handoff rather than a fresh start.
        """
        session = await self._session(str(params.get("session", "")))
        target = await self.sessions.get_lane(session.id, str(params.get("to_lane", "")))
        reference = str(params.get("step", ""))
        reason = str(params.get("reason", ""))
        forced = bool(params.get("force"))

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
            if plan is None:
                raise BusError("this session has no plan yet", context={"session": session.id})
            step = _find_step(plan, reference)
            if step is None:
                raise BusError(
                    f"no plan step matching {reference!r}", context={"reference": reference}
                )
            previous_owner = step.owner_lane
            step.handoff(target.id)
            await repo.save_plan(plan)
            lessons = await LessonStore(self.database, repo_id=self._repo_id()).live(
                session_id=session.id
            )
            history = await repo.count(BusMessage, session_id=session.id)

        await self.bus.emit(
            event_type="handoff.executed",
            session_id=session.id,
            thread_id=session.thread_id,
            lane_id=target.id,
            summary=(
                f"step {step.title!r} handed from {previous_owner or 'unowned'} to {target.name}"
                + (" (forced)" if forced else "")
            ),
            payload={
                "step_id": step.id,
                "from_lane": previous_owner,
                "to_lane": target.id,
                "reason": reason,
                "forced": forced,
            },
        )
        return {
            "step_id": step.id,
            "to_lane": target.id,
            "from_lane": previous_owner,
            "payload_keys": [
                "branch",
                "plan_status",
                "lessons",
                "a2a_history",
                "step",
            ],
            "lessons": len(lessons),
            "history": history,
            "forced": forced,
        }

    async def handoff_assign(self, params: dict) -> dict:
        """Assign a step to a teammate with no harness at all (item 100).

        A handoff to a human is deliberately *not* a lane operation: there is no
        process to restart and no worktree to create. The step's ownership moves
        to a person, the A2A task for it is cancelled so no agent picks it up,
        and the bus records who now owes the work. That is the whole mechanism —
        anything fancier would imply the platform can page a human, which it
        cannot; notifying them is the notifier's job, not the handoff's.
        """
        session = await self._session(str(params.get("session", "")))
        teammate = str(params.get("teammate", "")).strip()
        reference = str(params.get("step", ""))
        reason = str(params.get("reason", ""))
        if not teammate:
            raise BusError(
                "no teammate given",
                hint="Pass the teammate's subject (email or handle) with --teammate.",
            )

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            plan = await repo.plan_for_session(session.id)
            if plan is None:
                raise BusError("this session has no plan yet", context={"session": session.id})
            step = _find_step(plan, reference)
            if step is None:
                raise BusError(
                    f"no plan step matching {reference!r}", context={"reference": reference}
                )
            previous_owner = step.owner_lane
            # The step model's own handoff(): preserves original_owner_lane so
            # the audit trail still ends at whoever claimed the work first — a
            # reassignment is not a re-origination.
            step.handoff(f"human:{teammate}")
            await repo.save_plan(plan)

        # Cancel any live A2A task for this step: a human-owned step must not
        # stay broadcast as agent-claimable work.
        task = await self.sessions.fetch_task(step.id)
        if task is not None:
            await self.sessions.cancel_task(task.id, reason=f"handed to human {teammate}")

        await self.bus.emit(
            event_type="handoff.assigned_human",
            session_id=session.id,
            thread_id=session.thread_id,
            summary=f"step {step.title!r} assigned to teammate {teammate} (no harness)",
            payload={
                "step_id": step.id,
                "from_lane": previous_owner,
                "teammate": teammate,
                "reason": reason,
            },
        )
        return {
            "step_id": step.id,
            "assigned_to": f"human:{teammate}",
            "from_lane": previous_owner,
            "task_cancelled": task is not None,
        }

    # ======================================================================
    # Governance
    # ======================================================================
    async def governance_audit(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        violations_only = bool(params.get("violations_only"))
        strict_only = bool(params.get("strict_only"))

        async with self.database.session() as db_session:
            audit = AuditLog(db_session)
            report = await audit.accountability_report(session.id)
            records = await audit.for_session(
                session.id, violations_only=violations_only, strict_only=strict_only
            )
        return {"report": report, "records": records}

    async def governance_delegation_chain(self, params: dict) -> dict:
        delegation_id = str(params.get("delegation", ""))
        if not delegation_id:
            raise BusError("no delegation id given", hint="Pass --delegation <id>.")
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            audit = AuditLog(db_session)
            chain = await repo.delegation_chain(delegation_id)
            if not chain:
                return {
                    "report": f"no delegation chain found for {delegation_id}",
                    "chain": [],
                }
            report = await _render_chain(chain, delegation_id)
            for hop in chain:
                await audit.record(_chain_record(hop))
        return {"report": report, "chain": [d.model_dump(mode="json") for d in chain]}

    async def governance_scorecard(self, params: dict) -> dict:
        """ "Attacks caught" — the item-207 panel, as data.

        Every family of attack the roadmap names (items 188-190, 205-206) maps
        onto a detector whose flags land in the bus log as ``governance.flag``
        events. This reads those events back and reports, per family, what was
        seen and whether it was refused — the honest version of a scorecard that
        only ever shows green. A family with no flags reports zero rather than
        being omitted, because an attack surface nobody probed looks identical to
        one nobody watched.
        """
        session = await self._session(str(params.get("session", "")))
        async with self.database.session() as db_session:
            events = await BusEventLog(db_session).stream(session_id=session.id, limit=100_000)

        families = {
            "prompt_injection": "prompt injection in a message or lesson",
            "poisoned_lesson": "a lesson crafted to steer another agent",
            "adversarial_intent": "stated intent hiding a contested touch",
            "impersonation": "a sender claiming to be another lane",
            "capability_mismatch": "declared skills not matching behaviour",
            "authority_creep": "delegation exceeding granted scope",
        }
        caught: dict[str, dict[str, Any]] = {}
        for name, description in families.items():
            caught[name] = {"description": description, "flags": 0, "refused": 0, "events": []}

        refused_events = {
            "governance.policy_denied",
            "lesson.promotion_refused",
            "delegation.rejected",
            "approval.denied",
        }
        for event in events:
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") or {}
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            kinds: list[str] = []
            if isinstance(detail, dict):
                kinds = [str(k) for k in (detail.get("kinds") or []) if str(k) in families]
                single = str(detail.get("kind") or "")
                if single in families and single not in kinds:
                    kinds.append(single)
            if event_type == "governance.flag":
                for kind in kinds or ["prompt_injection"]:
                    caught[kind]["flags"] += 1
                    caught[kind]["events"].append(str(event.get("summary") or "")[:160])
            elif event_type in refused_events and kinds:
                for kind in kinds:
                    caught[kind]["refused"] += 1

        total_flags = sum(f["flags"] for f in caught.values())
        total_refused = sum(f["refused"] for f in caught.values())
        return {
            "session_id": session.id,
            "families": caught,
            "totals": {"flags": total_flags, "refused": total_refused},
            "note": (
                "flags are detections; refusals are enforcement actions. "
                "A flag without a refusal is visible, not necessarily stopped."
            ),
        }

    # ======================================================================
    # Approvals
    # ======================================================================
    async def approvals_list(self, params: dict) -> list[dict]:
        session_id = str(params.get("session", ""))
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            for stale in await repo.expired_approvals():
                stale.time_out()
                await repo.save(stale)
            if session_id:
                session = await self._session(session_id)
                pending = await repo.pending_approvals(session.id)
            else:
                pending = await repo.list_by(ApprovalRequest, status="pending")
        return [a.model_dump(mode="json") for a in pending]

    async def approvals_respond(self, params: dict) -> dict:
        approval_id = str(params.get("approval", ""))
        deny = bool(params.get("deny"))
        edited = str(params.get("edited_action") or "")
        note = str(params.get("note") or "")
        by = str(params.get("by") or self.config.settings.governance_human_id or "human")

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            request = await repo.get(ApprovalRequest, approval_id)
            if request is None:
                raise BusError(
                    f"no approval {approval_id!r}",
                    hint="List pending approvals with `burrow governance approvals list`.",
                    context={"approval": approval_id},
                )
            if request.status.value != "pending":
                raise BusError(
                    f"approval {approval_id!r} was already {request.status}",
                    context={"approval": approval_id, "status": str(request.status)},
                )
            if deny:
                request.deny(by=by, note=note)
            else:
                request.approve(by=by, note=note, edited_action=edited)
            await repo.save(request)

        await self.bus.emit(
            event_type="approval.resolved",
            session_id=request.session_id,
            lane_id=request.lane_id,
            summary=f"approval {request.id} {request.status} by {by}",
            payload=request.model_dump(mode="json"),
        )

        delivery = await self._deliver_approval(request, deny=deny, by=by)

        return {
            "status": str(request.status),
            "edited": request.was_edited,
            "effective_action": request.effective_action,
            "responded_by": by,
            **delivery,
        }

    async def _deliver_approval(
        self, request: ApprovalRequest, *, deny: bool, by: str
    ) -> dict[str, Any]:
        """Push the approval decision into the owning harness (item 176).

        Returns ``{delivered_to_harness, delivery_error?}`` rather than raising:
        the human has already decided, and a broken pipe must not erase that.
        An edited approval injects the *edited* text — feeding the original
        back after a human rewrote it would make the edit decorative.
        """
        delivered = False
        delivery_error = ""
        if not deny and request.lane_id:
            running = next(
                (
                    r
                    for r in self.sessions.running_lanes(request.session_id)
                    if r.lane.id == request.lane_id
                ),
                None,
            )
            if running is not None:
                instruction = (
                    f"Approval {request.id} was approved with edits by {by}. "
                    f"Run this instead of the original request:\n{request.effective_action}"
                    if request.was_edited
                    else f"Approval {request.id} was approved by {by}. Proceed: {request.action}"
                )
                try:
                    delivered = await running.adapter.inject_message(
                        BusMessage(
                            session_id=request.session_id,
                            sender_lane="human",
                            sender_human=by,
                            intent=Performative.INFORM,
                            priority=MessagePriority.BLOCKING,
                            urgency=Urgency.HIGH,
                            subject=f"approval {request.id} {request.status.value}",
                            body=instruction,
                            requires_reply=False,
                        ),
                        await self.sessions.fetch_task(request.task_id)
                        if request.task_id
                        else None,
                    )
                    if not delivered:
                        delivery_error = "adapter could not accept an injection"
                except Exception as exc:
                    delivery_error = str(exc)
            else:
                delivery_error = "lane is not running"
        return {
            "delivered_to_harness": delivered,
            **({"delivery_error": delivery_error} if delivery_error else {}),
        }

    # ======================================================================
    # Brain
    # ======================================================================
    def _brain(self) -> BrainStore:
        return BrainStore(
            self.database,
            repo_id=self._repo_id(),
            repo_root=str(self.config.paths.repo_root),
        )

    async def brain_list(self, params: dict) -> list[dict]:
        entries = await self._brain().list_entries(
            active_only=bool(params.get("active_only", True)),
            path=str(params.get("path") or "") or None,
        )
        return [e.model_dump(mode="json") for e in entries]

    async def brain_add(self, params: dict) -> dict:
        raw_type = str(params.get("entry_type") or BrainEntryType.CONVENTION)
        try:
            entry_type = BrainEntryType(raw_type)
        except ValueError as exc:
            raise BusError(
                f"unknown Brain entry type {raw_type!r}",
                hint="Use one of: decision, gotcha, convention.",
                context={"entry_type": raw_type},
            ) from exc

        candidate = Candidate(
            title=str(params.get("title", "")),
            body=str(params.get("body", "")),
            entry_type=entry_type,
            anchor_path=str(params.get("anchor_path", "")),
            anchor_symbol=str(params.get("anchor_symbol", "")),
            anchor_commit=str(params.get("anchor_commit", "")),
            source_lane=str(params.get("source_lane", "")),
            source_harness=str(params.get("source_harness", "")),
            source_message_id=str(params.get("source_message_id", "")),
            promoted_by=str(params.get("promoted_by") or "human"),
            tags=list(params.get("tags") or []),
        )
        result = await self._brain().promote(candidate)
        entry = result.entry
        if params.get("confirmed_by"):
            entry = await self._brain().confirm(entry.id, by=str(params["confirmed_by"]))

        await self.bus.emit(
            event_type="brain.entry_promoted",
            session_id=entry.session_id,
            summary=f"Brain entry: {entry.title}",
            payload=entry.model_dump(mode="json"),
        )
        return {
            "id": entry.id,
            "created": result.created,
            "corroborated": result.corroborated,
            "reason": result.reason,
            "confidence": entry.confidence,
        }

    async def brain_retire(self, params: dict) -> dict:
        entry_id = str(params.get("entry", ""))
        entry = await self._brain().retire(entry_id, reason=str(params.get("reason", "")))
        await self.bus.emit(
            event_type="brain.entry_retired",
            summary=f"Brain entry retired: {entry.title}",
            payload={"id": entry.id, "reason": entry.retired_reason},
        )
        return {"id": entry.id, "status": str(entry.status), "reason": entry.retired_reason}

    async def brain_diff(self, params: dict) -> dict:
        session = await self._session(str(params.get("session", "")))
        store = self._brain()
        stale = await store.check_anchors()
        async with self.database.session() as db_session:
            log_reader = BusEventLog(db_session)
            events = await log_reader.stream(session_id=session.id, limit=10_000)

        added: list[str] = []
        retired: list[str] = []
        for event in events:
            if event["event_type"] == "brain.entry_promoted":
                added.append(str(event.get("summary") or ""))
            elif event["event_type"] == "brain.entry_retired":
                retired.append(str(event.get("summary") or ""))
        return {
            "added": added,
            "stale": [f"{e.title} ({e.retired_reason or 'anchor superseded'})" for e in stale],
            "retired": retired,
        }

    async def brain_refresh_stale(self, params: dict) -> dict:
        """Sweep for stale entries and ask a nearby lane to rewrite them (item 112).

        A stale entry is a claim whose evidence expired, not a claim that is
        wrong — so the right consumer is a lane currently working on that file,
        which can re-check the fact in the same breath as its own change. The
        prompt is a question, not an instruction: the lane is asked to confirm or
        rewrite, and the result goes through ``brain.add`` like any other
        candidate, so corroboration rules still apply.

        Delivered through ``inject_message`` like every other inbound message, so
        a harness with no input hook is reported rather than pretended-to. With
        no running lane near the file, the sweep result is still returned — the
        staleness is real even when nobody is around to act on it.
        """
        session = await self._session(str(params.get("session", "")))
        store = self._brain()
        stale = await store.check_anchors()
        # Entries an earlier sweep already marked stale are still waiting for
        # their re-check — excluding them would make the prompt disappear for
        # good the first time it went undelivered, which is the opposite of
        # what a fix-it sweep is for.
        if not stale:
            entries = await store.list_entries(active_only=False)
            stale = [
                e for e in entries if e.status.value == "stale" and "rewrite-prompted" not in e.tags
            ]

        async with self.database.session() as db_session:
            lanes = await Repository(db_session).lanes_for_session(session.id)

        prompts: list[dict[str, Any]] = []
        for entry in stale:
            path = entry.anchor_path
            nearby = [
                lane
                for lane in lanes
                if lane.status.value in {"idle", "working"}
                and (
                    path in {str(c) for c in (lane.metadata.get("claims") or [])}
                    or any(path in str(step) for step in (lane.metadata.get("tool_calls") or []))
                )
            ]
            target = (
                nearby[0]
                if nearby
                else next((lane for lane in lanes if lane.status.value == "idle"), None)
            )
            prompt = {
                "entry_id": entry.id,
                "title": entry.title,
                "anchor_path": path,
                "reason": entry.retired_reason or "anchor superseded",
                "target_lane": target.id if target else "",
                "delivered": False,
            }
            if target is not None:
                running = next(
                    (r for r in self.sessions.running_lanes(session.id) if r.lane.id == target.id),
                    None,
                )
                if running is not None:
                    message = BusMessage(
                        session_id=session.id,
                        thread_id=session.thread_id or session.id,
                        sender_lane="",
                        intent=Performative.INFORM,
                        subject=f"brain entry needs a re-check: {entry.title}",
                        body=(
                            f"A recorded fact about {path} may be out of date: "
                            f'"{entry.title}" — {entry.body}\n'
                            f"Reason: {prompt['reason']}. You are working near this file. "
                            "If the fact still holds, confirm it; if it changed, "
                            "post the corrected version with `burrow knowledge brain add` "
                            "and retire the old entry."
                        ),
                        requires_reply=False,
                        payload={
                            "openburrow:brainEntryId": entry.id,
                            "openburrow:anchorPath": path,
                        },
                    )
                    try:
                        prompt["delivered"] = await running.adapter.inject_message(message)
                    except Exception as exc:
                        prompt["error"] = str(exc)
                    if prompt["delivered"]:
                        # Tagged so the next sweep does not re-ask. Tags are the
                        # only mutable scratch space the model carries; a
                        # dedicated column would be a migration for one bit.
                        entry.tags = sorted({*entry.tags, "rewrite-prompted"})
                        await store._save(entry)
                        await self.bus.emit(
                            event_type="brain.stale_prompt",
                            session_id=session.id,
                            thread_id=session.thread_id,
                            lane_id=target.id,
                            summary=f"asked {target.name} to re-check '{entry.title}'",
                            payload={"entry_id": entry.id, "anchor_path": path},
                        )
            prompts.append(prompt)

        return {"stale": len(stale), "prompts": prompts}

    # ======================================================================
    # Lessons
    # ======================================================================
    def _lessons(self) -> LessonStore:
        return LessonStore(self.database, repo_id=self._repo_id())

    async def lessons_list(self, params: dict) -> list[dict]:
        session = await self._session(str(params.get("session", "")))
        scope = str(params.get("scope") or "")
        lessons = await self._lessons().live(session_id=session.id)
        if scope:
            lessons = [lesson for lesson in lessons if str(lesson.scope) == scope]
        return [lesson.model_dump(mode="json") for lesson in lessons]

    async def lessons_retire(self, params: dict) -> dict:
        lesson_id = str(params.get("lesson", ""))
        lesson = await self._lessons().retire(lesson_id, reason=str(params.get("reason", "")))
        await self.bus.emit(
            event_type="lesson.retired",
            session_id=lesson.session_id,
            summary=f"lesson retired: {lesson.title}",
            payload={"id": lesson.id, "reason": lesson.retired_reason},
        )
        return {"id": lesson.id, "retired": True, "reason": lesson.retired_reason}

    async def lessons_promote(self, params: dict) -> dict:
        """Promote text to a lesson, with the poisoned-lesson check in front.

        The detector runs before the store sees the candidate because a lesson is
        a prompt fragment injected into every lane's context — the highest-value
        injection target in the system. Two independent checks on that path is the
        design, not belt-and-braces.
        """
        session = await self._session(str(params.get("session", "")))
        title = str(params.get("title", "")).strip()
        body = str(params.get("body", "")).strip()
        if not title:
            # `Lesson` rejects an empty title with a ValueError, and a pydantic
            # traceback is not an answer. Say what is missing instead.
            raise BusError(
                "a lesson needs a title",
                hint="One line naming the problem, e.g. 'pytest hangs on Windows PTYs'.",
            )

        raw_scope = str(params.get("scope") or LessonScope.SESSION)
        try:
            scope = LessonScope(raw_scope)
        except ValueError as exc:
            raise BusError(
                f"unknown lesson scope {raw_scope!r}",
                hint="Use one of: session, repo, org.",
            ) from exc

        from openburrow.brain import LessonCandidate

        candidate = LessonCandidate(
            title=title,
            body=body,
            trigger=str(params.get("trigger", "")),
            remedy=str(params.get("remedy", "")),
            scope=scope,
            ttl_days=params.get("ttl_days"),
            source_lane=str(params.get("source_lane", "")),
            source_harness=str(params.get("source_harness", "")),
            promoted_by=str(params.get("promoted_by") or "human"),
        )

        # The detector takes a ``Lesson``, not a string: it reads title, body,
        # trigger, remedy and provenance together, and it also applies the
        # low-confidence rule. This call used to be
        # ``detect_poisoned_lesson(f"{title}\n{body}")`` — a ``str`` — so the
        # check standing in front of the highest-value injection target in the
        # system raised ``AttributeError`` the first time it was reached, and
        # nothing noticed because no test ever promoted a lesson.
        detection = detect_poisoned_lesson(
            Lesson(
                session_id=session.id,
                scope=scope,
                title=title,
                body=body,
                trigger=candidate.trigger,
                remedy=candidate.remedy,
                source_lane=candidate.source_lane,
                source_harness=candidate.source_harness,
                promoted_by=candidate.promoted_by,
            )
        )
        if detection.flagged:
            await self.bus.emit(
                event_type="governance.flag",
                session_id=session.id,
                summary=detection.summary,
                payload=detection.detail,
            )
            raise BusError(
                "that lesson looks like a prompt injection and was refused",
                hint=detection.summary,
                context=detection.detail,
            )

        lesson = await self._lessons().promote(candidate, session_id=session.id)
        await self.bus.emit(
            event_type="lesson.promoted",
            session_id=session.id,
            summary=f"lesson: {lesson.title}",
            payload=lesson.model_dump(mode="json"),
        )
        return {"id": lesson.id, "scope": str(lesson.scope), "title": lesson.title}

    # ======================================================================
    # Bus
    # ======================================================================
    async def bus_message(self, params: dict) -> dict:
        """Post a message onto the session thread as a peer participant.

        This is the human-override path (item 50): a teammate types into the live
        thread and the message lands in the same log every lane reads, with the
        same shape. It is deliberately not a special "human" event type — a
        message that renders differently in the feed is a message that gets
        skimmed differently.
        """
        session = await self._session(str(params.get("session", "")))
        body = str(params.get("body", "")).strip()
        if not body:
            raise BusError("message requires a body")

        sender_lane = str(params.get("lane", ""))
        sender_human = str(params.get("sender_human") or "")
        if not sender_lane and not sender_human:
            sender_human = self.config.settings.governance_human_id or "human"

        recipients = [str(r) for r in (params.get("recipients") or []) if str(r)]
        message = BusMessage(
            session_id=session.id,
            thread_id=session.thread_id or session.id,
            sender_lane=sender_lane,
            sender_human=sender_human,
            recipients=recipients,
            broadcast=not recipients,
            subject=str(params.get("subject", "")),
            body=body,
            intent=Performative(str(params.get("intent") or Performative.INFORM)),
            payload=dict(params.get("payload") or {}),
        )

        async with self.database.session() as db_session:
            await Repository(db_session).save(message)

        await self.bus.emit(
            event_type="bus.message.sent",
            session_id=session.id,
            thread_id=message.thread_id,
            lane_id=sender_lane,
            summary=f"{sender_human or sender_lane}: {body[:120]}",
            payload=message.model_dump(mode="json"),
            content_hash=message.content_hash,
        )
        return {
            "id": message.id,
            "delivered": True,
            "recipients": recipients or ["*"],
            "thread_id": message.thread_id,
        }

    # ======================================================================
    # Report
    # ======================================================================
    async def report_generate(self, params: dict) -> dict:
        """End-of-session rollup, honest about what it could not measure.

        ``negotiation_precision`` and ``message_effectiveness`` are the two numbers
        that answer whether the collaboration is real. When a lane's harness
        reports no usage the cost is ``0.0`` and the token count is absent rather
        than estimated — a plausible number in the wrong direction is worse than a
        missing one.
        """
        session = await self._session(str(params.get("session", "")))

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            log_reader = BusEventLog(db_session)
            events = await log_reader.stream(session_id=session.id, limit=100_000)
            lanes = await repo.lanes_for_session(session.id)
            tasks = await repo.open_tasks(session.id)
            delegations = await repo.active_delegations(session.id)

        negotiations = [e for e in events if e["event_type"].startswith(_NEGOTIATION_PREFIX)]
        governance = [e for e in events if e["event_type"].startswith(_GOVERNANCE_PREFIX)]
        messages = [e for e in events if e["event_type"].startswith("bus.message")]
        finished = [
            e
            for e in negotiations
            if str(e.get("payload", {}).get("outcome")) in {"agreed", "rejected", "escalated"}
        ]
        agreed = [e for e in finished if str(e.get("payload", {}).get("outcome")) == "agreed"]
        avoided = sum(1 for e in agreed if bool(e.get("payload", {}).get("collision_avoided")))

        lane_rows = [
            {
                "lane_id": lane.id,
                "name": lane.name,
                "harness": lane.harness,
                "status": str(lane.status),
                "messages_sent": lane.messages_sent,
                "messages_received": lane.messages_received,
                "tasks_completed": len(lane.metadata.get("completed_tasks") or []),
                "tasks_submitted": len(lane.metadata.get("submitted_tasks") or []),
                "cost_usd": lane.cost_usd,
                "tokens_in": lane.tokens_in,
                "tokens_out": lane.tokens_out,
                "crash_count": lane.restarts,
                "message_effectiveness": None,
            }
            for lane in lanes
        ]

        metrics = {
            "duration_s": round(session.duration_seconds, 1),
            "lanes": len(lanes),
            "messages": len(messages),
            "negotiations": len(finished),
            "collisions_avoided": avoided,
            "negotiation_precision": round(avoided / len(finished), 4) if finished else None,
            "lessons": 0,
            "brain_entries": 0,
            "resumes": 0,
            "governance_flags": len(governance),
            "approvals": session.approvals_requested,
            "delegations": len(delegations),
            "open_tasks": len(tasks),
            "cost_usd": round(session.total_cost_usd, 6),
            "tokens": session.total_tokens or None,
        }
        return {
            "session_id": session.id,
            "session_name": session.name,
            "generated_at": now().isoformat(),
            "metrics": metrics,
            "lanes": lane_rows,
            "negotiations": [e.get("payload", {}) for e in negotiations[-20:]],
        }

    # ======================================================================
    # Reel export
    # ======================================================================
    async def reel_export(self, params: dict) -> dict:
        """Write a replay bundle and report what went into it.

        The recording is rebuilt from the append-only log rather than from any
        live recorder, so an export after the fact is possible — which is the case
        that matters, because the moment you want a reel is after something went
        wrong. Cast files are picked up when they exist and their absence is
        reported per lane instead of being papered over.
        """
        session = await self._session(str(params.get("session", "")))
        requested = str(params.get("output") or "")
        destination = _export_destination(requested, self.config.paths.reels_dir / session.id)

        async with self.database.session() as db_session:
            repo = Repository(db_session)
            lanes = await repo.lanes_for_session(session.id)
            events = await BusEventLog(db_session).stream(session_id=session.id, limit=100_000)
            audit = await AuditLog(db_session).for_session(session.id)

        destination.mkdir(parents=True, exist_ok=True)
        timeline_path = destination / "timeline.jsonl"
        writer = TimelineWriter(timeline_path)
        for event in events:
            writer.record(
                type=str(event.get("event_type") or ""),
                lane_id=str(event.get("lane_id") or ""),
                summary=str(event.get("summary") or ""),
                payload=dict(event.get("payload") or {}),
                seq=int(event.get("seq") or 0),
            )
        writer.close()

        coverage: list[LaneCoverage] = []
        for lane in lanes:
            cast_path = self.config.paths.lane_cast(lane.id)
            coverage.append(
                LaneCoverage(
                    lane_id=lane.id,
                    title=lane.name,
                    cast_path=str(cast_path) if cast_path.exists() else "",
                    has_cast=cast_path.exists(),
                    reason_missing="" if cast_path.exists() else "no cast recorded for this lane",
                )
            )

        recording = ReelRecording(
            session_id=session.id,
            session_name=session.name,
            timeline_path=timeline_path,
            lane_coverage=coverage,
            duration_s=round(session.duration_seconds, 3),
        )

        sign_requested = bool(params.get("sign"))
        share_secret = ""
        if sign_requested:
            from openburrow.reel import new_secret

            share_secret = new_secret()

        manifest, report = export(
            recording,
            directory=destination,
            audit=audit,
            metrics={"messages": len(events), "governance_events": len(audit)},
            total_cost_usd=session.total_cost_usd,
            share_secret=share_secret,
        )

        size = sum(f.stat().st_size for f in destination.rglob("*") if f.is_file())
        await self.bus.emit(
            event_type="reel.exported",
            session_id=session.id,
            summary=f"reel exported to {destination}",
            payload={"path": str(destination), "events": manifest.event_count},
        )
        return {
            "path": str(destination),
            "manifest_id": manifest.id,
            "lane_count": manifest.lane_count,
            "event_count": manifest.event_count,
            "negotiation_count": manifest.negotiation_count,
            "governance_event_count": manifest.governance_event_count,
            "size_bytes": size,
            "redactions": report.total,
            "signed": manifest.signed,
            "viewer_hint": str(destination / "index.html"),
        }

    # ======================================================================
    # Merge Radar
    # ======================================================================
    def _radar(self) -> Radar:
        """The daemon's single Radar, built on first use and kept.

        Held on the daemon rather than constructed per request because the
        object's value *is* its memory: which pairs it has already announced,
        and how often the judge returned something usable. A Radar built per
        scan would re-announce the same collision on every ``burrow radar scan``,
        and its coverage number would describe one instant while reading like it
        described a session — the exact kind of number that ends up in a slide.
        """
        if self.daemon.radar is not None:
            return self.daemon.radar

        judge = self._build_judge()
        self.daemon.radar = Radar(
            judge=judge,
            predictor=ConflictPredictor(
                judge,
                semantic_threshold=float(self.config.settings.radar_confidence_threshold),
            ),
        )
        return self.daemon.radar

    def _build_judge(self) -> ConflictJudge | None:
        """A model judge, but only when a model could actually answer.

        Not "an LLM judge by default". With no provider credentials every
        semantic pair spends a network round trip to come back unknown: slow,
        and informative about nothing. The absence is *reported* by
        :meth:`radar_status` rather than silently rendered as "zero semantic
        conflicts", because those two states mean completely different things.
        """
        settings = self.config.settings
        if not settings.radar_enabled or not settings.llm_enabled:
            return None
        if not any(settings.approved_provider_keys.values()):
            return None
        return ConflictJudge(
            model=settings.llm_judge_model,
            timeout=float(settings.llm_timeout_s),
        )

    def _judge_note(self) -> str:
        """Why the judge is not in play, in one line a human can act on."""
        settings = self.config.settings
        if not settings.radar_enabled:
            return "the Radar is disabled (OPENBURROW_RADAR_ENABLED=false)"
        if not settings.llm_enabled:
            return "LLM calls are disabled (OPENBURROW_LLM_ENABLED=false)"
        if not any(settings.approved_provider_keys.values()):
            return (
                "no provider credentials configured, so only deterministic signals ran "
                "(set an API key, or point OPENBURROW_OLLAMA_API_BASE at a local model)"
            )
        return ""

    async def _refresh_intents(self, session: Session) -> tuple[Radar, list[Lane]]:
        """Rebuild every lane's intent from records, then hand back the Radar.

        Records, not memory. A lane's file set is what its claims and its owned
        plan steps say — re-derived on every scan, which is what makes the
        Radar's ``certain`` signals auditable after the fact. There is no path
        here through which a model can widen a file set.
        """
        radar = self._radar()
        async with self.database.session() as db_session:
            repo = Repository(db_session)
            lanes = await repo.lanes_for_session(session.id)
            claims = await repo.active_claims(session.id)
            plan = await repo.plan_for_session(session.id)
            steps = await repo.steps_for_plan(plan.id) if plan is not None else []

        for lane in lanes:
            radar.set_intent(
                IntentExtractor.merge(
                    IntentExtractor.from_claims(lane.id, claims, session_id=session.id),
                    IntentExtractor.from_plan_steps(lane.id, steps, session_id=session.id),
                )
            )

        # A lane that has left the session must leave the Radar too. Its intent
        # describes work that is no longer happening, and carrying it forward
        # would report collisions against finished lanes — noise, and noise is
        # what gets a Radar muted.
        current = {lane.id for lane in lanes}
        tracked = {i.lane_id for i in radar.intents() if i.session_id == session.id}
        for lane_id in tracked - current:
            radar.remove_lane(lane_id)
        return radar, lanes

    def _judge_state(self, radar: Radar) -> dict:
        """Whether the judge was reachable, and why not when it was not."""
        return {
            "configured": radar.judge is not None,
            "model": radar.judge.model if radar.judge is not None else "",
            "reason": self._judge_note(),
        }

    async def radar_scan(self, params: dict) -> dict:
        """Re-derive intents, scan every pair, and announce what is new.

        Each announcement is a bus event, appended to the log before it is fanned
        out like every other event. A prediction that existed only in this
        response could not be replayed — and the Radar's whole value is that it
        spoke *before* the collision, which is unverifiable unless the speaking
        was recorded.
        """
        session = await self._session(str(params.get("session", "")))
        if not self.config.settings.radar_enabled:
            return {
                "enabled": False,
                "session_id": session.id,
                "reason": "the Radar is disabled (OPENBURROW_RADAR_ENABLED=false)",
                "announced": [],
                "stats": None,
            }

        radar, lanes = await self._refresh_intents(session)
        fresh = await radar.scan()
        for prediction in fresh:
            await self.bus.emit(
                event_type="radar.conflict",
                session_id=session.id,
                thread_id=session.thread_id,
                summary=_prediction_summary(prediction),
                payload=_prediction_payload(prediction),
            )
            await self._check_adversarial_intent(session, prediction, lanes)
        return {
            "enabled": True,
            "session_id": session.id,
            "announced": [_prediction_payload(p) for p in fresh],
            "stats": radar.stats.summary(),
            "judge": self._judge_state(radar),
            "lanes": {lane.id: lane.name for lane in lanes},
        }

    async def radar_intents(self, params: dict) -> dict:
        """What each lane is working on, re-derived from claims and the plan."""
        session = await self._session(str(params.get("session", "")))
        radar, lanes = await self._refresh_intents(session)
        by_lane = {lane.id: lane for lane in lanes}

        intents: list[dict[str, Any]] = []
        for intent in radar.intents():
            if intent.session_id and intent.session_id != session.id:
                continue
            lane = by_lane.get(intent.lane_id)
            intents.append(
                {
                    "lane_id": intent.lane_id,
                    "lane_name": lane.name if lane else "",
                    "status": str(lane.status) if lane else "unrecorded",
                    "files": sorted(intent.files),
                    "directories": sorted(intent.directories),
                    "step_ids": sorted(intent.step_ids),
                    "risk_tier": intent.risk_tier,
                    "source": intent.source,
                    "certain": intent.is_certain,
                    "summary": intent.summary(),
                }
            )
        intents.sort(key=lambda item: str(item["lane_id"]))
        return {
            "session_id": session.id,
            "lanes_tracked": len(intents),
            "intents": intents,
            "judge": self._judge_state(radar),
        }

    async def radar_status(self, params: dict) -> dict:
        """Counters and coverage — the numbers that say whether to trust a scan.

        Not gated on a live session on purpose: the judge's coverage, the scan
        count, and the deduplication memory are properties of the daemon, and
        asking for them should not require lanes to be running.
        """
        radar = self._radar()
        reference = str(params.get("session", ""))
        session_id = ""
        if reference:
            session = await self._session(reference)
            session_id = session.id
            intents = [i for i in radar.intents() if not i.session_id or i.session_id == session_id]
        else:
            intents = radar.intents()

        return {
            "enabled": bool(self.config.settings.radar_enabled),
            "session_id": session_id,
            "stats": radar.stats.summary(),
            "judge": self._judge_state(radar),
            "thresholds": {
                "semantic": radar.predictor.semantic_threshold,
                "scope": radar.predictor.scope_threshold,
            },
            "lanes_tracked": len(intents),
            "intents": [i.summary() for i in sorted(intents, key=lambda i: i.lane_id)],
        }

    async def _check_adversarial_intent(
        self, session: Session, prediction: ConflictPrediction, lanes: list[Lane]
    ) -> None:
        """Item 190: a stated intent that hides a contested touch is a flag.

        The Radar's prediction names the contested files; each lane's recent
        tool-calls (recorded by the output pump in ``sessions.py``) name what it
        actually touched. A lane touching a contested path it never disclosed
        has either made an error or misrepresented its intent — both are worth
        a flag on top of the Radar's own prediction, because the detection is
        about *disclosure*, not about the touch itself.
        """
        contested = list(prediction.files)
        if not contested:
            return
        for lane in lanes:
            if lane.id not in {prediction.lane_a, prediction.lane_b}:
                continue
            calls = [str(c) for c in (lane.metadata.get("tool_calls") or [])]
            if not calls:
                continue
            # `Lane` has no `task_description`. The column of that name exists on
            # `LaneRow`, but nothing writes it and nothing surfaces it, so this
            # read was an AttributeError waiting for the first lane that recorded
            # a tool call. `stated_intent` is the key this comparison is meant to
            # use; nothing populates it yet either, so the disclosure half of the
            # check currently sees an empty string. That makes the detector noisy
            # rather than wrong — ADR 0009 keeps detection on the high-recall
            # side — but it is not yet the comparison the docstring describes.
            stated = str(lane.metadata.get("stated_intent") or "")
            detection = detect_adversarial_intent(
                stated_intent=stated,
                actual_tool_calls=calls,
                contested_refs=contested,
            )
            if not detection.flagged:
                continue
            await self.bus.emit(
                event_type="governance.flag",
                session_id=session.id,
                thread_id=session.thread_id,
                lane_id=lane.id,
                priority="blocking" if detection.blocking else "informative",
                summary=f"adversarial intent ({lane.name}): {detection.summary}",
                payload=detection.detail,
            )

    # ======================================================================
    # Registration
    # ======================================================================
    def registrations(self) -> dict[str, Any]:
        """Method name -> handler. Consumed by the server's register loop."""
        return {
            "claim.create": self.claim_create,
            "claim.release": self.claim_release,
            "claim.list": self.claim_list,
            "plan.get": self.plan_get,
            "plan.diff": self.plan_diff,
            "plan.add_step": self.plan_add_step,
            "plan.remove_step": self.plan_remove_step,
            "plan.generate": self.plan_generate,
            "plan.take": self.plan_take,
            "handoff.offer": self.handoff_offer,
            "handoff.execute": self.handoff_execute,
            "handoff.assign": self.handoff_assign,
            "governance.audit": self.governance_audit,
            "governance.delegation_chain": self.governance_delegation_chain,
            "governance.scorecard": self.governance_scorecard,
            "approvals.list": self.approvals_list,
            "approvals.respond": self.approvals_respond,
            "brain.list": self.brain_list,
            "brain.add": self.brain_add,
            "brain.retire": self.brain_retire,
            "brain.diff": self.brain_diff,
            "lessons.list": self.lessons_list,
            "lessons.retire": self.lessons_retire,
            "lessons.promote": self.lessons_promote,
            "bus.message": self.bus_message,
            "report.generate": self.report_generate,
            "reel.export": self.reel_export,
            "radar.scan": self.radar_scan,
            "radar.intents": self.radar_intents,
            "radar.status": self.radar_status,
            "brain.refresh_stale": self.brain_refresh_stale,
        }


def _prediction_payload(prediction: ConflictPrediction) -> dict[str, Any]:
    """A JSON-safe view of one prediction.

    ``certain`` is carried explicitly rather than left to be inferred from the
    signal name, because a renderer that has to infer it will eventually infer
    wrong — and then a model's guess is drawn with a fact's weight, which is the
    one failure this whole layer exists to avoid.
    """
    payload: dict[str, Any] = {
        "lane_a": prediction.lane_a,
        "lane_b": prediction.lane_b,
        "kind": prediction.kind,
        "signal": prediction.signal.value,
        "severity": prediction.severity.value,
        "certain": prediction.certain,
        "confidence": prediction.confidence,
        "reason": prediction.reason,
        "files": list(prediction.files),
        "recommended_action": prediction.recommended_action,
        "risk": prediction.metadata.get("risk", ""),
    }
    if prediction.judge is not None:
        payload["judge"] = {
            "kind": prediction.judge.kind,
            "confidence": prediction.judge.confidence,
            "method": prediction.judge.method,
            "known": prediction.judge.known,
        }
    return payload


def _prediction_summary(prediction: ConflictPrediction) -> str:
    """One line for the bus log, readable without the payload."""
    marker = "conflict" if prediction.certain else "possible conflict"
    return (
        f"radar: {marker} between {prediction.lane_a} and {prediction.lane_b} — {prediction.reason}"
    )


def _find_step(plan: Plan, reference: str) -> PlanStep | None:
    """Resolve a step by full id, id prefix, or title fragment.

    The CLI accepts "an id or title fragment" in every one of its help strings,
    so the resolution lives here once rather than in each handler — three copies
    of this is how they come to disagree about what a prefix means.
    """
    needle = reference.strip().casefold()
    if not needle:
        return None
    exact = plan.step(reference)
    if exact is not None:
        return exact
    for step in plan.steps:
        if step.id.casefold().startswith(needle):
            return step
    for step in plan.steps:
        if needle in step.title.casefold():
            return step
    return None


async def _render_chain(chain: list, delegation_id: str) -> str:
    """Text rendering of a delegation chain, oldest hop first."""
    lines = [f"Delegation chain for {delegation_id}", ""]
    root = chain[0]
    lines.append(f"  ORIGIN  human: {root.authorized_by}")
    if root.authorized_by_email:
        lines.append(f"          email: {root.authorized_by_email}")
    lines.append("")
    for index, delegation in enumerate(chain):
        indent = "  " * (index + 1)
        lines.append(
            f"{indent}hop {delegation.depth}: "
            f"{delegation.delegator_lane} -> {delegation.delegatee_lane}"
        )
        lines.append(f"{indent}  scope: {delegation.scope.describe()}")
        lines.append(
            f"{indent}  consent: "
            f"{'explicit' if delegation.explicit_consent else 'policy-derived'}"
            f"  transferable: {delegation.transferable}"
        )
        lines.append(f"{indent}  purpose: {delegation.purpose}")
        lines.append(f"{indent}  status: {delegation.status}")
        lines.append("")
    lines.append(f"  Final actor: {chain[-1].delegatee_lane}")
    lines.append(f"  On behalf of: {chain[-1].authorized_by}")
    return "\n".join(lines)


def _chain_record(delegation: Delegation) -> AuditRecord:
    from openburrow.core.models import AuditRecord, AuditSeverity, TrustBoundary

    return AuditRecord(
        session_id=delegation.session_id,
        delegation_id=delegation.id,
        event="delegation.inspected",
        severity=AuditSeverity.INFO,
        trust_boundary=delegation.trust_boundary
        if isinstance(delegation.trust_boundary, TrustBoundary)
        else TrustBoundary(str(delegation.trust_boundary)),
        strict=str(delegation.trust_boundary) not in {"intra_repo", "intra_lane"},
        summary=delegation.authority_line(),
        actor_lane=delegation.delegator_lane,
        on_behalf_of=delegation.authorized_by,
        affected_lanes=[delegation.delegatee_lane],
        allowed=True,
    )


def _export_destination(requested: str, default: Path) -> Path:
    """Resolve the reel export path.

    This exists as a sync helper rather than inline because the handler is
    ``async``: a pathlib operation there is flagged as blocking (ASYNC240), and
    the honest fix is naming the tiny synchronous step, not burying it.
    """
    return Path(requested).expanduser() if requested else default


def _coverage_from_session(_config: ResolvedConfig, session: Session) -> list[LaneCoverage]:
    """Fallback coverage when a session has no lanes on disk."""
    return [
        LaneCoverage(
            lane_id=lane_id,
            title=lane_id,
            has_cast=False,
            reason_missing="lane record unavailable",
        )
        for lane_id in session.lanes
    ]


def recorder_for(config: ResolvedConfig, session: Session) -> ReelRecorder:
    """Build a recorder for a session, used by the daemon when recording is on."""
    return ReelRecorder(
        session_id=session.id,
        session_name=session.name,
        directory=config.paths.reels_dir / session.id,
        enabled=bool(config.settings.reel_enabled),
    )


__all__ = ["Handlers", "recorder_for", "repo_id_for"]
