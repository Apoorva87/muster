"""The V2 seam, defined in V1 but not implemented across a network.

Agent code calls the kernel, never Restate. The kernel routes through a
``BusAdapter``. V1 ships only ``LocalBusAdapter``, which resolves inside this
team; V2 adds a multi-team adapter without any agent changing.

No Restate SDK type may appear in this contract (V2 PRD, "BusAdapter contract").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.kernel.subscriptions import SubscriptionRegistry


@dataclass(frozen=True)
class Address:
    """``team://<team>/<agent>``, or just an agent name when team-local."""

    agent: str
    team: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "Address":
        if not raw.startswith("team://"):
            return cls(agent=raw)
        team, _, agent = raw[len("team://"):].partition("/")
        if not team or not agent:
            raise ValueError(f"malformed address: {raw!r}")
        return cls(agent=agent, team=team)

    @property
    def is_local(self) -> bool:
        return self.team is None

    def __str__(self) -> str:
        return f"team://{self.team}/{self.agent}" if self.team else self.agent


@dataclass(frozen=True)
class TeamDescriptor:
    team_id: str
    version: int
    agents: dict[str, list[str]] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


@runtime_checkable
class BusAdapter(Protocol):
    async def register_team(self, descriptor: TeamDescriptor) -> None: ...
    async def unregister_team(self, team_id: str) -> None: ...
    async def resolve(self, address: Address) -> str: ...
    async def subscribers_for(self, topic: str) -> list[str]: ...
    async def subscribe(self, topic: str, agent: str) -> None: ...


class LocalBusAdapter:
    """V1 default: everything resolves inside this team."""

    def __init__(self, subscriptions: SubscriptionRegistry,
                 team_id: str = "local") -> None:
        self._subs = subscriptions
        self._team_id = team_id
        self._registry: dict[str, TeamDescriptor] = {}

    async def register_team(self, descriptor: TeamDescriptor) -> None:
        self._registry[descriptor.team_id] = descriptor

    async def unregister_team(self, team_id: str) -> None:
        self._registry.pop(team_id, None)

    async def resolve(self, address: Address) -> str:
        if not address.is_local and address.team != self._team_id:
            raise NotImplementedError(
                f"cross-team address {address} needs a V2 bus adapter; "
                f"BUS_ADAPTER=local only routes within team {self._team_id!r}")
        return address.agent

    async def subscribers_for(self, topic: str) -> list[str]:
        return self._subs.subscribers_for(topic)

    async def subscribe(self, topic: str, agent: str) -> None:
        self._subs.subscribe(topic, agent)

    def known_teams(self) -> list[str]:
        return sorted(self._registry)
