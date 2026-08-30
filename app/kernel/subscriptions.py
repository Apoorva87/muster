"""Logical topic -> agent routing.

V1 stores routes in Postgres and resolves them in-process; there is no broker.
The abstraction is the point: V2 replaces the implementation with a bus adapter
without any agent code changing (V1 PRD, "Topic/subscription implementation").
"""

from __future__ import annotations

from app.db.repository import Repository


class SubscriptionRegistry:
    def __init__(self, repository: Repository) -> None:
        self._repo = repository

    def subscribers_for(self, topic: str) -> list[str]:
        """Agents that should be woken for ``topic``. Unknown topic -> ``[]``."""
        return self._repo.subscribers_for(topic)

    def subscribe(self, topic: str, agent: str) -> None:
        self._repo.add_subscription(topic, agent)

    def topics(self) -> list[str]:
        return sorted({s.topic for s in self._repo.list_subscriptions()})
