"""Agent behaviour. Registration, fan-out participation, and the timer loop."""
from datetime import timedelta

import pytest

import app.agents  # registers all five
from app.agents.base import AgentContext, StubLLMRunner, dispatch, registered_agents
from app.kernel.models import Task


@pytest.fixture
def agent_ctx(kernel):
    async def changed_market():
        return {"changed": True, "delta": 0.21}
    return AgentContext(kernel=kernel, llm=StubLLMRunner(),
                        probes={"market": changed_market})


@pytest.fixture
def quiet_ctx(kernel):
    async def flat_market():
        return {"changed": False}
    return AgentContext(kernel=kernel, llm=StubLLMRunner(),
                        probes={"market": flat_market})


def make_task(agent_name, type_, objective="Evaluate Company X", **kw):
    return Task(project_id="proj_1", type=type_, objective=objective,
                assigned_agent=agent_name, **kw)


def test_all_prd_agents_are_registered():
    """The investment team's agents register unscoped, so V1 code reaches them
    by bare name. Other teams register under their own scope."""
    assert set(registered_agents(team="")) == {
        "critic", "director", "finance", "monitor", "research"}


async def test_unknown_agent_raises_a_useful_error(agent_ctx):
    with pytest.raises(KeyError, match="unknown agent"):
        await dispatch("nosuchagent", agent_ctx, make_task("x", "y"))


async def test_research_writes_an_artifact_and_announces(agent_ctx, ctx, repo):
    artifact_id = await dispatch("research", agent_ctx, make_task("research", "analyze"))
    assert artifact_id.startswith("art_")
    assert "research.complete" in {e.topic for e in repo.list_events("proj_1")}
    # research.complete subscribes director
    assert "director" in {s.agent for s in ctx.sends}


async def test_finance_publishes_finance_complete(agent_ctx, repo):
    await dispatch("finance", agent_ctx, make_task("finance", "analyze"))
    assert "finance.complete" in {e.topic for e in repo.list_events("proj_1")}


async def test_critic_reads_the_proposal_by_reference(agent_ctx, store, repo):
    ref = await store.put(project_id="proj_1", task_id="t", created_by="director",
                          content="# Proposal\n\nBuy at 20x.")
    task = make_task("critic", "review", input_refs={"proposal": ref.id})
    out = await dispatch("critic", agent_ctx, task)
    assert out.startswith("art_")
    assert "critique.complete" in {e.topic for e in repo.list_events("proj_1")}


async def test_director_dispatches_both_specialists(agent_ctx, ctx):
    result = await dispatch("director", agent_ctx,
                            make_task("director", "evaluate_company"))
    assert result == {"dispatched": ["research", "finance"]}
    assert {s.agent for s in ctx.sends} == {"research", "finance"}


async def test_director_waits_until_both_inputs_land(agent_ctx, repo, store):
    from app.kernel.models import Artifact
    repo.save_artifact(Artifact(project_id="proj_1", task_id="t", type="research",
                                path="/x.md", created_by="research"))
    result = await dispatch("director", agent_ctx,
                            make_task("director", "on:research.complete"))
    assert result == {"waiting_for": ["valuation"]}


async def test_director_publishes_proposal_once_both_land(agent_ctx, repo):
    from app.kernel.models import Artifact
    for who, kind in (("research", "research"), ("finance", "valuation")):
        repo.save_artifact(Artifact(project_id="proj_1", task_id="t", type=kind,
                                    path=f"/{who}.md", created_by=who))
    result = await dispatch("director", agent_ctx,
                            make_task("director", "on:finance.complete"))
    assert "proposal" in result
    assert "proposal.ready" in {e.topic for e in repo.list_events("proj_1")}


async def test_monitor_publishes_only_on_material_change(agent_ctx, repo):
    await dispatch("monitor", agent_ctx, make_task("monitor", "wakeup"))
    assert "market.changed" in {e.topic for e in repo.list_events("proj_1")}


async def test_monitor_stays_quiet_when_nothing_changed(quiet_ctx, repo):
    await dispatch("monitor", quiet_ctx, make_task("monitor", "wakeup"))
    assert "market.changed" not in {e.topic for e in repo.list_events("proj_1")}


async def test_monitor_reschedules_itself_then_exits(quiet_ctx, ctx):
    """PRD: 'an agent can schedule its own future wakeup'. No polling LLM."""
    await dispatch("monitor", quiet_ctx, make_task("monitor", "wakeup"))
    scheduled = [s for s in ctx.sends if s.delay is not None]
    assert len(scheduled) == 1
    assert scheduled[0].agent == "monitor"
    assert scheduled[0].delay == timedelta(hours=6)


async def test_market_changed_fans_out_to_two_agents(agent_ctx, ctx):
    """PRD demo: market.changed must wake at least two subscribers."""
    await dispatch("monitor", agent_ctx, make_task("monitor", "wakeup"))
    woken = {s.agent for s in ctx.sends if s.delay is None}
    assert {"finance", "director"} <= woken
