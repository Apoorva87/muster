"""The envelope that crosses a team boundary.

Carries references and correlation metadata — never a report, a transcript or
an LLM output. A bus message should be hundreds of bytes, not megabytes.

Field names are chosen so a CloudEvents envelope can be emitted from this
without adding fields later; see ``to_cloudevent``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.kernel.ids import new_id

#: A message larger than this is almost certainly carrying content by value.
MAX_ADVISORY_BYTES = 64 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageKind(str, Enum):
    COMMAND = "command"   # exactly one logical destination
    EVENT = "event"       # zero or more subscribers


class Message(BaseModel):
    id: str = Field(default_factory=lambda: new_id("msg"))
    kind: MessageKind
    session_id: str
    source_team: str
    source_agent: str
    destination: str | None = None   # set for COMMAND
    topic: str | None = None         # set for EVENT
    project_id: str | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    trace_id: str | None = None
    span_id: str | None = None

    def model_post_init(self, _context: Any) -> None:
        if self.kind is MessageKind.COMMAND and not self.destination:
            raise ValueError("a command needs a destination")
        if self.kind is MessageKind.EVENT and not self.topic:
            raise ValueError("an event needs a topic")
        if self.kind is MessageKind.COMMAND and self.topic:
            raise ValueError("a command must not carry a topic")

    @property
    def is_oversized(self) -> bool:
        return len(self.model_dump_json()) > MAX_ADVISORY_BYTES

    def caused(self, **overrides: Any) -> "Message":
        """Derive a follow-on message, preserving the causal chain."""
        base: dict[str, Any] = {
            "session_id": self.session_id,
            "correlation_id": self.correlation_id or self.id,
            "causation_id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
        }
        base.update(overrides)
        return Message(**base)

    def to_cloudevent(self) -> dict[str, Any]:
        """A CloudEvents 1.0 envelope. We store the fields; this projects them."""
        return {
            "specversion": "1.0",
            "id": self.id,
            "source": f"team://{self.source_team}/{self.source_agent}",
            "type": self.topic or f"command.{self.destination}",
            "time": self.created_at.isoformat(),
            "datacontenttype": "application/json",
            "subject": self.project_id,
            "data": {**self.payload, "artifact_refs": self.artifact_refs},
        }
