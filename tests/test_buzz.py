"""Buzz as a control plane: what reaches a human room, and what never does."""
import pytest

from app.kernel.models import RunRecord
from bus.adapters.buzz import (NEVER_PROJECTED, SEMANTIC_TOPICS, is_semantic,
                               topic_for_run)
from bus.adapters.buzz_live import (AgentIdentities, BuzzCommandListener,
                                    BuzzControlPlane, parse_command)
from bus.nostr.events import KIND_CHAT, KIND_PROFILE, Identity, chat_message


class FakeTransport:
    """Records posts; replays a scripted inbound stream."""

    def __init__(self, inbound=None):
        self.posted = []
        self._inbound = list(inbound or [])
        self.accept = True

    async def post(self, event):
        event.require_valid()          # every post must be properly signed
        self.posted.append(event)
        return self.accept

    async def listen(self, channel):
        for event in self._inbound:
            if event.channel == channel:
                yield event


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def control(transport):
    return BuzzControlPlane(transport=transport, channel="chan-1",
                            team_id="investment")


def run(event_type, agent="research", **kw):
    return RunRecord(project_id="p1", agent=agent, event_type=event_type, **kw)


# ----------------------------------------------------------- the allow-list

def test_only_semantic_topics_are_projectable():
    assert SEMANTIC_TOPICS.isdisjoint(NEVER_PROJECTED)
    assert all(not is_semantic(t) for t in NEVER_PROJECTED)


def test_internal_run_events_have_no_semantic_topic():
    """A human room is not a log file."""
    for internal in ("event.delivered", "event.published", "wakeup.scheduled"):
        assert topic_for_run(internal) is None


def test_artifact_kinds_map_to_their_own_topics():
    assert topic_for_run("event.published", "proposal") == "proposal.ready"
    assert topic_for_run("event.published", "critique") == "critique.ready"
    assert topic_for_run("event.published", "synthesis") == "decision.completed"


async def test_a_filtered_run_is_never_posted(control, transport):
    assert await control.project_run(run("event.delivered")) is None
    assert transport.posted == []


async def test_a_semantic_run_is_posted_as_a_chat_message(control, transport):
    event = await control.project_run(run("task.sent"))
    assert event is not None
    assert event.kind == KIND_CHAT
    assert event.channel == "chan-1", "NIP-29 requires the h tag"
    assert event.tag_value("t") == "task.started"
    assert event.verify()


async def test_projection_is_idempotent(control, transport):
    record = run("task.sent")
    await control.project_run(record)
    await control.project_run(record)
    assert len(transport.posted) == 1, "a replay must not double-post"


async def test_a_timeline_projects_only_its_semantic_lines(control, transport):
    runs = [run("task.sent"), run("event.delivered"), run("event.published"),
            run("approval.requested", agent="director", awakeable_id="awk_1")]
    posted = await control.project_timeline(runs)
    assert len(posted) == 2
    assert {e.tag_value("t") for e in posted} == {"task.started", "approval.waiting"}


# -------------------------------------------------------- references only

async def test_artifact_references_are_posted_never_bodies(control, transport):
    record = run("task.sent", output_refs={"artifact_id": "art_abc123"})
    event = await control.project_run(record)
    assert "art_abc123" in event.content
    assert len(event.content) < 300, "a room gets a line, not a document"


async def test_a_decision_is_rendered_for_humans(control):
    event = await control.project_run(
        run("task.completed", agent="director", output_refs={"decision": "approve"}))
    assert "approve" in event.content


# ------------------------------------------------------------- identities

def test_every_agent_gets_its_own_keypair():
    ids = AgentIdentities()
    keys = {a: ids.for_agent("investment", a).pubkey
            for a in ("director", "research", "finance", "critic")}
    assert len(set(keys.values())) == 4, "agents must be distinguishable in a room"


def test_agent_identity_is_stable_across_restarts():
    assert (AgentIdentities().for_agent("t", "a").pubkey
            == AgentIdentities().for_agent("t", "a").pubkey)


def test_teams_do_not_collide_on_agent_names():
    ids = AgentIdentities()
    assert ids.for_agent("investment", "director").pubkey != \
           ids.for_agent("research", "director").pubkey


def test_a_real_secret_can_be_registered():
    ids = AgentIdentities()
    real = Identity.generate()
    assert ids.register("t", "a", real.secret_hex).pubkey == real.pubkey


async def test_agents_announce_profiles_so_the_room_shows_names(control, transport):
    await control.announce_agents(["director", "critic"])
    assert [e.kind for e in transport.posted] == [KIND_PROFILE, KIND_PROFILE]
    assert "director" in transport.posted[0].content


# --------------------------------------------------------------- approval

async def test_approval_binds_the_durable_promise_to_the_message(control):
    record = run("approval.requested", agent="director", awakeable_id="awk_42")
    event = await control.request_approval(record, "Approve art_x?")
    assert event.tag_value("muster-awakeable") == "awk_42"
    assert "approve" in event.content.lower() and "reject" in event.content.lower()


async def test_approval_without_an_awakeable_is_refused(control):
    with pytest.raises(ValueError, match="not parked on a human"):
        await control.request_approval(run("approval.requested"), "?")


# ---------------------------------------------------------------- inbound

def _msg(text, author=None):
    return chat_message(author or Identity.derive("human/apoorva"), "chan-1", text)


@pytest.mark.parametrize("text,verb", [
    ("run Evaluate Acme", "run"),
    ("start Evaluate Acme", "start"),
    ("@muster run Evaluate Acme", "run"),
    ("approve", "approve"),
    ("  REJECT  ", "reject"),
    ("status", "status"),
    ("help", "help"),
])
def test_commands_parse(text, verb):
    assert parse_command(_msg(text)).verb == verb


@pytest.mark.parametrize("text", ["nice work", "running late today", "", "approved?"])
def test_ordinary_chat_is_not_a_command(text):
    assert parse_command(_msg(text)) is None


def test_a_launch_command_keeps_its_objective():
    command = parse_command(_msg("run Evaluate whether Acme is cheap at 31x"))
    assert command.is_launch
    assert command.argument == "Evaluate whether Acme is cheap at 31x"


async def test_the_listener_ignores_our_own_agents(control):
    """Otherwise a posted line could be read back as an instruction."""
    ours = control.identities.for_agent("investment", "director")
    inbound = [chat_message(ours, "chan-1", "run something"),
               _msg("run a real objective")]
    listener = BuzzCommandListener(
        transport=FakeTransport(inbound), channel="chan-1", control=control,
        ignore={ours.pubkey})
    assert [c.argument async for c in listener.commands()] == ["a real objective"]


async def test_an_allow_list_restricts_who_can_command(control):
    boss = Identity.derive("human/boss")
    inbound = [_msg("run from a stranger"), _msg("run from the boss", boss)]
    listener = BuzzCommandListener(
        transport=FakeTransport(inbound), channel="chan-1", control=control,
        allow={boss.pubkey})
    assert [c.argument async for c in listener.commands()] == ["from the boss"]


async def test_the_listener_only_reads_its_own_channel(control):
    other = chat_message(Identity.derive("human/x"), "chan-2", "run elsewhere")
    listener = BuzzCommandListener(transport=FakeTransport([other]),
                                   channel="chan-1", control=control)
    assert [c async for c in listener.commands()] == []
