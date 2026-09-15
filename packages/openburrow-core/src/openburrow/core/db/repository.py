"""Persistence: object CRUD plus the two append-only logs.

:class:`Repository` handles the mutable half — sessions, lanes, claims, plans —
with round-tripping between domain models and their rows. It is deliberately
dumb: no business rules, no cascades, no side effects. Rules live in the service
layer, because a repository that enforces invariants is a repository you cannot
write a focused test for.

:class:`BusEventLog` and :class:`AuditLog` are the immutable half. They only
append and read. Every interesting feature in OpenBurrow reads from these: the
TUI feed, the replay viewer, the metrics rollup, the accountability report. If
you are unsure where a feature should get its data, the answer is usually here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import timedelta
from typing import Any, TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from openburrow.core.db.models import (
    ApprovalRow,
    AuditRow,
    BrainRow,
    BusEventRow,
    CheckpointRow,
    ClaimRow,
    DelegationRow,
    LaneRow,
    LessonRow,
    MessageRow,
    NegotiationRow,
    PlanRow,
    SessionRow,
    StepRow,
    TaskRow,
)
from openburrow.core.errors import BusError
from openburrow.core.logging import get_logger
from openburrow.core.models import (
    A2ATask,
    ApprovalRequest,
    AuditRecord,
    BrainEntry,
    BusMessage,
    Checkpoint,
    Claim,
    Delegation,
    Lane,
    Lesson,
    NegotiationExchange,
    Plan,
    PlanStep,
    Session,
)
from openburrow.core.models.base import BurrowModel, now

log = get_logger(__name__)

#: The domain model type. Bound to BurrowModel, not SQLModel, and the
#: distinction is the whole point of this module: it maps between the two.
#: Every caller passes a domain model — `get(Session, ...)`, `get(Lesson,
#: ...)`, `get(A2ATask, ...)` — and none of them is a SQLModel, so the old
#: bound made every one of those calls unsatisfiable. mypy reported 164
#: errors in this file alone, all downstream of this line.
M = TypeVar("M", bound=BurrowModel)

#: Domain model -> row class.
_MODEL_TO_ROW: dict[type, type[SQLModel]] = {
    Session: SessionRow,
    Lane: LaneRow,
    A2ATask: TaskRow,
    BusMessage: MessageRow,
    NegotiationExchange: NegotiationRow,
    Claim: ClaimRow,
    Plan: PlanRow,
    PlanStep: StepRow,
    BrainEntry: BrainRow,
    Lesson: LessonRow,
    Delegation: DelegationRow,
    ApprovalRequest: ApprovalRow,
    Checkpoint: CheckpointRow,
}


def _to_row_data(model: Any) -> dict[str, Any]:
    """Serialise a domain model into column values.

    Nested structures stay as Python objects and let SQLAlchemy's JSON type
    handle encoding — round-tripping through ``model_dump_json`` here would
    double-encode and produce a string where a list is expected.

    Only *declared* fields survive. ``model_dump`` also emits
    ``@computed_field`` members — ``Session.is_open``, ``Session.lane_count``,
    ``A2ATask.is_open`` and so on — because including them is the entire point of
    a computed field when you are serialising to JSON. They are not columns, and
    a row object rejects them::

        ValueError: "SessionRow" object has no field "is_open"

    This only bit on the update path. ``SessionRow(**data)`` on insert silently
    ignores the extras (pydantic's default ``extra="ignore"``), so a session
    saved cleanly the first time and then failed the instant anything updated it
    — which is exactly what ``burrow session start`` does when it writes the
    status back.

    Filtering on the model's declared fields rather than a hand-maintained
    exclusion list means this keeps working the next time a computed field is
    added, instead of failing again in a different place.
    """
    declared = type(model).model_fields
    data = {key: value for key, value in model.model_dump(mode="python").items() if key in declared}
    data.pop("content_hash", None)
    metadata = data.pop("metadata", None)
    if metadata is not None:
        data["metadata_json"] = metadata
    return data


def _from_row[M: BurrowModel](row: Any, model_cls: type[M]) -> M:
    """Deserialise a row back into a domain model.

    ``M`` is bounded to :class:`BurrowModel` rather than left free. Without the
    bound, ``type[M]`` is "some class" and mypy is right to say it has no
    ``model_validate``; with it, the call is checked against the real signature
    and a row/model mismatch becomes a type error instead of a runtime one.
    """
    data = {key: value for key, value in vars(row).items() if not key.startswith("_")}
    metadata = data.pop("metadata_json", None)
    if metadata is not None:
        data["metadata"] = metadata
    data.pop("seq", None)  # only BusEventRow/AuditRow have it
    return model_cls.model_validate(data)


class Repository:
    """CRUD over the mutable half of the schema."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- generic -----------------------------------------------------------
    async def save(self, model: Any) -> Any:
        """Insert or update, keyed on the model's primary key."""
        row_cls = _MODEL_TO_ROW.get(type(model))
        if row_cls is None:
            raise BusError(
                f"no table mapped for {type(model).__name__}",
                context={"model": type(model).__name__},
            )
        model.touch() if hasattr(model, "touch") else None
        data = _to_row_data(model)

        # A domain model may declare a field that has no column. Dropping it is
        # right — the alternative is a ``ValueError`` from ``setattr`` deep in
        # SQLAlchemy — but dropping it *silently* is how a field stops being
        # persisted for six months without anyone noticing, so say so.
        columns = set(row_cls.model_fields)
        unmapped = sorted(set(data) - columns)
        if unmapped:
            log.debug(
                "repo.unmapped_fields_dropped", model=type(model).__name__, fields=unmapped
            )
            data = {key: value for key, value in data.items() if key in columns}

        existing = await self.session.get(row_cls, model.id)
        if existing is None:
            self.session.add(row_cls(**data))
        else:
            for key, value in data.items():
                setattr(existing, key, value)
            self.session.add(existing)
        await self.session.flush()
        return model

    async def save_many(self, models: Iterable[Any]) -> int:
        count = 0
        for model in models:
            await self.save(model)
            count += 1
        return count

    async def get(self, model_cls: type[M], entity_id: str) -> M | None:
        row_cls = _MODEL_TO_ROW.get(model_cls)
        if row_cls is None:
            return None
        row = await self.session.get(row_cls, entity_id)
        return _from_row(row, model_cls) if row is not None else None

    async def delete(self, model_cls: type[M], entity_id: str) -> bool:
        row_cls = _MODEL_TO_ROW.get(model_cls)
        if row_cls is None:
            return False
        row = await self.session.get(row_cls, entity_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def count(self, model_cls: type[M], **filters: Any) -> int:
        row_cls = _MODEL_TO_ROW.get(model_cls)
        if row_cls is None:
            return 0
        statement = select(func.count()).select_from(row_cls)
        for key, value in filters.items():
            statement = statement.where(getattr(row_cls, key) == value)
        result = await self.session.execute(statement)
        return int(result.scalar() or 0)

    async def list_by(self, model_cls: type[M], **filters: Any) -> list[M]:
        row_cls = _MODEL_TO_ROW.get(model_cls)
        if row_cls is None:
            return []
        statement = select(row_cls)
        for key, value in filters.items():
            statement = statement.where(getattr(row_cls, key) == value)
        result = await self.session.execute(statement)
        return [_from_row(row, model_cls) for row in result.scalars().all()]

    # --- sessions ----------------------------------------------------------
    async def latest_session(self) -> Session | None:
        result = await self.session.execute(
            select(SessionRow).order_by(SessionRow.created_at.desc()).limit(1)  # type: ignore[attr-defined]
        )
        row = result.scalars().first()
        return _from_row(row, Session) if row is not None else None

    async def open_sessions(self) -> list[Session]:
        result = await self.session.execute(
            select(SessionRow)
            .where(SessionRow.status.in_(["created", "active", "paused"]))  # type: ignore[attr-defined]
            .order_by(SessionRow.created_at.desc())  # type: ignore[attr-defined]
        )
        return [_from_row(row, Session) for row in result.scalars().all()]

    async def find_session(self, reference: str) -> Session | None:
        """Resolve a session by full id, id prefix, or name.

        Accepting a prefix is what makes the CLI usable — nobody types a 30-char
        ULID, but everybody can type the first six characters of one.
        """
        exact = await self.get(Session, reference)
        if exact is not None:
            return exact
        result = await self.session.execute(
            select(SessionRow)
            .where(SessionRow.id.startswith(reference))
            .order_by(SessionRow.created_at.desc())  # type: ignore[attr-defined]
            .limit(2)
        )
        matches = result.scalars().all()
        if len(matches) == 1:
            return _from_row(matches[0], Session)
        if len(matches) > 1:
            raise BusError(
                f"session reference {reference!r} is ambiguous ({len(matches)} matches)",
                hint="Use more characters, or the full session id.",
            )
        by_name = await self.session.execute(
            select(SessionRow)
            .where(SessionRow.name == reference)
            .order_by(SessionRow.created_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = by_name.scalars().first()
        return _from_row(row, Session) if row is not None else None

    # --- lanes -------------------------------------------------------------
    async def lanes_for_session(self, session_id: str) -> list[Lane]:
        result = await self.session.execute(
            select(LaneRow).where(LaneRow.session_id == session_id).order_by(LaneRow.created_at)
        )
        return [_from_row(row, Lane) for row in result.scalars().all()]

    async def find_lane(self, session_id: str, reference: str) -> Lane | None:
        lanes = await self.lanes_for_session(session_id)
        for lane in lanes:
            if reference in (lane.id, lane.name):
                return lane
        for lane in lanes:
            if lane.id.startswith(reference):
                return lane
        return None

    async def stale_lanes(self, *, older_than_seconds: int) -> list[Lane]:
        cutoff = now() - timedelta(seconds=older_than_seconds)
        result = await self.session.execute(
            select(LaneRow)
            .where(LaneRow.status.notin_(["stopped", "crashed"]))  # type: ignore[attr-defined]
            .where(
                # Both codes are needed. `operator` covers the `|` between two
                # column comparisons that mypy has already widened to `bool`;
                # `union-attr` covers `.is_()` on a column mypy types as
                # `datetime | None`. The comment used to name only `operator`,
                # so the ignore did not suppress the error it was written for
                # and mypy said so in a note nobody was reading.
                (LaneRow.last_heartbeat.is_(None)) | (LaneRow.last_heartbeat < cutoff)  # type: ignore[operator, union-attr]
            )
        )
        return [_from_row(row, Lane) for row in result.scalars().all()]

    # --- tasks -------------------------------------------------------------
    async def open_tasks(self, session_id: str) -> list[A2ATask]:
        result = await self.session.execute(
            select(TaskRow)
            .where(TaskRow.session_id == session_id)
            .where(
                TaskRow.state.notin_(["completed", "failed", "canceled", "rejected"])  # type: ignore[attr-defined]
            )
            .order_by(TaskRow.submitted_at)
        )
        return [_from_row(row, A2ATask) for row in result.scalars().all()]

    async def blocking_tasks(self, session_id: str) -> list[A2ATask]:
        result = await self.session.execute(
            select(TaskRow)
            .where(TaskRow.session_id == session_id)
            .where(TaskRow.state.in_(["input_required", "auth_required"]))  # type: ignore[attr-defined]
        )
        return [_from_row(row, A2ATask) for row in result.scalars().all()]

    async def task_chain(self, task_id: str) -> list[A2ATask]:
        """Walk ``parent_task_id`` back to the root — the delegation chain of work."""
        chain: list[A2ATask] = []
        current = await self.get(A2ATask, task_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            if not current.parent_task_id:
                break
            current = await self.get(A2ATask, current.parent_task_id)
        return list(reversed(chain))

    # --- claims ------------------------------------------------------------
    async def active_claims(self, session_id: str) -> list[Claim]:
        result = await self.session.execute(
            select(ClaimRow)
            .where(ClaimRow.session_id == session_id)
            .where(ClaimRow.status == "active")
        )
        return [_from_row(row, Claim) for row in result.scalars().all()]

    async def conflicting_claims(self, session_id: str, claim: Claim) -> list[Claim]:
        """Active claims (from other lanes) that overlap ``claim``."""
        return [
            other
            for other in await self.active_claims(session_id)
            if other.id != claim.id and other.lane_id != claim.lane_id and claim.overlaps(other)
        ]

    async def expire_claims(self) -> int:
        result = await self.session.execute(
            select(ClaimRow)
            .where(ClaimRow.status == "active")
            .where(ClaimRow.expires_at.is_not(None))
            .where(ClaimRow.expires_at < now())  # type: ignore[operator]
        )
        rows = result.scalars().all()
        for row in rows:
            row.status = "expired"
            row.released_at = now()
            row.released_reason = "expired"
            self.session.add(row)
        if rows:
            await self.session.flush()
        return len(rows)

    # --- plans -------------------------------------------------------------
    async def plan_for_session(self, session_id: str) -> Plan | None:
        result = await self.session.execute(
            select(PlanRow)
            .where(PlanRow.session_id == session_id)
            .order_by(PlanRow.version.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return None
        plan = _from_row(row, Plan)
        plan.steps = await self.steps_for_plan(plan.id)
        return plan

    async def steps_for_plan(self, plan_id: str) -> list[PlanStep]:
        result = await self.session.execute(
            select(StepRow).where(StepRow.plan_id == plan_id).order_by(StepRow.order)
        )
        return [_from_row(row, PlanStep) for row in result.scalars().all()]

    async def save_plan(self, plan: Plan) -> Plan:
        await self.save(plan)
        await self.save_many(plan.steps)
        return plan

    # --- governance --------------------------------------------------------
    async def delegation_chain(self, delegation_id: str) -> list[Delegation]:
        """Reconstruct the full chain from the originating human to the acting agent."""
        chain: list[Delegation] = []
        current = await self.get(Delegation, delegation_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            if not current.parent_delegation_id:
                break
            current = await self.get(Delegation, current.parent_delegation_id)
        return list(reversed(chain))

    async def active_delegations(self, session_id: str) -> list[Delegation]:
        return await self.list_by(Delegation, session_id=session_id, status="active")

    async def pending_approvals(self, session_id: str) -> list[ApprovalRequest]:
        return await self.list_by(ApprovalRequest, session_id=session_id, status="pending")

    async def expired_approvals(self) -> list[ApprovalRequest]:
        result = await self.session.execute(
            select(ApprovalRow)
            .where(ApprovalRow.status == "pending")
            .where(ApprovalRow.expires_at.is_not(None))
            .where(ApprovalRow.expires_at < now())  # type: ignore[operator]
        )
        return [_from_row(row, ApprovalRequest) for row in result.scalars().all()]

    # --- knowledge ---------------------------------------------------------
    async def brain_entries(
        self,
        *,
        repo_id: str,
        path: str | None = None,
        active_only: bool = True,
    ) -> list[BrainEntry]:
        statement = select(BrainRow).where(BrainRow.repo_id == repo_id)
        if active_only:
            statement = statement.where(BrainRow.status == "active")
        if path:
            statement = statement.where(BrainRow.anchor_path == path)
        result = await self.session.execute(statement.order_by(BrainRow.created_at.desc()))  # type: ignore[attr-defined]
        return [_from_row(row, BrainEntry) for row in result.scalars().all()]

    async def live_lessons(self, *, session_id: str, repo_id: str = "") -> list[Lesson]:
        result = await self.session.execute(
            select(LessonRow)
            .where(LessonRow.retired_at.is_(None))
            .where(
                (LessonRow.session_id == session_id)
                | (LessonRow.repo_id == repo_id if repo_id else LessonRow.id.is_(None))
            )
            .where(
                (LessonRow.expires_at.is_(None)) | (LessonRow.expires_at > now())  # type: ignore[operator]
            )
            .order_by(LessonRow.created_at.desc())  # type: ignore[attr-defined]
        )
        return [_from_row(row, Lesson) for row in result.scalars().all()]

    # --- checkpoints -------------------------------------------------------
    async def latest_checkpoint(self, lane_id: str) -> Checkpoint | None:
        result = await self.session.execute(
            select(CheckpointRow)
            .where(CheckpointRow.lane_id == lane_id)
            .order_by(CheckpointRow.sequence.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = result.scalars().first()
        return _from_row(row, Checkpoint) if row is not None else None

    async def unconsumed_checkpoints(self, session_id: str) -> list[Checkpoint]:
        result = await self.session.execute(
            select(CheckpointRow)
            .where(CheckpointRow.session_id == session_id)
            .where(CheckpointRow.consumed_at.is_(None))
            .order_by(CheckpointRow.sequence)
        )
        return [_from_row(row, Checkpoint) for row in result.scalars().all()]

    # --- retention ---------------------------------------------------------
    async def prune(self, *, retention_days: int) -> dict[str, int]:
        """Delete old bus/audit events and finished sessions.

        Never touches governance records while any session is still open, and
        never prunes the audit log below the retention floor — the audit trail
        is the one thing that must outlive the work it describes.
        """
        if retention_days <= 0:
            return {}
        cutoff = now() - timedelta(days=retention_days)
        removed: dict[str, int] = {}

        bus_result = await self.session.execute(
            delete(BusEventRow).where(BusEventRow.created_at < cutoff)  # type: ignore[arg-type]
        )
        removed["bus_events"] = int(bus_result.rowcount or 0)  # type: ignore[attr-defined]

        for name, row_cls in (
            ("sessions", SessionRow),
            ("lanes", LaneRow),
            ("messages", MessageRow),
            ("checkpoints", CheckpointRow),
        ):
            result = await self.session.execute(
                delete(row_cls).where(row_cls.created_at < cutoff)  # type: ignore[arg-type]
            )
            removed[name] = int(result.rowcount or 0)  # type: ignore[attr-defined]

        log.info("db.pruned", retention_days=retention_days, removed=removed)
        return removed


# ---------------------------------------------------------------------------
# Append-only logs
# ---------------------------------------------------------------------------
class BusEventLog:
    """The append-only bus log.

    This is the canonical record. Everything that displays, replays, or audits a
    session reads from here, which is why there is exactly one writer path
    (:meth:`append`) and no update path at all.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- session ownership -------------------------------------------------
    async def __aenter__(self) -> BusEventLog:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Commit on success, roll back on failure, and always close.

        This class is used as ``async with factory() as log`` — the factory
        hands back a fresh session per write, so somebody has to end that
        session's transaction. Without these two methods the protocol is simply
        absent and ``async with`` raises ``TypeError``; the callers caught that
        and logged it at debug level, so the canonical write path was dead and
        silent. ``append`` only ever flushed, never committed, so even a call
        that got past the protocol would have discarded its row on close.

        Constructing a ``BusEventLog`` directly — as the read path does inside
        an outer ``async with database.session()`` — does not invoke either
        method, which is correct: whoever owns the session owns the transaction.
        """
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()

    async def append(
        self,
        *,
        event_type: str,
        session_id: str = "",
        thread_id: str = "",
        lane_id: str = "",
        task_id: str = "",
        task_state: str = "",
        priority: str = "informative",
        trust_boundary: str = "intra_repo",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        correlation_id: str = "",
        content_hash: str = "",
    ) -> int:
        """Append one event. Returns the assigned ``seq``.

        A ``content_hash`` match against the last few events is rejected as a
        duplicate — that is the mechanism behind ``A2A_DEDUPE_WINDOW_S`` and it
        is what stops a retrying lane from flooding the log with identical rows.
        """
        if content_hash:
            duplicate = await self.session.execute(
                select(BusEventRow.seq)
                .where(BusEventRow.content_hash == content_hash)
                .where(BusEventRow.session_id == session_id)
                .order_by(BusEventRow.seq.desc())
                .limit(1)
            )
            if duplicate.scalars().first() is not None:
                log.debug("bus.duplicate_rejected", event_type=event_type, hash=content_hash[:12])
                raise BusError(
                    "duplicate bus event rejected",
                    hint="Identical content hash already present in this session.",
                    context={"event_type": event_type, "content_hash": content_hash},
                )

        event_id = payload.get("id") if payload else None
        from openburrow.core.models.ids import new_id

        row = BusEventRow(
            id=str(event_id) if event_id else new_id("message"),
            session_id=session_id,
            thread_id=thread_id,
            lane_id=lane_id,
            task_id=task_id,
            event_type=event_type,
            task_state=task_state,
            priority=priority,
            trust_boundary=trust_boundary,
            summary=summary[:1000],
            payload=payload or {},
            correlation_id=correlation_id,
            content_hash=content_hash,
        )
        self.session.add(row)
        await self.session.flush()
        return int(row.seq or 0)

    async def append_model(self, model: Any, *, event_type: str, **overrides: Any) -> int:
        """Convenience: log a domain object as an event, deriving the common fields."""
        return await self.append(
            event_type=event_type,
            session_id=overrides.pop("session_id", getattr(model, "session_id", "")),
            thread_id=overrides.pop("thread_id", getattr(model, "thread_id", "")),
            lane_id=overrides.pop(
                "lane_id", getattr(model, "lane_id", "") or getattr(model, "sender_lane", "")
            ),
            task_id=overrides.pop(
                "task_id", getattr(model, "task_id", "") or getattr(model, "id", "")
            ),
            content_hash=getattr(model, "content_hash", ""),
            payload=overrides.pop("payload", model.model_dump(mode="json")),
            **overrides,
        )

    async def stream(
        self,
        *,
        session_id: str,
        since_seq: int = 0,
        limit: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        statement = (
            select(BusEventRow)
            .where(BusEventRow.session_id == session_id)
            .where(BusEventRow.seq > since_seq)  # type: ignore[operator]
            .order_by(BusEventRow.seq)
        )
        if event_types:
            statement = statement.where(BusEventRow.event_type.in_(list(event_types)))  # type: ignore[attr-defined]
        if limit:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return [_event_to_dict(row) for row in result.scalars().all()]

    async def tail(
        self,
        *,
        session_id: str,
        poll_interval: float = 0.5,
        batch: int = 200,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async generator that yields new events as they are appended.

        Implemented by polling ``seq`` rather than SQLite update hooks, because
        polling is portable, survives the writer being a different process, and
        at local-disk latency the difference is imperceptible.
        """
        import asyncio

        cursor = await self.latest_seq(session_id=session_id)
        while True:
            events = await self.stream(session_id=session_id, since_seq=cursor, limit=batch)
            if not events:
                await asyncio.sleep(poll_interval)
                continue
            for event in events:
                cursor = max(cursor, int(event["seq"]))
                yield event

    async def latest_seq(self, *, session_id: str) -> int:
        result = await self.session.execute(
            select(func.max(BusEventRow.seq)).where(BusEventRow.session_id == session_id)
        )
        return int(result.scalar() or 0)

    async def replay(self, *, session_id: str) -> list[dict[str, Any]]:
        """Full ordered replay of a session — the input to the reel exporter."""
        return await self.stream(session_id=session_id)

    async def count(self, *, session_id: str, event_type: str | None = None) -> int:
        statement = (
            select(func.count())
            .select_from(BusEventRow)
            .where(BusEventRow.session_id == session_id)
        )
        if event_type:
            statement = statement.where(BusEventRow.event_type == event_type)
        result = await self.session.execute(statement)
        return int(result.scalar() or 0)

    async def volume_by_type(self, *, session_id: str) -> dict[str, int]:
        """Event counts grouped by type — the bus-health panel (item 218)."""
        result = await self.session.execute(
            select(BusEventRow.event_type, func.count())
            .where(BusEventRow.session_id == session_id)
            .group_by(BusEventRow.event_type)
        )
        return {str(row[0]): int(row[1]) for row in result.all()}


class AuditLog:
    """Append-only audit log, with stricter handling for cross-boundary records."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, entry: AuditRecord) -> int:
        row = AuditRow(
            id=entry.id,
            session_id=entry.session_id,
            lane_id=entry.lane_id,
            task_id=entry.task_id,
            delegation_id=entry.delegation_id,
            message_id=entry.message_id,
            event=entry.event,
            severity=str(entry.severity),
            trust_boundary=str(entry.trust_boundary),
            strict=entry.strict,
            summary=entry.summary,
            detail=entry.detail,
            actor_lane=entry.actor_lane,
            actor_harness=entry.actor_harness,
            on_behalf_of=entry.on_behalf_of,
            affected_lanes=entry.affected_lanes,
            allowed=entry.allowed,
            reason=entry.reason,
            correlation_id=entry.correlation_id,
            created_at=entry.created_at,
        )
        self.session.add(row)
        await self.session.flush()
        return int(row.seq or 0)

    async def for_session(
        self,
        session_id: str,
        *,
        violations_only: bool = False,
        strict_only: bool = False,
    ) -> list[dict[str, Any]]:
        statement = select(AuditRow).where(AuditRow.session_id == session_id).order_by(AuditRow.seq)
        if violations_only:
            statement = statement.where(AuditRow.severity.in_(["violation", "critical"]))  # type: ignore[attr-defined]
        if strict_only:
            statement = statement.where(AuditRow.strict.is_(True))  # type: ignore[attr-defined]
        result = await self.session.execute(statement)
        return [_audit_to_dict(row) for row in result.scalars().all()]

    async def accountability_report(self, session_id: str) -> dict[str, Any]:
        """The ``burrow audit`` payload: who authorized what, on whose behalf.

        Deliberately assembled from the audit log alone rather than joining
        against live tables — the report must be reproducible from the immutable
        record even after lanes and tasks have been pruned.
        """
        rows = await self.for_session(session_id)
        by_actor: dict[str, int] = {}
        by_boundary: dict[str, int] = {}
        violations: list[dict[str, Any]] = []

        for row in rows:
            actor = row.get("on_behalf_of") or row.get("actor_lane") or "unknown"
            by_actor[actor] = by_actor.get(actor, 0) + 1
            boundary = str(row.get("trust_boundary", "intra_repo"))
            by_boundary[boundary] = by_boundary.get(boundary, 0) + 1
            if str(row.get("severity")) in {"violation", "critical"}:
                violations.append(row)

        return {
            "session_id": session_id,
            "total_records": len(rows),
            "by_actor": by_actor,
            "by_boundary": by_boundary,
            "violations": violations,
            "violation_count": len(violations),
        }


def _event_to_dict(row: BusEventRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "id": row.id,
        "session_id": row.session_id,
        "thread_id": row.thread_id,
        "lane_id": row.lane_id,
        "task_id": row.task_id,
        "event_type": row.event_type,
        "task_state": row.task_state,
        "priority": row.priority,
        "trust_boundary": row.trust_boundary,
        "summary": row.summary,
        "payload": row.payload,
        "correlation_id": row.correlation_id,
        "content_hash": row.content_hash,
        "at": row.created_at.isoformat() if row.created_at else None,
    }


def _audit_to_dict(row: AuditRow) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "id": row.id,
        "session_id": row.session_id,
        "lane_id": row.lane_id,
        "task_id": row.task_id,
        "delegation_id": row.delegation_id,
        "message_id": row.message_id,
        "event": row.event,
        "severity": row.severity,
        "trust_boundary": row.trust_boundary,
        "strict": row.strict,
        "summary": row.summary,
        "detail": row.detail,
        "actor_lane": row.actor_lane,
        "actor_harness": row.actor_harness,
        "on_behalf_of": row.on_behalf_of,
        "affected_lanes": row.affected_lanes,
        "allowed": row.allowed,
        "reason": row.reason,
        "correlation_id": row.correlation_id,
        "at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = ["AuditLog", "BusEventLog", "Repository"]
