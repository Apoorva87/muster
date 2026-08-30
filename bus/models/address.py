"""Logical addressing.

An address names a destination, never a process, container or network
location. ``team://investment/finance`` resolves the same whether the target
runs in this process or on another machine.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEME = "team://"


@dataclass(frozen=True)
class Address:
    agent: str
    team: str | None = None

    @classmethod
    def parse(cls, raw: str) -> "Address":
        if not raw:
            raise ValueError("empty address")
        if not raw.startswith(SCHEME):
            if "/" in raw:
                raise ValueError(f"bare agent name may not contain '/': {raw!r}")
            return cls(agent=raw)
        team, _, agent = raw[len(SCHEME):].partition("/")
        if not team or not agent or "/" in agent:
            raise ValueError(f"malformed address: {raw!r}")
        return cls(agent=agent, team=team)

    @property
    def is_local(self) -> bool:
        """True when no team is named, i.e. 'resolve inside my own team'."""
        return self.team is None

    def qualified(self, default_team: str) -> "Address":
        return self if self.team else Address(agent=self.agent, team=default_team)

    def __str__(self) -> str:
        return f"{SCHEME}{self.team}/{self.agent}" if self.team else self.agent
