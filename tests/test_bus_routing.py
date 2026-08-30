"""V2 routing: command resolution, duplicate suppression, topic fan-out.

Hermetic — no Restate, no Postgres, no Docker.
"""

import pytest

from bus.adapters.base import DeliveryError
from bus.models.address import Address
from bus.models.message import Message, MessageKind
from bus.models.team import AgentDescriptor, Health, TeamDescriptor
from bus.routing.commands import CommandRouter
from bus.routing.registry import TeamRegistry
from bus.routing.topics import TopicRouter


# -- fixtures ------------------------------------------------------------

def _team(team_id, agents, subscriptions=()):
    return TeamDescriptor(
        team_id=team_id,
        agents=[AgentDescriptor(name=n, capabilities=c) for n, c in agents],
        subscriptions=list(subscriptions))


@pytest.fixture
def registry():
    r = TeamRegistry(session_id="workstation-01")
    r.register(_team(
        "investment",
        [("director", ["plan"]), ("finance", ["model"]), ("critic", ["challenge"])],
        [("investment.proposal.ready", "critic"),
         ("investment.proposal.ready", "finance"),
         ("system.team.registered", "director")]))
    r.register(_team(
        "research",
        [("web-researcher", ["research_company"])],
        [("investment.proposal.ready", "web-researcher"),
         ("research.report.ready", "web-researcher")]))
    r.register(_team(
        "security",
        [("triage", ["assess"])],
        [("system.team.registered", "triage")]))
    return r


@pytest.fixture
def commands(registry):
    return CommandRouter(registry)


@pytest.fixture
def topics(registry):
    return TopicRouter(registry)


def command(destination="finance", source_team="investment", **kw):
    return Message(kind=MessageKind.COMMAND, session_id="workstation-01",
                   source_team=source_team, source_agent="director",
                   destination=destination, project_id="proj_1", **kw)


def event(topic="investment.proposal.ready", source_team="investment", **kw):
    return Message(kind=MessageKind.EVENT, session_id="workstation-01",
                   source_team=source_team, source_agent="director",
                   topic=topic, project_id="proj_1", **kw)


# -- command routing -----------------------------------------------------

async def test_bare_address_is_qualified_by_the_callers_team(commands):
    """'finance' from the investment team means team://investment/finance."""
    resolved = await commands.route("investment", "finance", command())
    assert resolved == Address(agent="finance", team="investment")
    assert str(resolved) == "team://investment/finance"


async def test_bare_address_does_not_leak_into_another_team(commands):
    """The research team has no 'finance'; qualification must not fall through."""
    with pytest.raises(DeliveryError):
        await commands.route("research", "finance", command(source_team="research"))


async def test_cross_team_command_routes_to_the_right_team_and_agent(commands):
    msg = command(destination="team://research/web-researcher")
    resolved = await commands.route("investment", msg.destination, msg)
    assert (resolved.team, resolved.agent) == ("research", "web-researcher")


async def test_route_accepts_an_address_object_as_well_as_a_string(commands):
    target = Address(agent="triage", team="security")
    assert await commands.route("investment", target, command()) == target


async def test_explicit_address_ignores_the_source_team(commands):
    """An address that names its own team is never re-qualified."""
    resolved = await commands.route(
        "security", Address.parse("team://research/web-researcher"), command())
    assert resolved.team == "research"


async def test_unknown_team_raises_delivery_error_naming_the_roll(commands):
    msg = command(destination="team://marketing/copywriter")
    with pytest.raises(DeliveryError) as exc:
        await commands.route("investment", msg.destination, msg)
    text = str(exc.value)
    assert "marketing" in text                      # what was asked for
    assert "investment" in text and "research" in text   # what exists
    assert msg.id in text                           # which message failed


async def test_unknown_agent_raises_delivery_error_naming_the_agents(commands):
    msg = command(destination="team://research/finance")
    with pytest.raises(DeliveryError) as exc:
        await commands.route("investment", msg.destination, msg)
    text = str(exc.value)
    assert "finance" in text
    assert "web-researcher" in text                 # what the team does expose


async def test_delivery_error_chains_the_registry_error(commands):
    """The actionable message is ours; the cause stays inspectable."""
    msg = command(destination="team://marketing/copywriter")
    with pytest.raises(DeliveryError) as exc:
        await commands.route("investment", msg.destination, msg)
    assert isinstance(exc.value.__cause__, KeyError)


async def test_bare_address_with_no_source_team_is_a_delivery_error(commands):
    with pytest.raises(DeliveryError, match="source_team"):
        await commands.route("", "finance", command(source_team="investment"))


async def test_malformed_address_is_a_delivery_error_not_a_value_error(commands):
    with pytest.raises(DeliveryError, match="malformed"):
        await commands.route("investment", "team://onlyteam", command())


async def test_routing_is_pure_and_repeatable(commands):
    """Routing the same message twice yields the same answer; only seen() ticks."""
    msg = command()
    first = await commands.route("investment", "finance", msg)
    second = await commands.route("investment", "finance", msg)
    assert first == second
    assert commands.seen_count == 0


# -- duplicate suppression ----------------------------------------------

def test_first_sighting_of_a_message_id_is_not_a_duplicate(commands):
    assert commands.seen("msg_1") is False


def test_redelivered_message_id_is_reported_as_seen(commands):
    commands.seen("msg_1")
    assert commands.seen("msg_1") is True
    assert commands.seen("msg_1") is True


def test_distinct_message_ids_do_not_collide(commands):
    assert commands.seen("msg_1") is False
    assert commands.seen("msg_2") is False
    assert commands.seen("msg_1") is True


def test_seen_window_is_bounded_and_evicts_oldest_first(registry):
    router = CommandRouter(registry, seen_capacity=3)
    for i in range(3):
        router.seen(f"msg_{i}")
    assert router.seen_count == 3
    router.seen("msg_3")                    # evicts msg_0
    assert router.seen_count == 3
    assert router.seen("msg_0") is False    # forgotten, so re-admitted
    assert router.seen("msg_2") is True     # still inside the window


def test_seen_window_keeps_recently_repeated_ids(registry):
    router = CommandRouter(registry, seen_capacity=2)
    router.seen("a")
    router.seen("b")
    router.seen("a")          # refreshes 'a'
    router.seen("c")          # evicts 'b', not 'a'
    assert router.seen("a") is True
    assert router.seen("b") is False


def test_seen_capacity_must_be_positive(registry):
    with pytest.raises(ValueError):
        CommandRouter(registry, seen_capacity=0)


def test_forget_reopens_a_message_id(commands):
    commands.seen("msg_1")
    commands.forget("msg_1")
    assert commands.seen("msg_1") is False


# -- topic fan-out -------------------------------------------------------

async def test_fan_out_reaches_subscribers_in_more_than_one_team(topics):
    """The headline V2 criterion: one bus-wide topic wakes several teams."""
    woken = await topics.fan_out("investment.proposal.ready", event())
    assert set(woken) == {
        Address(agent="critic", team="investment"),
        Address(agent="finance", team="investment"),
        Address(agent="web-researcher", team="research"),
    }
    assert len({a.team for a in woken}) == 2


async def test_fan_out_spans_teams_for_a_system_topic(topics):
    woken = await topics.fan_out(
        "system.team.registered", event(topic="system.team.registered"))
    assert {a.team for a in woken} == {"investment", "security"}


async def test_topic_with_no_subscribers_is_not_an_error(topics):
    assert await topics.fan_out("nobody.listens", event(topic="nobody.listens")) == []


async def test_matching_is_exact_a_parent_topic_does_not_match_a_child(topics):
    """PRD defers wildcards: a subscriber to a.b is NOT woken by a.b.c."""
    registry = topics.registry
    registry.register(_team("alpha", [("a1", [])], [("a.b", "a1")]))
    assert await topics.fan_out("a.b", event(topic="a.b")) == [
        Address(agent="a1", team="alpha")]
    assert await topics.fan_out("a.b.c", event(topic="a.b.c")) == []


async def test_matching_is_exact_a_child_topic_does_not_match_a_parent(topics):
    topics.registry.register(_team("alpha", [("a1", [])], [("a.b.c", "a1")]))
    assert await topics.fan_out("a.b", event(topic="a.b")) == []


async def test_no_wildcard_subscription_is_interpreted(topics):
    """A literal '*' subscribes to the literal topic '*', nothing more."""
    topics.registry.register(_team("alpha", [("a1", [])], [("a.*", "a1")]))
    assert await topics.fan_out("a.b", event(topic="a.b")) == []
    assert await topics.fan_out("a.*", event(topic="a.*")) == [
        Address(agent="a1", team="alpha")]


async def test_namespaced_topics_are_plain_strings(topics):
    topics.registry.register(
        _team("alpha", [("a1", [])], [("investment.proposal.ready", "a1")]))
    woken = await topics.fan_out("investment.proposal.ready", event())
    assert Address(agent="a1", team="alpha") in woken


async def test_unreachable_team_is_skipped_not_crashed(topics):
    topics.registry.set_health("research", Health.UNREACHABLE)
    woken = await topics.fan_out("investment.proposal.ready", event())
    assert {a.team for a in woken} == {"investment"}
    assert "research" not in {a.team for a in woken}


async def test_unreachable_team_is_reported(topics):
    topics.registry.set_health("research", Health.UNREACHABLE)
    result = await topics.resolve("investment.proposal.ready", event())
    assert [s.address.team for s in result.skipped] == ["research"]
    assert result.skipped[0].health is Health.UNREACHABLE
    assert "unreachable" in str(result.skipped[0])
    assert topics.last_skipped == result.skipped


async def test_degraded_team_still_receives_events(topics):
    """Restate holds the invocation; degraded is not a routing decision."""
    topics.registry.set_health("research", Health.DEGRADED)
    woken = await topics.fan_out("investment.proposal.ready", event())
    assert Address(agent="web-researcher", team="research") in woken


async def test_every_team_unreachable_yields_an_empty_fan_out(topics):
    for team_id in topics.registry.team_ids():
        topics.registry.set_health(team_id, Health.UNREACHABLE)
    result = await topics.resolve("investment.proposal.ready", event())
    assert result.delivered == []
    assert len(result.skipped) == 3


async def test_last_skipped_resets_between_fan_outs(topics):
    topics.registry.set_health("research", Health.UNREACHABLE)
    await topics.fan_out("investment.proposal.ready", event())
    assert topics.last_skipped
    await topics.fan_out("research.report.ready", event(topic="research.report.ready"))
    # research is the only subscriber to research.report.ready and is skipped;
    # then a topic nobody subscribes to must clear the list entirely.
    await topics.fan_out("nobody.listens", event(topic="nobody.listens"))
    assert topics.last_skipped == []


async def test_fan_out_rejects_an_empty_topic(topics):
    with pytest.raises(ValueError):
        await topics.fan_out("", event())


async def test_unregistering_a_team_removes_its_subscribers(topics):
    topics.registry.unregister("research")
    woken = await topics.fan_out("investment.proposal.ready", event())
    assert {a.team for a in woken} == {"investment"}
