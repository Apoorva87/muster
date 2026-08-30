"""Memory backends, selected by configuration.

Markdown is canonical in every backend. The default needs no service, no
network and no model; ``none`` is a fully supported mode, not a stub, because
memory must never be load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.kernel.memory import MemoryStore, NullMemoryStore

BACKENDS = ("filesystem", "gbrain", "none")

#: Per-agent permissions from team.yaml.
PERMISSIONS = ("off", "read", "read-write")


class MemoryError_(RuntimeError):
    """The configured backend cannot be built, with how to fix it."""


def build_memory_store(*, backend: str = "filesystem", team_id: str,
                       root: Path | None = None, **options: Any) -> MemoryStore:
    """Construct the configured store. Unknown backend raises, never guesses."""
    if backend not in BACKENDS:
        raise MemoryError_(
            f"unknown MEMORY_BACKEND {backend!r}; available: {list(BACKENDS)}")

    if backend == "none":
        return NullMemoryStore(team_id)

    corpus = Path(root) if root is not None else Path("teams") / team_id / "memory"

    if backend == "filesystem":
        from app.memory.filesystem import FilesystemMemoryStore

        return FilesystemMemoryStore(root=corpus, team_id=team_id)

    from app.memory.gbrain import GBrainMemoryStore

    return GBrainMemoryStore(root=corpus, team_id=team_id, **options)


def store_from_settings(settings: Any, team_id: str) -> MemoryStore:
    return build_memory_store(backend=settings.memory_backend, team_id=team_id,
                              root=settings.memory_root_for(team_id))


class ReadOnlyMemory:
    """Wraps a store so an agent can recall but not write.

    ``memory: read`` in team.yaml. A refused write is loud rather than silently
    dropped — a team that thinks it is learning and is not would be worse than
    one that knows it cannot.
    """

    def __init__(self, inner: MemoryStore) -> None:
        self._inner = inner

    @property
    def team_id(self) -> str:
        return self._inner.team_id

    async def remember(self, **_: Any):
        raise PermissionError(
            f"agent has memory: read on team {self.team_id!r}; "
            "set memory: read-write in team.yaml to let it write")

    async def recall(self, query: str, **kwargs: Any):
        return await self._inner.recall(query, **kwargs)

    async def get(self, note_id: str):
        return await self._inner.get(note_id)

    async def forget(self, note_id: str) -> bool:
        raise PermissionError(f"agent has memory: read on team {self.team_id!r}")

    async def rebuild_index(self) -> int:
        return await self._inner.rebuild_index()


def apply_permission(store: MemoryStore, permission: str | None) -> MemoryStore:
    """Narrow a store to what an agent is allowed to do."""
    if permission in (None, "read-write"):
        return store
    if permission == "off":
        return NullMemoryStore(store.team_id)
    if permission == "read":
        return ReadOnlyMemory(store)
    raise MemoryError_(
        f"unknown memory permission {permission!r}; expected one of {list(PERMISSIONS)}")


__all__ = ["BACKENDS", "PERMISSIONS", "MemoryError_", "ReadOnlyMemory",
           "apply_permission", "build_memory_store", "store_from_settings"]
