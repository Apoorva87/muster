"""A2A interoperability seam — INTERFACE ONLY, implementation deferred.

The V2 PRD is explicit about two things:

* A2A is an **interoperability adapter, not the internal bus.** Our internal
  protocol stays ``bus.models.message.Message`` over Restate. A2A sits at the
  edge, in one of two directions::

      our bus  <->  A2A adapter  <->  external agent

  Inbound: expose one of our agents to external A2A clients.
  Outbound: wrap a remote A2A agent as an ordinary bus ``Address`` so our teams
  address it exactly like a local one.
* "If this threatens the Day-2 schedule, define/test the interface and defer the
  complete implementation."

That is what this module is. Nothing here talks to the network, and **no
dependency is added** — the real thing needs an A2A client/server stack, and
pulling one in before we have a second team talking to a first would be
infrastructure ahead of a demo (CLAUDE.md, "minimal surface").

What V2-complete would do
-------------------------
* Serve an **agent card** per exposed agent, projected from ``AgentDescriptor``
  (name, capabilities) plus the team's ``public_commands``.
* Translate an inbound A2A task into a ``Message`` with ``kind=COMMAND``, minting
  ``correlation_id``/``causation_id`` so the external hop stays traceable, and
  route it through the ordinary ``BusAdapter``.
* Translate an outbound ``send`` into an A2A task submission, then map the remote
  task's terminal state back onto a bus EVENT so the waiting team wakes.
* Carry artifacts **by reference**, never by value: A2A artifacts become
  ``artifact_refs`` entries, so a bus message stays hundreds of bytes.
* Delegate durability to Restate. The adapter performs the HTTP call inside
  ``ctx.run_typed(...)``; it never grows its own retry loop.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from bus.models.address import Address
from bus.models.message import Message
from bus.models.team import TeamDescriptor

#: Why every entry point below raises. Asserted by the contract test.
DEFERRED = (
    "A2A is an optional interoperability adapter, not the internal bus. The V2 "
    "PRD permits defining and testing the interface and deferring the "
    "implementation; this is that deferral."
)


@runtime_checkable
class A2AEndpoint(Protocol):
    """The narrow surface an A2A peer presents to the bus.

    Deliberately not the full A2A specification — only the four operations the
    bus would actually drive. Anything wider belongs in a real A2A library, not
    in Muster (CLAUDE.md: do not reinvent).
    """

    async def agent_card(self, descriptor: TeamDescriptor, agent: str) -> dict[str, Any]:
        """The card advertising one of our agents to external A2A clients."""

    async def submit_task(self, destination: Address, command: Message) -> str:
        """Send a command to a remote A2A agent. Returns the remote task ID."""

    async def get_task(self, remote_task_id: str) -> dict[str, Any]:
        """Poll one remote task's state. Called from a durable step, never a loop."""

    async def accept_task(self, envelope: dict[str, Any]) -> Message:
        """Translate an inbound A2A task into a bus ``Message``."""


class A2ABusAdapter:
    """``BusAdapter``-shaped stub for a remote A2A-hosted team. Not implemented.

    Present so the seam is real and typed: a future ``A2ABusAdapter`` drops into
    the same slot as ``RestateBusAdapter`` with no change to agent code or to the
    routing layer. Every method raises ``NotImplementedError`` rather than
    quietly misrouting — the same choice V1 made for cross-team addresses.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(f"A2ABusAdapter is deferred. {DEFERRED}")

    async def register_team(self, descriptor: TeamDescriptor) -> None:
        raise NotImplementedError(DEFERRED)

    async def unregister_team(self, team_id: str) -> None:
        raise NotImplementedError(DEFERRED)

    async def send(self, destination: Address, command: Message) -> None:
        raise NotImplementedError(DEFERRED)

    async def publish(self, topic: str, event: Message) -> list[Address]:
        raise NotImplementedError(DEFERRED)

    async def subscribe(self, topic: str, address: Address) -> None:
        raise NotImplementedError(DEFERRED)

    async def unsubscribe(self, topic: str, address: Address) -> None:
        raise NotImplementedError(DEFERRED)
