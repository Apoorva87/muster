"""Team registry — the muster roll.

Deliberately not a service-discovery framework. A team registers a descriptor
at startup; the bus keeps a small durable record and can list teams by ID or
capability. That is all local multi-team coordination needs.
"""

from __future__ import annotations

from bus.models.address import Address
from bus.models.team import Health, TeamDescriptor


class UnknownTeam(KeyError):
    pass


class UnknownAgent(KeyError):
    pass


class TeamRegistry:
    def __init__(self, session_id: str = "workstation-01") -> None:
        self.session_id = session_id
        self._teams: dict[str, TeamDescriptor] = {}

    def register(self, descriptor: TeamDescriptor) -> TeamDescriptor:
        """Idempotent: re-registering replaces the descriptor for that team."""
        self._teams[descriptor.team_id] = descriptor
        return descriptor

    def unregister(self, team_id: str) -> None:
        self._teams.pop(team_id, None)

    def get(self, team_id: str) -> TeamDescriptor:
        try:
            return self._teams[team_id]
        except KeyError:
            raise UnknownTeam(
                f"no team {team_id!r} in session {self.session_id!r}; "
                f"registered: {sorted(self._teams)}") from None

    def teams(self) -> list[TeamDescriptor]:
        return [self._teams[k] for k in sorted(self._teams)]

    def team_ids(self) -> list[str]:
        return sorted(self._teams)

    def resolve(self, address: Address) -> tuple[str, str]:
        """Validate an address against the roll. Returns ``(team_id, agent)``."""
        if address.is_local:
            raise ValueError(
                f"address {address} names no team; qualify it with the caller's "
                "team before resolving")
        descriptor = self.get(address.team)
        if not descriptor.has_agent(address.agent):
            raise UnknownAgent(
                f"team {address.team!r} has no agent {address.agent!r}; "
                f"it exposes: {descriptor.agent_names}")
        return address.team, address.agent

    def find_capability(self, capability: str) -> list[Address]:
        return [Address(agent=agent, team=team.team_id)
                for team in self.teams()
                for agent in team.agents_with(capability)]

    def subscribers_for(self, topic: str) -> list[Address]:
        """Every registered agent subscribed to ``topic``, across all teams."""
        return [Address(agent=agent, team=team.team_id)
                for team in self.teams()
                for subscribed_topic, agent in team.subscriptions
                if subscribed_topic == topic]

    def set_health(self, team_id: str, health: Health) -> None:
        self.get(team_id).health = health
