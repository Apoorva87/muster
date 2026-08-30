"""Topic fan-out — one event, zero or more subscribers, across every team.

Exact-topic matching only. The V2 PRD says to defer wildcard/pattern routing
("Exact-topic subscriptions are sufficient initially"), so a subscriber to
``a.b`` is *not* woken by ``a.b.c``. Namespaced topics are therefore just plain
strings: ``investment.proposal.ready``, ``system.team.registered``.

A team that is UNREACHABLE is skipped rather than crashing the fan-out — one
sick team must not stop the other teams from hearing an event. Skips are
reported (returned, logged and recorded) so they are visible rather than silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bus.models.address import Address
from bus.models.message import Message
from bus.models.team import Health
from bus.routing.registry import TeamRegistry

logger = logging.getLogger(__name__)

#: Health states a subscriber's team may be in and still be worth waking.
#: DEGRADED still gets the event — Restate will hold the invocation.
DELIVERABLE_HEALTH = (Health.HEALTHY, Health.DEGRADED)


@dataclass(frozen=True)
class SkippedSubscriber:
    """A subscriber that was not woken, and why."""

    address: Address
    topic: str
    health: Health
    reason: str

    def __str__(self) -> str:
        return f"{self.address} skipped for {self.topic!r}: {self.reason}"


@dataclass
class FanOut:
    """The full result of one fan-out: who was woken, who was not."""

    topic: str
    delivered: list[Address] = field(default_factory=list)
    skipped: list[SkippedSubscriber] = field(default_factory=list)


class TopicRouter:
    """Resolves event subscribers across every registered team."""

    def __init__(self, registry: TeamRegistry) -> None:
        self.registry = registry
        #: Subscribers skipped by the most recent ``fan_out`` call.
        self.last_skipped: list[SkippedSubscriber] = []

    async def fan_out(self, topic: str, event: Message) -> list[Address]:
        """Return every reachable subscriber to ``topic``, across all teams.

        Unreachable teams are skipped and recorded on ``last_skipped``; use
        :meth:`resolve` when the caller wants both halves of the answer.
        """
        return (await self.resolve(topic, event)).delivered

    async def resolve(self, topic: str, event: Message) -> FanOut:
        """Like :meth:`fan_out`, but returns the skips alongside the deliveries."""
        if not topic:
            raise ValueError("cannot fan out an event with no topic")

        result = FanOut(topic=topic)
        for address in self.registry.subscribers_for(topic):
            health = self._health_of(address)
            if health in DELIVERABLE_HEALTH:
                result.delivered.append(address)
                continue
            skip = SkippedSubscriber(
                address=address, topic=topic, health=health,
                reason=f"team {address.team!r} is {health.value}")
            result.skipped.append(skip)
            logger.warning("bus fan-out: %s (event %s)", skip, event.id)

        self.last_skipped = result.skipped
        return result

    def _health_of(self, address: Address) -> Health:
        # subscribers_for only ever yields addresses of registered teams, so a
        # missing team here means the roll changed mid-fan-out. Treat it as
        # unreachable rather than raising: the other subscribers still deserve
        # their event.
        try:
            return self.registry.get(address.team).health
        except KeyError:
            return Health.UNREACHABLE
