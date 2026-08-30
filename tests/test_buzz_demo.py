"""End to end: a team driven entirely from a Buzz room, over a real relay.

Exercises the same path as `demo/buzz_session.py` but as assertions. Needs no
Docker, no Buzz binary and no model endpoint — the relay is Muster's own
DevRelay, which speaks the real NIP-01/29/42 protocol.
"""
import asyncio
import time

import pytest

pytest.importorskip("coincurve", reason="needs the 'buzz' extra: uv sync --extra buzz")
pytest.importorskip("websockets", reason="needs the 'buzz' extra: uv sync --extra buzz")

from app.launcher import Launcher
from bus.adapters.buzz_live import (AgentIdentities, BuzzCommandListener,
                                    BuzzControlPlane)
from bus.adapters.buzz_transport import connect
from bus.nostr.dev_relay import DevRelay
from bus.nostr.events import KIND_CHAT, Identity, chat_message

CHANNEL = "muster-test"
TEAM = "investment"
AGENTS = ["director", "research", "finance", "critic"]


@pytest.fixture
async def room(tmp_path):
    async with DevRelay() as relay:
        identities = AgentIdentities()
        human = Identity.derive("human/tester")
        human_client, human_tx = await connect(relay.url, human)
        bot_client, bot_tx = await connect(
            relay.url, identities.for_agent(TEAM, "director"))
        control = BuzzControlPlane(transport=bot_tx, channel=CHANNEL,
                                   team_id=TEAM, identities=identities)
        listener = BuzzCommandListener(
            transport=human_tx, channel=CHANNEL, control=control,
            ignore={identities.for_agent(TEAM, a).pubkey for a in AGENTS})
        launcher = Launcher(teams=[f"teams/{TEAM}"], artifact_root=tmp_path)
        try:
            yield {"relay": relay, "control": control, "listener": listener,
                   "launcher": launcher, "human": human, "human_tx": human_tx,
                   "identities": identities}
        finally:
            await human_client.close()
            await bot_client.close()


async def say(room, text):
    await room["human_tx"].post(chat_message(room["human"], CHANNEL, text))
    await asyncio.sleep(0.1)


async def next_command(room, timeout=5.0):
    return await asyncio.wait_for(anext(aiter(room["listener"].commands())), timeout)


async def test_a_chat_message_starts_real_work(room):
    await say(room, "run Evaluate Acme at 31x")
    command = await next_command(room)
    assert command.is_launch and command.argument == "Evaluate Acme at 31x"

    result = await room["launcher"].launch(command.argument, auto_approve=None)
    assert result.runs
    assert len(result.waiting) == 1, "the workflow should park on the human"


async def test_the_room_shows_progress_but_not_internals(room):
    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    types = {a.task_id: a.type for a in result.artifacts}
    await room["control"].project_timeline(
        result.runs, artifact_types={r.id: types.get(r.task_id) for r in result.runs})
    await asyncio.sleep(0.1)

    chat = [e for e in room["relay"].channel(CHANNEL) if e.kind == KIND_CHAT]
    topics = {e.tag_value("t") for e in chat}
    assert "proposal.ready" in topics
    assert topics.isdisjoint({"event.delivered", "event.published",
                              "wakeup.scheduled", "tool.called"})


async def test_every_event_in_the_relay_is_signed_and_verifies(room):
    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    await room["control"].project_timeline(result.runs)
    await asyncio.sleep(0.1)
    stored = room["relay"].events
    assert stored and all(e.verify() for e in stored)


async def test_artifact_bodies_never_reach_the_room(room):
    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    types = {a.task_id: a.type for a in result.artifacts}
    await room["control"].project_timeline(
        result.runs, artifact_types={r.id: types.get(r.task_id) for r in result.runs})
    await asyncio.sleep(0.1)
    for event in room["relay"].channel(CHANNEL):
        assert len(event.content) < 500, "a room gets references, not documents"


async def test_each_agent_speaks_under_its_own_identity(room):
    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    await room["control"].project_timeline(result.runs)
    await asyncio.sleep(0.1)

    authors = {e.pubkey for e in room["relay"].channel(CHANNEL) if e.kind == KIND_CHAT}
    expected = {room["identities"].for_agent(TEAM, a).pubkey for a in AGENTS}
    assert len(authors & expected) >= 2, "the room must distinguish who spoke"


async def test_approve_from_chat_resolves_the_durable_promise(room):
    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    parked = result.waiting[0]
    await room["control"].request_approval(parked, f"Approve {parked.task_id}?")

    await say(room, "approve")
    answer = await next_command(room)
    assert answer.is_decision and answer.verb == "approve"

    await room["launcher"].resolve(parked.id, answer.verb)
    assert room["launcher"].waiting() == [], "the workflow never resumed"


async def test_reject_from_chat_takes_the_rejection_path(room):
    from app.kernel.models import TaskStatus

    result = await room["launcher"].launch("Evaluate Acme", auto_approve=None)
    parked = result.waiting[0]
    await say(room, "reject")
    answer = await next_command(room)
    await room["launcher"].resolve(parked.id, answer.verb)

    repo = room["launcher"].repository_for(TEAM)
    assert any(t.status is TaskStatus.REJECTED
               for t in repo.list_tasks(result.project_id))


async def test_the_team_keeps_working_if_the_room_is_unreachable(room, tmp_path):
    """Buzz is a control plane, not the transport. A dead relay must not stop work."""
    await room["relay"].stop()
    launcher = Launcher(teams=[f"teams/{TEAM}"], artifact_root=tmp_path)
    result = await launcher.launch("Evaluate Acme", auto_approve="approve")
    assert result.runs, "durable coordination must not depend on Buzz"


async def test_ordinary_chat_never_launches_work(room):
    """A room is a conversation; only commands are instructions."""
    for chatter in ("nice work team", "running late", "I approve of this design"):
        await say(room, chatter)
    got = []
    with pytest.raises(asyncio.TimeoutError):
        got.append(await next_command(room, timeout=0.4))
    assert got == []
