"""Semantic state persistence.

Deliberately synchronous. At laptop scale a local Postgres round-trip is
sub-millisecond, and a sync repository is far easier to read than an async one
(CLAUDE.md: structured, obvious internals). If this ever shows up in a profile,
the fix is an async engine behind this same interface — callers do not change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import (ArtifactRow, Base, EventRow, RunRow, SubscriptionRow,
                           TaskRow)
from app.kernel.ids import new_id
from app.kernel.models import (Artifact, Event, RunRecord, Subscription, Task,
                               TaskStatus)

# The PRD's V1 subscription table.
DEFAULT_SUBSCRIPTIONS: tuple[tuple[str, str], ...] = (
    ("proposal.ready", "critic"),
    ("proposal.ready", "finance"),
    ("research.complete", "director"),
    ("finance.complete", "director"),
    ("critique.complete", "director"),
    ("market.changed", "finance"),
    ("market.changed", "director"),
)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo. Everything we store is UTC, so put it back."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "Repository":
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            # An in-memory SQLite DB lives inside one connection. The default
            # pool hands each thread its own, so a web request on another thread
            # would see an empty database. StaticPool shares the one connection.
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        return cls(create_engine(url, future=True, **kwargs))

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    # ---------------------------------------------------------------- tasks

    def save_task(self, task: Task) -> Task:
        with self._session.begin() as s:
            s.merge(TaskRow(**task.model_dump()))
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self._session() as s:
            row = s.get(TaskRow, task_id)
            return self._to_task(row) if row else None

    def set_task_status(self, task_id: str, status: TaskStatus) -> None:
        with self._session.begin() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            row.status = status.value

    def list_tasks(self, project_id: str) -> list[Task]:
        with self._session() as s:
            rows = s.scalars(
                select(TaskRow).where(TaskRow.project_id == project_id)
                .order_by(TaskRow.created_at)
            ).all()
            return [self._to_task(r) for r in rows]

    # --------------------------------------------------------------- events

    def save_event(self, event: Event) -> Event:
        with self._session.begin() as s:
            s.merge(EventRow(**event.model_dump()))
        return event

    def list_events(self, project_id: str) -> list[Event]:
        with self._session() as s:
            rows = s.scalars(
                select(EventRow).where(EventRow.project_id == project_id)
                .order_by(EventRow.created_at)
            ).all()
            return [Event(**self._fields(r, Event)) for r in rows]

    # ------------------------------------------------------------ artifacts

    def save_artifact(self, artifact: Artifact) -> Artifact:
        with self._session.begin() as s:
            s.merge(ArtifactRow(**artifact.model_dump()))
        return artifact

    def list_artifacts(self, project_id: str) -> list[Artifact]:
        with self._session() as s:
            rows = s.scalars(
                select(ArtifactRow).where(ArtifactRow.project_id == project_id)
                .order_by(ArtifactRow.created_at)
            ).all()
            return [Artifact(**self._fields(r, Artifact)) for r in rows]

    # ----------------------------------------------------------------- runs

    def record_run(self, run: RunRecord) -> RunRecord:
        with self._session.begin() as s:
            s.merge(RunRow(**run.model_dump()))
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._session() as s:
            row = s.get(RunRow, run_id)
            return self._to_run(row) if row else None

    def finish_run(self, run_id: str, *, status: str,
                   output_refs: dict[str, Any] | None = None,
                   error: str | None = None) -> None:
        with self._session.begin() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            finished = datetime.now(timezone.utc)
            row.finished_at = finished
            row.status = status
            started = _aware(row.started_at)
            row.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
            if output_refs is not None:
                row.output_refs = output_refs
            if error is not None:
                row.error = error

    def list_runs(self, project_id: str) -> list[RunRecord]:
        with self._session() as s:
            rows = s.scalars(
                select(RunRow).where(RunRow.project_id == project_id)
                .order_by(RunRow.started_at)
            ).all()
            return [self._to_run(r) for r in rows]

    # ------------------------------------------------------------- approval

    def set_awakeable(self, run_id: str, awakeable_id: str) -> None:
        """Persist the durable promise ID so the UI can resume this run."""
        with self._session.begin() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            row.awakeable_id = awakeable_id
            row.awaiting_since = datetime.now(timezone.utc)
            row.status = "WAITING_FOR_HUMAN"

    def clear_awakeable(self, run_id: str) -> None:
        with self._session.begin() as s:
            row = s.get(RunRow, run_id)
            if row is not None:
                row.awakeable_id = None
                row.awaiting_since = None

    def list_waiting_runs(self, project_id: str) -> list[RunRecord]:
        with self._session() as s:
            rows = s.scalars(
                select(RunRow)
                .where(RunRow.project_id == project_id)
                .where(RunRow.awakeable_id.is_not(None))
                .where(RunRow.finished_at.is_(None))
                .order_by(RunRow.awaiting_since)
            ).all()
            return [self._to_run(r) for r in rows]

    # -------------------------------------------------------- subscriptions

    def add_subscription(self, topic: str, agent: str) -> None:
        with self._session.begin() as s:
            existing = s.scalar(
                select(SubscriptionRow)
                .where(SubscriptionRow.topic == topic)
                .where(SubscriptionRow.agent == agent)
            )
            if existing is None:
                s.add(SubscriptionRow(id=new_id("sub"), topic=topic, agent=agent))

    def seed_default_subscriptions(self,
                                   pairs: Iterable[tuple[str, str]] | None = None) -> None:
        for topic, agent in (pairs or DEFAULT_SUBSCRIPTIONS):
            self.add_subscription(topic, agent)

    def subscribers_for(self, topic: str) -> list[str]:
        with self._session() as s:
            return list(s.scalars(
                select(SubscriptionRow.agent)
                .where(SubscriptionRow.topic == topic)
                .order_by(SubscriptionRow.agent)
            ).all())

    def list_subscriptions(self) -> list[Subscription]:
        with self._session() as s:
            rows = s.scalars(select(SubscriptionRow).order_by(SubscriptionRow.topic)).all()
            return [Subscription(id=r.id, topic=r.topic, agent=r.agent) for r in rows]

    # -------------------------------------------------------------- mapping

    @staticmethod
    def _fields(row: Any, model: type) -> dict[str, Any]:
        return {name: _aware(getattr(row, name)) if name.endswith("_at")
                else getattr(row, name)
                for name in model.model_fields}

    def _to_task(self, row: TaskRow) -> Task:
        data = self._fields(row, Task)
        data["status"] = TaskStatus(row.status)
        return Task(**data)

    def _to_run(self, row: RunRow) -> RunRecord:
        return RunRecord(**self._fields(row, RunRecord))
