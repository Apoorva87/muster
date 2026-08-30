"""Buzz control-plane seam — the contract and the projection allow-list.

The live implementation is ``bus.adapters.buzz_live``. This module stays free of
any transport so the allow-list can be imported and tested without a relay.

Buzz is a **control plane**, not the bus. It carries human/agent rooms,
approvals, agent identities and a searchable semantic audit trail. It is *not*
the durable execution engine, and Buzz/Nostr events are *not* our internal wire
protocol — that stays ``bus.models.message.Message`` over Restate.

The projection is one-directional and lossy on purpose: only **semantic** events
reach Buzz. See ``SEMANTIC_TOPICS``. Mirroring every tool call, retry, token
count or DB operation into a human room would turn an audit trail into a log
file and make approvals unfindable — the PRD forbids it explicitly.

The V1 local timeline UI remains the default control plane; Buzz is opt-in and
needs the ``buzz`` extra plus a relay.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from bus.models.message import Message

#: The only event topics that may be projected into Buzz. Exact matches, in
#: keeping with the bus's exact-topic routing. Namespaced by the emitting
#: domain; ``system.*`` events come from the bus itself.
SEMANTIC_TOPICS: frozenset[str] = frozenset({
    "task.started",
    "task.completed",
    "proposal.ready",
    "critique.ready",
    "approval.waiting",
    "decision.completed",
    "system.agent.failed",
    "system.team.failed",
})

#: Never projected. Listed so the exclusion is testable rather than folklore.
NEVER_PROJECTED: frozenset[str] = frozenset({
    "tool.called",
    "tool.returned",
    "step.retried",
    "llm.tokens.used",
    "db.write",
})

#: Muster's internal run events, mapped onto the semantic vocabulary a human
#: reads in a room. Anything absent from this map is invisible to Buzz — the
#: allow-list direction, so a new internal event never leaks by accident.
RUN_EVENT_TO_TOPIC: dict[str, str] = {
    "task.sent": "task.started",
    "approval.requested": "approval.waiting",
    "task.completed": "task.completed",
    "run.failed": "system.agent.failed",
}

#: Artifact types that carry their own semantic topic when produced.
ARTIFACT_TO_TOPIC: dict[str, str] = {
    "proposal": "proposal.ready",
    "critique": "critique.ready",
    # NOT decision.completed — a synthesis is written *before* the human is
    # asked, so announcing a decision here would tell the room the call was
    # made while the workflow is still parked waiting for it.
    "synthesis": "task.completed",
}


def is_semantic(topic: str | None) -> bool:
    """True when ``topic`` is one a human should see in a Buzz room.

    Allow-list, not deny-list: a new internal topic is invisible to Buzz until
    someone deliberately adds it. That is the safe direction to fail.
    """
    return topic in SEMANTIC_TOPICS


@runtime_checkable
class BuzzProjector(Protocol):
    """The narrow surface the bus would drive on a Buzz deployment."""

    async def ensure_room(self, session_id: str) -> str:
        """Room for a bus session. Returns the room ID."""

    async def project(self, event: Message) -> str | None:
        """Post one semantic event. Returns a post ID, or ``None`` if filtered."""

    async def request_approval(self, event: Message, awakeable_id: str) -> str:
        """Post an approval prompt bound to a durable awakeable."""

    async def agent_identity(self, team_id: str, agent: str) -> dict[str, Any]:
        """The published identity for one agent in the room."""


def topic_for_run(event_type: str, artifact_type: str | None = None) -> str | None:
    """The semantic topic for a Muster run event, or None if it stays internal.

    Shaped as a projector input rather than a ``BusAdapter`` call: Buzz observes
    the bus, it never routes for it. Keeping those two roles apart is what stops
    Buzz from being mistaken for the transport.
    """
    if artifact_type and artifact_type in ARTIFACT_TO_TOPIC:
        return ARTIFACT_TO_TOPIC[artifact_type]
    return RUN_EVENT_TO_TOPIC.get(event_type)
