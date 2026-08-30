"""The BusAdapter contract.

Agent-team code must not know whether communication is local, Restate-backed
or routed across a multi-team bus. That is this interface's whole job.

No Restate SDK type may appear here. V1 already codes against it through
``app.kernel.bus``; V2 implements it fully.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from bus.models.address import Address
from bus.models.message import Message
from bus.models.team import TeamDescriptor


class DeliveryError(RuntimeError):
    """Routing failed for a reason the caller can act on."""


@runtime_checkable
class BusAdapter(Protocol):
    async def register_team(self, descriptor: TeamDescriptor) -> None: ...

    async def unregister_team(self, team_id: str) -> None: ...

    async def send(self, destination: Address, command: Message) -> None:
        """Deliver a command to exactly one logical destination."""

    async def publish(self, topic: str, event: Message) -> list[Address]:
        """Fan out to every subscriber. Returns who was woken."""

    async def subscribe(self, topic: str, address: Address) -> None: ...

    async def unsubscribe(self, topic: str, address: Address) -> None: ...
