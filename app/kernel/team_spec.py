"""``team.yaml`` — a team's public contract, declared not coded.

This lives in ``app/`` rather than ``bus/`` on purpose: a custom team must work
standalone with the V1 runtime alone. Joining a V2 bus is opt-in, so nothing
here may require the bus to be installed.

A new team owner edits team.yaml, prompts, agents and tools — never Restate
internals, retry logic, routing, the artifact backend or human-resume plumbing.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class SpecError(ValueError):
    """team.yaml is malformed or internally inconsistent."""


class AgentSpec(BaseModel):
    entrypoint: str
    capabilities: list[str] = Field(default_factory=list)
    #: Optional per-agent model selection. Unset means the team default, which
    #: means the deployment default. A cheap local model for routine work and a
    #: stronger one for the critic is the common shape.
    provider: str | None = None
    model: str | None = None
    #: off | read | read-write. Unset inherits the team default. A critic that
    #: remembers past objections is useful; a research agent that accumulates
    #: opinions usually is not.
    memory: str | None = None

    @field_validator("memory", mode="before")
    @classmethod
    def _yaml_off_is_not_a_boolean(cls, value: Any) -> Any:
        """YAML 1.1 reads bare ``off`` as ``False``.

        Everyone writes ``memory: off``, so accept what they meant instead of
        failing with a type error about a boolean they never typed. ``on`` is
        not a permission, so a True gets a message naming the real options.
        """
        if value is False:
            return "off"
        if value is True:
            raise ValueError(
                "memory: on is not a permission — use read or read-write "
                "(and quote it if you meant the string)")
        return value


class SubscriptionSpec(BaseModel):
    topic: str
    agent: str


class PublicSpec(BaseModel):
    commands: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class TeamMeta(BaseModel):
    id: str
    version: int = 1
    description: str = ""

    @field_validator("id")
    @classmethod
    def _short_and_stable(cls, value: str) -> str:
        if not value or not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"team.id must be a short alphanumeric slug, got {value!r}")
        return value


class TeamSpec(BaseModel):
    team: TeamMeta
    agents: dict[str, AgentSpec]
    subscriptions: list[SubscriptionSpec] = Field(default_factory=list)
    public: PublicSpec = Field(default_factory=PublicSpec)

    @property
    def team_id(self) -> str:
        return self.team.id

    @property
    def agent_names(self) -> list[str]:
        return sorted(self.agents)

    def check(self) -> "TeamSpec":
        """Validate what YAML parsing alone cannot.

        Catches the two mistakes a new team owner actually makes: subscribing a
        topic to an agent that does not exist, and publishing a topic nobody in
        the team can produce.
        """
        problems: list[str] = []

        for name, spec in self.agents.items():
            if spec.memory not in (None, "off", "read", "read-write"):
                problems.append(
                    f"agent {name!r}: memory must be off|read|read-write, "
                    f"got {spec.memory!r}")

        for subscription in self.subscriptions:
            if subscription.agent not in self.agents:
                problems.append(
                    f"subscription {subscription.topic!r} -> {subscription.agent!r}: "
                    f"no such agent; team declares {self.agent_names}")

        if not self.agents:
            problems.append("team declares no agents")

        duplicates = {(s.topic, s.agent) for s in self.subscriptions}
        if len(duplicates) != len(self.subscriptions):
            problems.append("duplicate (topic, agent) subscription")

        if problems:
            raise SpecError(
                f"team.yaml for {self.team_id!r} is invalid:\n  - "
                + "\n  - ".join(problems))
        return self

    def load_entrypoints(self) -> dict[str, Any]:
        """Import every declared agent module. Fails loudly on a typo."""
        loaded: dict[str, Any] = {}
        for name, spec in self.agents.items():
            try:
                loaded[name] = importlib.import_module(spec.entrypoint)
            except ImportError as exc:
                raise SpecError(
                    f"agent {name!r} entrypoint {spec.entrypoint!r} "
                    f"is not importable: {exc}") from exc
        return loaded

    def llm_for(self, agent: str) -> tuple[str | None, str | None]:
        """``(provider, model)`` overrides for ``agent``; None means inherit."""
        spec = self.agents.get(agent)
        return (spec.provider, spec.model) if spec else (None, None)

    def memory_for(self, agent: str) -> str | None:
        """``off`` | ``read`` | ``read-write``, or None to inherit."""
        spec = self.agents.get(agent)
        return spec.memory if spec else None

    def subscription_pairs(self) -> list[tuple[str, str]]:
        return [(s.topic, s.agent) for s in self.subscriptions]

    def seed_into(self, repository) -> None:
        """Write this team's routes into the V1 subscription table."""
        for topic, agent in self.subscription_pairs():
            repository.add_subscription(topic, agent)

    def to_descriptor(self):
        """Project to a bus ``TeamDescriptor``.

        Imported lazily so a standalone team never needs the V2 package.
        """
        from bus.models.team import AgentDescriptor, TeamDescriptor

        return TeamDescriptor(
            team_id=self.team.id,
            version=self.team.version,
            description=self.team.description,
            agents=[AgentDescriptor(name=name, capabilities=spec.capabilities)
                    for name, spec in sorted(self.agents.items())],
            subscriptions=self.subscription_pairs(),
            public_topics=self.public.topics,
            public_commands=self.public.commands,
        )


def load_team_spec(path: str | Path) -> TeamSpec:
    path = Path(path)
    if path.is_dir():
        path = path / "team.yaml"
    if not path.is_file():
        raise SpecError(f"no team.yaml at {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError(f"{path} must contain a mapping at the top level")
    return TeamSpec.model_validate(raw).check()
