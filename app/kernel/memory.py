"""Team memory: the note format and the store contract.

Markdown is canonical. Any index is derived and disposable — deleting it must
lose nothing (V1 PRD: do not use a vector DB as canonical state). A note is a
file a human can read, correct with an editor and revert with Git, because a
wrong memory is a bug and bugs must be fixable.

Notes follow a **compiled truth + timeline** shape: a summary that is the team's
current belief, over dated evidence that produced it. A reader sees both what
the team thinks and why.

Nothing here knows about a backend, an index or an agent.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, Field

from app.kernel.ids import new_id


class MemoryKind(str, Enum):
    LESSON = "lesson"      # what worked, what did not, and why
    DOMAIN = "domain"      # durable facts about the subject matter
    DECISION = "decision"  # an approval or rejection, and its reasoning
    ENTITY = "entity"      # a recurring subject the team keeps meeting


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TimelineEntry(BaseModel):
    """One dated piece of evidence."""

    on: date
    note: str
    source: str | None = None

    def render(self) -> str:
        suffix = f" ({self.source})" if self.source else ""
        return f"- {self.on.isoformat()} — {self.note}{suffix}"

    @classmethod
    def parse(cls, line: str) -> "TimelineEntry | None":
        match = re.match(r"^-\s*(\d{4}-\d{2}-\d{2})\s*—\s*(.*?)(?:\s*\(([^)]+)\))?$",
                         line.strip())
        if match is None:
            return None
        return cls(on=date.fromisoformat(match.group(1)),
                   note=match.group(2).strip(), source=match.group(3))


def _today() -> date:
    return datetime.now(timezone.utc).date()


class MemoryNote(BaseModel):
    """One markdown file."""

    id: str = Field(default_factory=lambda: new_id("mem"))
    team: str
    kind: MemoryKind
    subject: str
    summary: str
    timeline: list[TimelineEntry] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    #: Runs and artifacts this came from. A note with no sources is a rumour.
    sources: list[str] = Field(default_factory=list)
    created: date = Field(default_factory=_today)
    updated: date = Field(default_factory=_today)

    @property
    def slug(self) -> str:
        """Filename stem: readable, stable, safe on every filesystem."""
        base = re.sub(r"[^a-z0-9]+", "-", self.subject.lower()).strip("-") or "note"
        return f"{base}-{self.id.removeprefix('mem_')[:8]}"

    @property
    def is_grounded(self) -> bool:
        return bool(self.sources)

    def searchable_text(self) -> str:
        return " ".join([self.subject, self.summary,
                         *(e.note for e in self.timeline)])

    def with_evidence(self, note: str, *, source: str | None = None,
                      summary: str | None = None) -> "MemoryNote":
        """Append evidence. Consolidation is the only thing that rewrites."""
        entry = TimelineEntry(on=_today(), note=note, source=source)
        return self.model_copy(update={
            "timeline": [*self.timeline, entry],
            "summary": summary or self.summary,
            "sources": list(dict.fromkeys([*self.sources, *( [source] if source else [] )])),
            "updated": _today(),
        })

    # ------------------------------------------------------------- markdown

    def to_markdown(self) -> str:
        front = {
            "id": self.id, "team": self.team, "kind": self.kind.value,
            "subject": self.subject, "confidence": self.confidence.value,
            "sources": self.sources,
            "created": self.created.isoformat(),
            "updated": self.updated.isoformat(),
        }
        head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
        body = [f"---\n{head}\n---", "", "# Summary", "", self.summary.strip()]
        if self.timeline:
            body += ["", "## Timeline", "",
                     *(e.render() for e in sorted(self.timeline, key=lambda e: e.on))]
        return "\n".join(body).rstrip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryNote":
        match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if match is None:
            raise ValueError("memory note is missing YAML frontmatter")
        front = yaml.safe_load(match.group(1)) or {}
        rest = match.group(2)

        summary_match = re.search(r"#\s*Summary\s*\n(.*?)(?=\n##\s|\Z)", rest, re.DOTALL)
        summary = (summary_match.group(1) if summary_match else rest).strip()

        timeline: list[TimelineEntry] = []
        timeline_match = re.search(r"##\s*Timeline\s*\n(.*?)(?=\n##\s|\Z)", rest, re.DOTALL)
        if timeline_match:
            for line in timeline_match.group(1).splitlines():
                entry = TimelineEntry.parse(line)
                if entry is not None:
                    timeline.append(entry)

        return cls(id=front.get("id") or new_id("mem"),
                   team=front["team"], kind=MemoryKind(front["kind"]),
                   subject=front["subject"], summary=summary, timeline=timeline,
                   confidence=Confidence(front.get("confidence", "medium")),
                   sources=list(front.get("sources") or []),
                   created=_as_date(front.get("created")),
                   updated=_as_date(front.get("updated")))


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    return _today()


class MemoryRef(BaseModel):
    """What crosses into an agent's context — a reference, never a body.

    Same discipline as artifacts: an agent sees enough to decide whether to
    load the note, and loads it deliberately.
    """

    id: str
    subject: str
    kind: MemoryKind
    confidence: Confidence
    updated: date
    #: A single line, so a recall result is scannable without loading anything.
    preview: str = ""

    @classmethod
    def of(cls, note: MemoryNote, *, preview_chars: int = 160) -> "MemoryRef":
        first = note.summary.strip().splitlines()[0] if note.summary.strip() else ""
        return cls(id=note.id, subject=note.subject, kind=note.kind,
                   confidence=note.confidence, updated=note.updated,
                   preview=first[:preview_chars])

    def render(self) -> str:
        return (f"[{self.kind.value}] {self.subject} "
                f"({self.confidence.value}, {self.updated.isoformat()}): {self.preview}")


@runtime_checkable
class MemoryStore(Protocol):
    """Per-team memory. Every implementation keeps markdown canonical."""

    @property
    def team_id(self) -> str: ...

    async def remember(self, *, kind: MemoryKind, subject: str, summary: str,
                       sources: list[str] | None = None,
                       confidence: Confidence = Confidence.MEDIUM,
                       evidence: str | None = None) -> MemoryRef:
        """Write or extend a note. Returns a reference, not the note."""

    async def recall(self, query: str, *, limit: int = 3,
                     kinds: list[MemoryKind] | None = None) -> list[MemoryRef]:
        """Explicit retrieval. Never called on an agent's behalf."""

    async def get(self, note_id: str) -> MemoryNote:
        """Load one note by reference."""

    async def forget(self, note_id: str) -> bool:
        """Delete a note. It must stay deleted."""

    async def rebuild_index(self) -> int:
        """Rebuild any derived index from the files. Loses nothing."""


class NullMemoryStore:
    """``MEMORY_BACKEND=none``. A team runs exactly as it did in V3.

    Not a stub to be replaced later: a fully supported mode. Memory must never
    be load-bearing, so the disabled path is a first-class one.
    """

    def __init__(self, team_id: str = "") -> None:
        self._team_id = team_id

    @property
    def team_id(self) -> str:
        return self._team_id

    async def remember(self, **_: Any) -> MemoryRef:
        return MemoryRef(id="mem_disabled", subject="", kind=MemoryKind.LESSON,
                         confidence=Confidence.LOW, updated=_today())

    async def recall(self, query: str, *, limit: int = 3,
                     kinds: list[MemoryKind] | None = None) -> list[MemoryRef]:
        return []

    async def get(self, note_id: str) -> MemoryNote:
        raise KeyError(f"memory is disabled; no note {note_id}")

    async def forget(self, note_id: str) -> bool:
        return False

    async def rebuild_index(self) -> int:
        return 0
