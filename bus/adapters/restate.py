"""The Restate-backed bus adapter — routing only, never a second retry system.

**Restate stays the durability authority. The bus is routing logic, not an
independent delivery/retry engine.** This module resolves a logical address or
a topic to concrete destinations and hands each one to Restate through a
``KernelContext``. It owns no queue, no lease, no backoff, no dead-letter, no
redelivery timer. If a send fails after it reaches Restate, Restate retries it;
if a target is down, Restate holds the invocation. Adding retry logic here would
duplicate the journal and break exactly-once semantics.

The seam is ``app.kernel.context.KernelContext``. This module must not import
``restate`` — that is asserted by a test — which is what keeps the V2 bus
testable with no Restate server, no Postgres and no Docker.

Duplicate suppression here is a *routing* guard: a redelivered ``message_id``
does not produce a second logical send (V2 acceptance criterion 8). The real
side-effect guarantee is Restate's, via the ``idempotency_key`` carried on every
invocation.
"""

from __future__ import annotations

import logging
from typing import Callable

from app.kernel.context import KernelContext
from bus.adapters.base import DeliveryError
from bus.models.address import Address
from bus.models.message import Message
from bus.models.team import TeamDescriptor
from bus.routing.commands import CommandRouter
from bus.routing.registry import TeamRegistry, UnknownTeam
from bus.routing.topics import SkippedSubscriber, TopicRouter

logger = logging.getLogger(__name__)

#: Every agent exposes one entry point to the bus. The envelope in the payload
#: says what the work actually is.
HANDLER = "handle"


class RestateBusAdapter:
    """``BusAdapter`` over Restate, reached through ``KernelContext``.

    ``ctx_factory`` maps a team ID to the ``KernelContext`` used to invoke that
    team. In production that wraps the real SDK context for the team's endpoint;
    in tests it returns a ``FakeKernelContext``, which is the whole point of the
    seam.
    """

    def __init__(self, registry: TeamRegistry,
                 ctx_factory: Callable[[str], KernelContext]) -> None:
        self.registry = registry
        self.ctx_factory = ctx_factory
        self.commands = CommandRouter(registry)
        self.topics = TopicRouter(registry)

    # -- registration ----------------------------------------------------

    async def register_team(self, descriptor: TeamDescriptor) -> None:
        """Idempotent: re-registering a team replaces its descriptor."""
        self.registry.register(descriptor)

    async def unregister_team(self, team_id: str) -> None:
        self.registry.unregister(team_id)

    # -- delivery --------------------------------------------------------

    async def send(self, destination: Address, command: Message) -> None:
        """Deliver a command to exactly one logical destination.

        A redelivered ``command.id`` is a no-op: the invocation was already
        handed to Restate, and Restate is what guarantees it ran.
        """
        address = await self.commands.route(
            command.source_team, destination, command)

        if self.commands.seen(command.id):
            logger.info("bus: dropping duplicate command %s to %s",
                        command.id, address)
            return

        self._invoke(address, command)

    async def publish(self, topic: str, event: Message) -> list[Address]:
        """Fan out to every reachable subscriber. Returns who was woken."""
        if self.commands.seen(event.id):
            logger.info("bus: dropping duplicate event %s on %r", event.id, topic)
            return []

        subscribers = await self.topics.fan_out(topic, event)
        for address in subscribers:
            self._invoke(address, event, suffix=f"{address.team}/{address.agent}")
        return subscribers

    @property
    def last_skipped(self) -> list[SkippedSubscriber]:
        """Subscribers the most recent ``publish`` could not reach."""
        return self.topics.last_skipped

    # -- subscriptions ---------------------------------------------------

    async def subscribe(self, topic: str, address: Address) -> None:
        if address.is_local:
            raise DeliveryError(
                f"cannot subscribe {address} to {topic!r}: qualify the address "
                "with a team first")
        descriptor = self._descriptor_for(address, verb="subscribe")
        if not descriptor.has_agent(address.agent):
            raise DeliveryError(
                f"cannot subscribe {address} to {topic!r}: team "
                f"{address.team!r} exposes {descriptor.agent_names}")
        pair = (topic, address.agent)
        if pair not in descriptor.subscriptions:
            descriptor.subscriptions.append(pair)

    async def unsubscribe(self, topic: str, address: Address) -> None:
        descriptor = self._descriptor_for(address, verb="unsubscribe")
        descriptor.subscriptions = [
            pair for pair in descriptor.subscriptions
            if tuple(pair) != (topic, address.agent)]

    # -- internals -------------------------------------------------------

    def _invoke(self, address: Address, message: Message,
                suffix: str | None = None) -> None:
        """Hand one durable invocation to Restate for ``address``."""
        ctx = self.ctx_factory(address.team)
        key = message.project_id or message.session_id
        # One event fanned out to N subscribers is N distinct invocations, so
        # each needs its own idempotency key; a command has exactly one target
        # and keeps the bare message ID.
        idempotency_key = f"{message.id}:{suffix}" if suffix else message.id
        ctx.send(agent=address.agent, handler=HANDLER, key=key,
                 payload=message.model_dump(mode="json"),
                 idempotency_key=idempotency_key)

    def _descriptor_for(self, address: Address, *, verb: str) -> TeamDescriptor:
        try:
            return self.registry.get(address.team)
        except UnknownTeam as exc:
            raise DeliveryError(
                f"cannot {verb} {address}: {exc.args[0]}") from exc
