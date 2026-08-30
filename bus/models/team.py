"""What a team publishes about itself when it registers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"


class AgentDescriptor(BaseModel):
    name: str
    capabilities: list[str] = Field(default_factory=list)


class TeamDescriptor(BaseModel):
    team_id: str
    version: int = 1
    description: str = ""
    agents: list[AgentDescriptor] = Field(default_factory=list)
    subscriptions: list[tuple[str, str]] = Field(default_factory=list)
    public_topics: list[str] = Field(default_factory=list)
    public_commands: list[str] = Field(default_factory=list)
    health: Health = Health.HEALTHY
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def agent_names(self) -> list[str]:
        return [a.name for a in self.agents]

    def has_agent(self, name: str) -> bool:
        return any(a.name == name for a in self.agents)

    def agents_with(self, capability: str) -> list[str]:
        return [a.name for a in self.agents if capability in a.capabilities]
