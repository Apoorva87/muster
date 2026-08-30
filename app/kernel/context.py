"""The seam between Muster's agent semantics and Restate's durable execution.

Every kernel primitive takes a ``KernelContext`` rather than a Restate context.
Two implementations exist:

* ``RestateKernelContext`` (``app/runtime/durable.py``) wraps the real SDK.
* ``FakeKernelContext`` (here) records calls in memory.

That is what lets the whole unit suite run with no Restate server, no Postgres
and no Docker — and it is the same seam V2's ``BusAdapter`` plugs into. No
Restate SDK type may appear in this file (CLAUDE.md, public surface).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from app.kernel.ids import new_id


@dataclass(frozen=True)
class Send:
    """A durable one-way invocation. ``delay`` set means a timer wakeup."""

    agent: str
    handler: str
    key: str
    payload: dict[str, Any]
    delay: timedelta | None = None
    idempotency_key: str | None = None


@runtime_checkable
class KernelContext(Protocol):
    """The narrow set of durable capabilities the kernel needs."""

    @property
    def key(self) -> str:
        """The Virtual Object key — the project this invocation belongs to."""

    def send(self, *, agent: str, handler: str, key: str,
             payload: dict[str, Any], delay: timedelta | None = None,
             idempotency_key: str | None = None) -> None:
        """Fire-and-forget durable invocation of another agent."""

    def awakeable(self) -> tuple[str, Awaitable[Any]]:
        """A durable promise: an ID to hand out, and a future to await."""

    async def run_typed(self, name: str, fn: Callable[..., Awaitable[Any]],
                        /, **kwargs: Any) -> Any:
        """Execute a side effect exactly once across replays."""

    async def sleep(self, delta: timedelta) -> None:
        """Durable sleep."""


@dataclass
class FakeKernelContext:
    """In-memory ``KernelContext`` for tests.

    Models the two Restate behaviours the kernel depends on: sends are recorded
    rather than dispatched, and journalled steps are not re-executed on replay.
    """

    key: str = "proj_test"
    sends: list[Send] = field(default_factory=list)
    journal: list[tuple[str, Any]] = field(default_factory=list)
    awakeables: dict[str, asyncio.Future] = field(default_factory=dict)
    _replaying: bool = False

    def send(self, *, agent: str, handler: str, key: str,
             payload: dict[str, Any], delay: timedelta | None = None,
             idempotency_key: str | None = None) -> None:
        self.sends.append(Send(agent=agent, handler=handler, key=key,
                               payload=payload, delay=delay,
                               idempotency_key=idempotency_key))

    def awakeable(self) -> tuple[str, Awaitable[Any]]:
        awakeable_id = new_id("awk")
        self.awakeables[awakeable_id] = asyncio.get_event_loop().create_future()
        return awakeable_id, self.awakeables[awakeable_id]

    def resolve_awakeable(self, awakeable_id: str, value: Any) -> None:
        """Stand-in for the external Approve/Reject callback."""
        future = self.awakeables.get(awakeable_id)
        if future is None:
            raise KeyError(f"unknown awakeable: {awakeable_id}")
        if not future.done():
            future.set_result(value)

    async def run_typed(self, name: str, fn: Callable[..., Awaitable[Any]],
                        /, **kwargs: Any) -> Any:
        for journalled_name, result in self.journal:
            if journalled_name == name:
                return result
        result = await fn(**kwargs)
        self.journal.append((name, result))
        return result

    async def sleep(self, delta: timedelta) -> None:
        return None

    def journal_names(self) -> list[str]:
        return [name for name, _ in self.journal]

    def replay(self) -> None:
        """Simulate a crash and replay: journal survives, sends are re-derived."""
        self._replaying = True
        self.sends.clear()
