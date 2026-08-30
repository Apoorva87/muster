"""Buzz control-plane seam — INTERFACE ONLY, implementation deferred.

Buzz is a **control plane**, not the bus. It carries human/agent rooms,
approvals, agent identities and a searchable semantic audit trail. It is *not*
the durable execution engine, and Buzz/Nostr events are *not* our internal wire
protocol — that stays ``bus.models.message.Message`` over Restate.

The projection is one-directional and lossy on purpose: only **semantic** events
reach Buzz. See ``SEMANTIC_TOPICS``. Mirroring every tool call, retry, token
count or DB operation into a human room would turn an audit trail into a log
file and make approvals unfindable — the PRD forbids it explicitly.

The V2 PRD allows keeping this adapter "implemented/optional" and retaining the
V1 local UI as the default control plane. We take the lighter option: define the
seam, add **no dependency** (Buzz would bring a Nostr relay and its own local
deployment weight), and leave the local timeline UI as the control plane.

What V2-complete would do
-------------------------
* Filter every published bus event through :func:`is_semantic`, dropping the rest.
* Map ``session_id`` -> room, ``source_team``/``source_agent`` -> agent identity,
  ``project_id``/``task_id`` -> thread, so history stays searchable per project.
* Post a human-readable progress line carrying **references** (``artifact_refs``),
  never the artifact body.
* For an approval, post the prompt and hand the human response back as a durable
  signal that resolves the workflow's awakeable — no LLM polls while waiting.
* Run every post inside ``ctx.run_typed(...)`` so a replay does not double-post;
  Buzz is an external side effect like any other.
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

#: Why every entry point below raises. Asserted by the contract test.
DEFERRED = (
    "Buzz is an optional control plane, not the durable execution engine. The "
    "V2 PRD permits keeping the adapter optional and retaining the V1 local UI "
    "as the default control plane; this is that deferral."
)


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


class BuzzControlPlaneAdapter:
    """Stub Buzz projector. Not implemented.

    Shaped as a projector rather than a ``BusAdapter``: Buzz observes the bus,
    it never routes for it. Keeping those two roles apart is what stops Buzz
    from being mistaken for the transport.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(f"BuzzControlPlaneAdapter is deferred. {DEFERRED}")

    async def ensure_room(self, session_id: str) -> str:
        raise NotImplementedError(DEFERRED)

    async def project(self, event: Message) -> str | None:
        raise NotImplementedError(DEFERRED)

    async def request_approval(self, event: Message, awakeable_id: str) -> str:
        raise NotImplementedError(DEFERRED)

    async def agent_identity(self, team_id: str, agent: str) -> dict[str, Any]:
        raise NotImplementedError(DEFERRED)
