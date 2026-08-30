"""V2 adapters: the Restate-backed bus, plus the deferred A2A/Buzz seams.

Hermetic — no Restate server, no Postgres, no Docker. The adapter reaches
Restate only through ``KernelContext``, so a ``FakeKernelContext`` is enough.
"""

import ast
import pathlib

import pytest

from app.kernel.context import FakeKernelContext
from bus.adapters import a2a, buzz
from bus.adapters.base import BusAdapter, DeliveryError
from bus.adapters.restate import HANDLER, RestateBusAdapter
from bus.models.address import Address
from bus.models.message import Message, MessageKind
from bus.models.team import AgentDescriptor, Health, TeamDescriptor
from bus.routing.registry import TeamRegistry

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# -- fixtures ------------------------------------------------------------

def _team(team_id, agents, subscriptions=()):
    return TeamDescriptor(
        team_id=team_id,
        agents=[AgentDescriptor(name=n, capabilities=c) for n, c in agents],
        subscriptions=list(subscriptions))


@pytest.fixture
def contexts():
    """One FakeKernelContext per team, created on demand."""
    return {}


@pytest.fixture
def ctx_factory(contexts):
    def factory(team_id: str) -> FakeKernelContext:
        return contexts.setdefault(team_id, FakeKernelContext(key=team_id))
    return factory


@pytest.fixture
def registry():
    return TeamRegistry(session_id="workstation-01")


@pytest.fixture
async def adapter(registry, ctx_factory):
    bus = RestateBusAdapter(registry, ctx_factory)
    await bus.register_team(_team(
        "investment",
        [("director", ["plan"]), ("finance", ["model"]), ("critic", ["challenge"])],
        [("investment.proposal.ready", "critic"),
         ("investment.proposal.ready", "finance")]))
    await bus.register_team(_team(
        "research",
        [("web-researcher", ["research_company"])],
        [("investment.proposal.ready", "web-researcher")]))
    return bus


def command(destination="team://research/web-researcher", **kw):
    return Message(kind=MessageKind.COMMAND, session_id="workstation-01",
                   source_team="investment", source_agent="director",
                   destination=destination, project_id="proj_1",
                   payload={"company": "ACME"}, **kw)


def event(topic="investment.proposal.ready", **kw):
    return Message(kind=MessageKind.EVENT, session_id="workstation-01",
                   source_team="investment", source_agent="director",
                   topic=topic, project_id="proj_1", **kw)


# -- protocol conformance -------------------------------------------------

def test_adapter_satisfies_the_bus_adapter_protocol(registry, ctx_factory):
    assert isinstance(RestateBusAdapter(registry, ctx_factory), BusAdapter)


def test_adapter_imports_no_restate_module():
    """CLAUDE.md: no Restate SDK type may reach the bus. KernelContext is the seam."""
    for module in ("bus/adapters/restate.py", "bus/adapters/base.py",
                   "bus/adapters/a2a.py", "bus/adapters/buzz.py",
                   "bus/routing/commands.py", "bus/routing/topics.py"):
        tree = ast.parse((REPO_ROOT / module).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "restate" not in imported, module


def test_adapter_module_states_restate_is_the_durability_authority():
    from bus.adapters import restate
    doc = restate.__doc__.lower()
    assert "durability authority" in doc
    assert "routing" in doc


# -- registration ---------------------------------------------------------

async def test_registration_round_trips(adapter, registry):
    assert registry.team_ids() == ["investment", "research"]
    await adapter.unregister_team("research")
    assert registry.team_ids() == ["investment"]


async def test_registration_is_idempotent(adapter, registry):
    await adapter.register_team(_team("research", [("web-researcher", [])]))
    assert registry.team_ids().count("research") == 1


# -- cross-team send ------------------------------------------------------

async def test_send_reaches_the_target_team_only(adapter, contexts):
    await adapter.send(Address.parse("team://research/web-researcher"), command())
    assert [s.agent for s in contexts["research"].sends] == ["web-researcher"]
    assert "investment" not in contexts


async def test_send_carries_the_envelope_and_the_idempotency_key(adapter, contexts):
    msg = command()
    await adapter.send(Address.parse(msg.destination), msg)
    sent = contexts["research"].sends[0]
    assert sent.handler == HANDLER
    assert sent.key == "proj_1"
    assert sent.idempotency_key == msg.id
    assert sent.payload["id"] == msg.id
    assert sent.payload["payload"] == {"company": "ACME"}
    assert sent.delay is None


async def test_send_keys_by_session_when_there_is_no_project(adapter, contexts):
    msg = Message(kind=MessageKind.COMMAND, session_id="workstation-01",
                  source_team="investment", source_agent="director",
                  destination="team://research/web-researcher")
    await adapter.send(Address.parse(msg.destination), msg)
    assert contexts["research"].sends[0].key == "workstation-01"


async def test_send_qualifies_a_bare_destination_with_the_source_team(adapter, contexts):
    await adapter.send(Address.parse("finance"), command(destination="finance"))
    assert [s.agent for s in contexts["investment"].sends] == ["finance"]


async def test_send_to_unknown_team_raises_delivery_error(adapter, contexts):
    with pytest.raises(DeliveryError, match="marketing"):
        await adapter.send(Address.parse("team://marketing/copywriter"),
                           command(destination="team://marketing/copywriter"))
    assert contexts == {}


async def test_send_to_unknown_agent_raises_delivery_error(adapter, contexts):
    with pytest.raises(DeliveryError, match="web-researcher"):
        await adapter.send(Address.parse("team://research/finance"),
                           command(destination="team://research/finance"))
    assert contexts == {}


# -- duplicate suppression -----------------------------------------------

async def test_duplicate_message_id_does_no_duplicate_work(adapter, contexts):
    """V2 acceptance criterion 8."""
    msg = command()
    await adapter.send(Address.parse(msg.destination), msg)
    await adapter.send(Address.parse(msg.destination), msg)
    await adapter.send(Address.parse(msg.destination), msg)
    assert len(contexts["research"].sends) == 1


async def test_two_distinct_messages_both_get_through(adapter, contexts):
    await adapter.send(Address.parse("team://research/web-researcher"), command())
    await adapter.send(Address.parse("team://research/web-researcher"), command())
    assert len(contexts["research"].sends) == 2


async def test_duplicate_event_is_not_fanned_out_twice(adapter, contexts):
    evt = event()
    first = await adapter.publish(evt.topic, evt)
    second = await adapter.publish(evt.topic, evt)
    assert len(first) == 3 and second == []
    assert len(contexts["research"].sends) == 1
    assert len(contexts["investment"].sends) == 2


async def test_duplicate_is_dropped_before_it_reaches_restate(adapter, contexts):
    """Suppression is a routing guard; it must not invent a second invocation."""
    msg = command()
    await adapter.send(Address.parse(msg.destination), msg)
    keys = [s.idempotency_key for s in contexts["research"].sends]
    await adapter.send(Address.parse(msg.destination), msg)
    assert [s.idempotency_key for s in contexts["research"].sends] == keys


# -- cross-team publish ---------------------------------------------------

async def test_publish_wakes_subscribers_in_more_than_one_team(adapter, contexts):
    """The headline V2 criterion, end to end through the adapter."""
    woken = await adapter.publish("investment.proposal.ready", event())
    assert {a.team for a in woken} == {"investment", "research"}
    assert sorted(s.agent for s in contexts["investment"].sends) == ["critic", "finance"]
    assert [s.agent for s in contexts["research"].sends] == ["web-researcher"]


async def test_publish_is_exact_topic_only(adapter, contexts):
    assert await adapter.publish("investment.proposal", event(topic="investment.proposal")) == []
    assert await adapter.publish(
        "investment.proposal.ready.v2", event(topic="investment.proposal.ready.v2")) == []
    assert contexts == {}


async def test_fan_out_gives_each_subscriber_its_own_idempotency_key(adapter, contexts):
    """One event, N invocations: N distinct keys, all derived from the event ID."""
    evt = event()
    await adapter.publish(evt.topic, evt)
    keys = [s.idempotency_key
            for ctx in contexts.values() for s in ctx.sends]
    assert len(keys) == len(set(keys)) == 3
    assert all(k.startswith(evt.id) for k in keys)


async def test_publish_skips_an_unreachable_team_without_crashing(adapter, contexts,
                                                                  registry):
    registry.set_health("research", Health.UNREACHABLE)
    woken = await adapter.publish("investment.proposal.ready", event())
    assert {a.team for a in woken} == {"investment"}
    assert "research" not in contexts
    assert [s.address.team for s in adapter.last_skipped] == ["research"]


async def test_publish_with_no_subscribers_wakes_nobody(adapter, contexts):
    assert await adapter.publish("nobody.listens", event(topic="nobody.listens")) == []
    assert contexts == {}


# -- subscriptions --------------------------------------------------------

async def test_subscribe_adds_a_live_subscription(adapter, contexts):
    await adapter.subscribe("research.report.ready",
                            Address(agent="director", team="investment"))
    woken = await adapter.publish("research.report.ready",
                                  event(topic="research.report.ready"))
    assert woken == [Address(agent="director", team="investment")]


async def test_subscribe_is_idempotent(adapter, registry):
    addr = Address(agent="director", team="investment")
    await adapter.subscribe("research.report.ready", addr)
    await adapter.subscribe("research.report.ready", addr)
    subs = registry.get("investment").subscriptions
    assert sum(1 for s in subs if tuple(s) == ("research.report.ready", "director")) == 1


async def test_unsubscribe_removes_it_again(adapter, registry):
    await adapter.unsubscribe("investment.proposal.ready",
                              Address(agent="critic", team="investment"))
    woken = await adapter.publish("investment.proposal.ready", event())
    assert Address(agent="critic", team="investment") not in woken


async def test_unsubscribe_is_a_no_op_when_not_subscribed(adapter, registry):
    before = list(registry.get("investment").subscriptions)
    await adapter.unsubscribe("never.subscribed",
                              Address(agent="critic", team="investment"))
    assert registry.get("investment").subscriptions == before


async def test_subscribe_rejects_an_unqualified_address(adapter):
    with pytest.raises(DeliveryError, match="qualify"):
        await adapter.subscribe("a.b", Address(agent="critic"))


async def test_subscribe_rejects_an_unknown_team(adapter):
    with pytest.raises(DeliveryError, match="marketing"):
        await adapter.subscribe("a.b", Address(agent="copywriter", team="marketing"))


async def test_subscribe_rejects_an_unknown_agent(adapter):
    with pytest.raises(DeliveryError, match="web-researcher"):
        await adapter.subscribe("a.b", Address(agent="ghost", team="research"))


# -- correlation across a routed hop -------------------------------------

async def test_caused_preserves_correlation_and_sets_causation_across_a_hop(
        adapter, contexts):
    """Criterion 10: trace/correlation IDs connect the cross-team path."""
    origin = command(correlation_id=None, trace_id="trace-abc")
    await adapter.send(Address.parse(origin.destination), origin)

    # research completes and publishes; the follow-on is derived from what it got.
    received = Message(**contexts["research"].sends[0].payload)
    follow_on = received.caused(
        kind=MessageKind.EVENT, topic="research.report.ready",
        source_team="research", source_agent="web-researcher")

    assert follow_on.correlation_id == origin.id     # the chain's root
    assert follow_on.causation_id == origin.id       # its immediate parent
    assert follow_on.trace_id == "trace-abc"
    assert follow_on.id != origin.id

    await adapter.subscribe("research.report.ready",
                            Address(agent="director", team="investment"))
    await adapter.publish(follow_on.topic, follow_on)

    delivered = Message(**contexts["investment"].sends[0].payload)
    assert delivered.correlation_id == origin.id
    assert delivered.causation_id == origin.id
    assert delivered.source_team == "research"


async def test_correlation_survives_three_hops(adapter, contexts):
    first = command()
    second = first.caused(kind=MessageKind.EVENT, topic="research.report.ready",
                          source_team="research", source_agent="web-researcher")
    third = second.caused(kind=MessageKind.COMMAND, destination="finance",
                          source_team="investment", source_agent="director")
    assert third.correlation_id == first.id     # root is preserved, not rewritten
    assert third.causation_id == second.id      # parent is the previous hop


# -- deferred seams: A2A --------------------------------------------------

def test_a2a_endpoint_is_a_protocol_describing_the_seam():
    from typing import Protocol
    assert issubclass(a2a.A2AEndpoint, Protocol)
    for op in ("agent_card", "submit_task", "get_task", "accept_task"):
        assert hasattr(a2a.A2AEndpoint, op)


def test_a2a_adapter_is_an_unimplemented_stub():
    with pytest.raises(NotImplementedError, match="deferred"):
        a2a.A2ABusAdapter()


def test_a2a_module_documents_why_it_is_deferred():
    assert "interoperability" in a2a.__doc__
    assert "not the internal bus" in a2a.__doc__
    assert "V2-complete would do" in a2a.__doc__
    assert "deferral" in a2a.DEFERRED


def test_a2a_adds_no_dependency():
    tree = ast.parse((REPO_ROOT / "bus/adapters/a2a.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "bus"}


@pytest.mark.integration
@pytest.mark.skip(reason="A2A implementation deferred; see bus/adapters/a2a.py")
async def test_a2a_adapter_contract(registry, ctx_factory):
    """Contract an A2A adapter must satisfy once implemented.

    Deselected by default (``-m 'not integration'``) and skipped besides: it
    needs a live external A2A agent, which V2 defers.
    """
    adapter = a2a.A2ABusAdapter(registry, ctx_factory)
    assert isinstance(adapter, BusAdapter)
    await adapter.register_team(_team("remote", [("external", ["summarize"])]))
    msg = command(destination="team://remote/external")
    await adapter.send(Address.parse(msg.destination), msg)
    # ... and the remote task's completion must arrive back as a bus EVENT
    # carrying the same correlation_id, with artifacts passed by reference.


# -- deferred seams: Buzz -------------------------------------------------

def test_buzz_projector_is_a_protocol_describing_the_seam():
    from typing import Protocol
    assert issubclass(buzz.BuzzProjector, Protocol)
    for op in ("ensure_room", "project", "request_approval", "agent_identity"):
        assert hasattr(buzz.BuzzProjector, op)


def test_buzz_adapter_is_an_unimplemented_stub():
    with pytest.raises(NotImplementedError, match="deferred"):
        buzz.BuzzControlPlaneAdapter()


def test_buzz_projects_only_semantic_events():
    """PRD: task started/completed, proposal, critique, approval, failure, decision."""
    for topic in ("task.started", "task.completed", "proposal.ready",
                  "critique.ready", "approval.waiting", "decision.completed",
                  "system.agent.failed", "system.team.failed"):
        assert buzz.is_semantic(topic), topic


def test_buzz_never_projects_tool_calls_retries_tokens_or_db_operations():
    for topic in buzz.NEVER_PROJECTED:
        assert not buzz.is_semantic(topic), topic
    assert not buzz.SEMANTIC_TOPICS & buzz.NEVER_PROJECTED


def test_buzz_filter_is_an_allow_list():
    """An unknown internal topic stays invisible until deliberately added."""
    assert not buzz.is_semantic("some.new.internal.topic")
    assert not buzz.is_semantic(None)


def test_buzz_module_documents_why_it_is_deferred():
    collapsed = " ".join(buzz.__doc__.split()).replace("*", "")
    assert "not the durable execution engine" in collapsed
    assert "not our internal wire protocol" in collapsed
    assert "V2-complete would do" in buzz.__doc__
    assert "deferral" in buzz.DEFERRED


def test_buzz_adds_no_dependency():
    tree = ast.parse((REPO_ROOT / "bus/adapters/buzz.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "bus"}


@pytest.mark.integration
@pytest.mark.skip(reason="Buzz implementation deferred; see bus/adapters/buzz.py")
async def test_buzz_projection_contract():
    """Contract a Buzz projector must satisfy once implemented.

    Deselected by default and skipped besides: it needs a running Buzz/Nostr
    stack, which V2 keeps optional in favour of the V1 local timeline UI.
    """
    projector = buzz.BuzzControlPlaneAdapter(endpoint="http://localhost:0")
    await projector.ensure_room("workstation-01")
    assert await projector.project(event(topic="proposal.ready")) is not None
    assert await projector.project(event(topic="tool.called")) is None
