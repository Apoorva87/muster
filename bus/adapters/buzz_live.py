"""Live Buzz control plane: project semantic events, take commands back.

Buzz is a Nostr relay, so this speaks NIP-01/NIP-29 over WebSocket. Two halves:

* **Outbound** — :class:`BuzzControlPlane` posts a human-readable line per
  *semantic* event into a channel. Filtered through ``bus.adapters.buzz``'s
  allow-list, so tool calls, retries, token counts and DB writes never appear.
  Messages carry artifact **references**, never bodies.

* **Inbound** — :class:`BuzzCommandListener` reads the same channel and turns
  human messages into launches and approvals. This is what lets someone drive
  the team from chat instead of the CLI.

Each Muster agent gets its **own** keypair, derived from ``team/agent``. That is
Buzz's model — an agent is a cryptographic identity with a signed history, not a
bot token — and it means a room shows who actually said what.

Buzz remains a control plane, never the transport. Durable coordination stays on
Restate; if the relay is down, the team keeps working and the room goes quiet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from app.kernel.models import RunRecord
from bus.adapters.buzz import is_semantic, topic_for_run
from bus.nostr.events import Event, Identity, chat_message, profile

#: How each semantic topic reads in a room.
TOPIC_STYLE: dict[str, tuple[str, str]] = {
    "task.started": ("▶", "started"),
    "task.completed": ("✓", "completed"),
    "proposal.ready": ("📋", "proposal ready"),
    "critique.ready": ("⚔", "critique ready"),
    "approval.waiting": ("⏸", "needs your decision"),
    "decision.completed": ("🏁", "decision recorded"),
    "system.agent.failed": ("✗", "agent failed"),
    "system.team.failed": ("✗", "team failed"),
}

COMMAND_PATTERN = re.compile(
    r"^\s*(?:@muster\s+)?(run|start|approve|reject|status|help)\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL)


@runtime_checkable
class ChatTransport(Protocol):
    """The narrow surface a control plane needs from a relay."""

    async def post(self, event: Event) -> bool: ...

    def listen(self, channel: str) -> AsyncIterator[Event]: ...


@dataclass(frozen=True)
class Command:
    """A human instruction parsed out of a chat message."""

    verb: str
    argument: str
    author: str
    source_event: str
    channel: str

    @property
    def is_launch(self) -> bool:
        return self.verb in ("run", "start")

    @property
    def is_decision(self) -> bool:
        return self.verb in ("approve", "reject")


def parse_command(event: Event) -> Command | None:
    """Read a chat message as a command, or None if it is ordinary talk.

    Deliberately forgiving — people type in chat, not in a CLI. ``@muster``
    is optional so a dedicated channel needs no prefix at all.
    """
    match = COMMAND_PATTERN.match(event.content or "")
    if match is None:
        return None
    return Command(verb=match.group(1).lower(), argument=match.group(2).strip(),
                   author=event.pubkey, source_event=event.id,
                   channel=event.channel or "")


class AgentIdentities:
    """One keypair per agent, derived so a deployment needs no key management.

    Deterministic derivation is a development convenience: the seed *is* the
    key. A real deployment supplies real secrets via ``register``.
    """

    def __init__(self, namespace: str = "muster") -> None:
        self._namespace = namespace
        self._identities: dict[tuple[str, str], Identity] = {}

    def for_agent(self, team: str, agent: str) -> Identity:
        key = (team, agent)
        if key not in self._identities:
            self._identities[key] = Identity.derive(
                f"{self._namespace}/{team}/{agent}")
        return self._identities[key]

    def register(self, team: str, agent: str, secret_hex: str) -> Identity:
        identity = Identity.from_hex(secret_hex)
        self._identities[(team, agent)] = identity
        return identity

    def known(self) -> dict[str, str]:
        return {f"{t}/{a}": i.pubkey for (t, a), i in sorted(self._identities.items())}


@dataclass
class BuzzControlPlane:
    """Outbound projection into a Buzz channel."""

    transport: ChatTransport
    channel: str
    team_id: str = "investment"
    identities: AgentIdentities = field(default_factory=AgentIdentities)
    #: Posted event ids, so a replay or a re-read does not double-post.
    posted: set[str] = field(default_factory=set)

    async def announce_agents(self, agents: list[str]) -> list[str]:
        """Publish a kind:0 profile per agent so the room shows real names."""
        published: list[str] = []
        for agent in agents:
            identity = self.identities.for_agent(self.team_id, agent)
            event = profile(identity, name=f"{agent} ({self.team_id})",
                            about=f"Muster agent on team {self.team_id}")
            if await self.transport.post(event):
                published.append(identity.pubkey)
        return published

    async def say(self, agent: str, text: str, *,
                  tags: list[list[str]] | None = None,
                  reply_to: str | None = None) -> Event | None:
        """Post as ``agent``. The room attributes it to that agent's key."""
        identity = self.identities.for_agent(self.team_id, agent)
        event = chat_message(identity, self.channel, text,
                             reply_to=reply_to, extra_tags=tags)
        return event if await self.transport.post(event) else None

    async def project_run(self, run: RunRecord, *,
                          artifact_type: str | None = None) -> Event | None:
        """Post one run, if it is semantic. Returns None when filtered out."""
        topic = topic_for_run(run.event_type, artifact_type)
        if topic is None or not is_semantic(topic):
            return None
        if run.id in self.posted:
            return None

        symbol, phrase = TOPIC_STYLE.get(topic, ("·", topic))
        refs = self._references(run)
        line = f"{symbol} **{run.agent}** {phrase}"
        if refs:
            line += f" — {refs}"

        tags = [["t", topic], ["muster-run", run.id]]
        if run.task_id:
            tags.append(["muster-task", run.task_id])
        if run.awakeable_id:
            tags.append(["muster-awakeable", run.awakeable_id])

        event = await self.say(run.agent, line, tags=tags)
        if event is not None:
            self.posted.add(run.id)
        return event

    async def project_timeline(self, runs: list[RunRecord], *,
                               artifact_types: dict[str, str] | None = None
                               ) -> list[Event]:
        """Project a whole timeline, keeping only what a human should see."""
        posted: list[Event] = []
        for run in runs:
            event = await self.project_run(
                run, artifact_type=(artifact_types or {}).get(run.id))
            if event is not None:
                posted.append(event)
        return posted

    async def request_approval(self, run: RunRecord, prompt: str) -> Event | None:
        """Ask the room to decide, binding the durable promise to the message."""
        if not run.awakeable_id:
            raise ValueError(
                f"run {run.id} is not parked on a human — no awakeable to bind")
        text = (f"⏸ **{run.agent}** needs your decision\n\n{prompt}\n\n"
                f"Reply `approve` or `reject`.")
        return await self.say(run.agent, text, tags=[
            ["t", "approval.waiting"], ["muster-run", run.id],
            ["muster-awakeable", run.awakeable_id]])

    @staticmethod
    def _references(run: RunRecord) -> str:
        """Render references only. An artifact body must never reach the room."""
        parts: list[str] = []
        for source in (run.output_refs, run.input_refs):
            for key, value in (source or {}).items():
                if isinstance(value, str) and value.startswith("art_"):
                    parts.append(f"`{value}`")
                elif key in ("decision", "topic") and isinstance(value, str):
                    parts.append(f"{key}: **{value}**")
        return ", ".join(dict.fromkeys(parts))


@dataclass
class BuzzCommandListener:
    """Inbound: chat messages become launches and approvals.

    The listener is deliberately dumb about domain logic. It parses, then hands
    off to a launcher; everything durable stays behind that call.
    """

    transport: ChatTransport
    channel: str
    control: BuzzControlPlane
    #: Pubkeys allowed to command the team. Empty means anyone in the channel.
    allow: set[str] = field(default_factory=set)
    #: Pubkeys belonging to our own agents, so we never obey ourselves.
    ignore: set[str] = field(default_factory=set)

    def accepts(self, event: Event) -> bool:
        if event.pubkey in self.ignore:
            return False
        return not self.allow or event.pubkey in self.allow

    async def commands(self) -> AsyncIterator[Command]:
        async for event in self.transport.listen(self.channel):
            if not self.accepts(event):
                continue
            command = parse_command(event)
            if command is not None:
                yield command

    async def help(self) -> Event | None:
        return await self.control.say("director", HELP_TEXT)


HELP_TEXT = (
    "**Muster** — I take work in this channel.\n"
    "• `run <objective>` — start a project\n"
    "• `approve` / `reject` — answer a pending decision\n"
    "• `status` — what is running and what is waiting\n"
    "I post progress here. Artifacts stay in the team's store; you get references."
)


def summarise(payload: dict[str, Any]) -> str:
    """Compact JSON for a tag value. Keeps rooms readable."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)
