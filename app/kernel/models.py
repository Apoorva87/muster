"""Domain models — the only first-class concepts V1 implements.

These types appear in the public kernel API, so changing them is a contract
change (CLAUDE.md, "Never break the public surface").
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.kernel.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class Task(BaseModel):
    """A bounded unit of work. Never carries a transcript."""

    model_config = ConfigDict(frozen=False)

    id: str = Field(default_factory=lambda: new_id("task"))
    project_id: str
    type: str
    objective: str
    assigned_agent: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    parent_task_id: str | None = None
    # Artifact references only — never inlined artifact bodies.
    input_refs: dict[str, str] = Field(default_factory=dict)
    #: Bus provenance. None for a team-local task; ``team://team/agent`` for one
    #: delegated from another team (V2: propagate correlation through Tasks).
    source: str | None = None
    correlation_id: str | None = None


class Event(BaseModel):
    """A small structured notification: metadata and references, not content."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    topic: str
    project_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    task_id: str | None = None
    correlation_id: str | None = None


class Artifact(BaseModel):
    """Metadata for a large output stored outside agent context."""

    id: str = Field(default_factory=lambda: new_id("art"))
    project_id: str
    task_id: str
    type: str
    path: str
    created_by: str
    created_at: datetime = Field(default_factory=_utcnow)
    meta: dict[str, Any] = Field(default_factory=dict)


class Subscription(BaseModel):
    """Maps a logical topic to one agent. Fan-out is many of these."""

    id: str = Field(default_factory=lambda: new_id("sub"))
    topic: str
    agent: str


class RunRecord(BaseModel):
    """One human-visible line in the project timeline.

    ``awakeable_id`` is populated when a run parks on human input; without it
    the Approve button cannot resume the workflow (decision D3).
    """

    id: str = Field(default_factory=lambda: new_id("run"))
    parent_run_id: str | None = None
    project_id: str
    task_id: str | None = None
    agent: str
    event_type: str
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    status: str = "RUNNING"
    duration_ms: int | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    input_refs: dict[str, Any] = Field(default_factory=dict)
    output_refs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # Reserved so OpenTelemetry can attach in V2 without a schema redesign.
    trace_id: str | None = None
    span_id: str | None = None
    awakeable_id: str | None = None
    awaiting_since: datetime | None = None
