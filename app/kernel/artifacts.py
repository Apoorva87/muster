"""Artifact storage.

Large agent output lives here, outside agent context. Other agents receive an
``ArtifactRef`` and load the body only when they actually need it (V1 PRD,
"Agent execution and context").

Team code must always go through this interface and never hard-code a
filesystem path into an inter-agent message, so a later S3-compatible backend
needs no agent changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from app.kernel.ids import new_id

_EXTENSIONS = {"markdown": "md", "json": "json", "text": "txt",
               "proposal": "md", "critique": "md", "synthesis": "md",
               "research": "md", "valuation": "md"}


class ArtifactRef(BaseModel):
    """The small thing that crosses between agents."""

    id: str
    project_id: str
    type: str

    def as_input(self) -> str:
        return self.id


@runtime_checkable
class ArtifactStore(Protocol):
    async def put(self, *, project_id: str, task_id: str, created_by: str,
                  content: Any, type: str = "markdown",
                  meta: dict[str, Any] | None = None,
                  artifact_id: str | None = None) -> ArtifactRef: ...

    async def get(self, artifact_id: str) -> str: ...


def _safe_segment(value: str, field: str) -> str:
    if not value or "/" in value or "\\" in value or value.startswith("."):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


class FilesystemArtifactStore:
    """V1 backend: ``<root>/<project-id>/<artifact-id>.<ext>``."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Path] = {}

    async def put(self, *, project_id: str, task_id: str, created_by: str,
                  content: Any, type: str = "markdown",
                  meta: dict[str, Any] | None = None,
                  artifact_id: str | None = None) -> ArtifactRef:
        """Write an artifact.

        ``artifact_id`` must be supplied from inside a durable handler. Minting
        one here would be non-deterministic: a replay would generate a fresh id,
        the resulting send would differ from the journalled one, and Restate
        would fail the invocation with a code-path mismatch.
        """
        _safe_segment(project_id, "project_id")
        artifact_id = artifact_id or new_id("art")
        ext = _EXTENSIONS.get(type, "txt")
        directory = self._root / project_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{artifact_id}.{ext}"

        body = content if isinstance(content, str) else json.dumps(content, indent=2)
        path.write_text(body, encoding="utf-8")
        self._index[artifact_id] = path

        return ArtifactRef(id=artifact_id, project_id=project_id, type=type)

    async def get(self, artifact_id: str) -> str:
        path = self._index.get(artifact_id) or self._find(artifact_id)
        if path is None:
            raise KeyError(f"unknown artifact: {artifact_id}")
        return path.read_text(encoding="utf-8")

    def path_for(self, artifact_id: str) -> Path | None:
        return self._index.get(artifact_id) or self._find(artifact_id)

    def _find(self, artifact_id: str) -> Path | None:
        """Recover the path after a restart, when the in-memory index is empty."""
        for candidate in self._root.glob(f"*/{artifact_id}.*"):
            self._index[artifact_id] = candidate
            return candidate
        return None
