"""The PRD's Day-1 demonstration, driven end to end in-process.

    User -> Director -> Research + Finance -> proposal.ready -> Critic
         -> critique.complete -> Director synthesis -> WAITING_FOR_HUMAN
         -> Approve -> complete

A small driver drains the FakeKernelContext's sends and dispatches each to its
agent, which is exactly what Restate does for real. This proves the whole
choreography without a server.
"""
import asyncio

import pytest

import app.agents  # registers all five
from app.agents.base import AgentContext, StubLLMRunner, dispatch
from app.kernel.models import Task, TaskStatus
from app.kernel.runtime import Kernel
from app.kernel.subscriptions import SubscriptionRegistry


async def _drive(ctx, repo, store, *, decision="approve", rounds=40):
    """Dispatch every queued send until the system goes quiet."""
    seen: set[int] = set()
    inflight: list[asyncio.Task] = []

    for _ in range(rounds):
        pending = [s for s in ctx.sends if id(s) not in seen and s.delay is None]
        for send in pending:
            seen.add(id(send))
            task = repo.get_task(send.payload["task_id"])
            # One journal per invocation, exactly as Restate scopes it.
            kernel = Kernel(ctx=ctx.invocation(), repository=repo,
                            subscriptions=SubscriptionRegistry(repo), artifacts=store)
            agent_ctx = AgentContext(kernel=kernel, llm=StubLLMRunner())
            inflight.append(asyncio.create_task(dispatch(send.agent, agent_ctx, task)))

        await asyncio.sleep(0)

        # Stand in for a human pressing the button in the local UI.
        for run in repo.list_waiting_runs(ctx.key):
            ctx.resolve_awakeable(run.awakeable_id, {"decision": decision})
        await asyncio.sleep(0)

        if not pending and all(t.done() for t in inflight):
            break

    await asyncio.wait_for(asyncio.gather(*inflight), timeout=10)


@pytest.fixture
async def flow(ctx, repo, store):
    kernel = Kernel(ctx=ctx, repository=repo,
                    subscriptions=SubscriptionRegistry(repo), artifacts=store)
    root = await kernel.send(agent="director", task="evaluate_company",
                             objective="Evaluate whether Company X is attractive "
                                       "at its current valuation.")
    await _drive(ctx, repo, store)
    return root


async def test_every_agent_participates(flow, repo):
    agents = {r.agent for r in repo.list_runs("proj_1")}
    assert {"director", "research", "finance", "critic"} <= agents


async def test_the_full_topic_choreography_fires(flow, repo):
    topics = {e.topic for e in repo.list_events("proj_1")}
    assert topics >= {"research.complete", "finance.complete",
                      "proposal.ready", "critique.complete"}


async def test_proposal_reaches_the_critic(flow, repo):
    critic_tasks = [t for t in repo.list_tasks("proj_1")
                    if t.assigned_agent == "critic"]
    assert critic_tasks, "critic was never woken"
    assert any(t.input_refs for t in critic_tasks), "critic got no artifact reference"


async def test_critic_never_receives_another_agents_reasoning(flow, repo, store):
    """The core thesis: independence is preserved across the whole flow."""
    critic_task = next(t for t in repo.list_tasks("proj_1")
                       if t.assigned_agent == "critic")
    for ref in critic_task.input_refs.values():
        body = await store.get(ref)
        assert "scratchpad" not in body.lower()
        assert "reasoning" not in body.lower()


async def test_artifacts_are_registered_not_just_written(flow, repo):
    artifacts = repo.list_artifacts("proj_1")
    assert {a.created_by for a in artifacts} >= {"research", "finance", "director", "critic"}
    assert all(a.path for a in artifacts)


async def test_workflow_reaches_a_human_decision_and_completes(flow, repo):
    approval = [r for r in repo.list_runs("proj_1")
                if r.event_type == "approval.requested"]
    assert approval, "the flow never asked a human"
    assert approval[0].output_refs["decision"] == "approve"
    assert repo.list_waiting_runs("proj_1") == []


async def test_rejection_path_marks_the_task_rejected(ctx, repo, store):
    kernel = Kernel(ctx=ctx, repository=repo,
                    subscriptions=SubscriptionRegistry(repo), artifacts=store)
    await kernel.send(agent="director", task="evaluate_company", objective="Evaluate X")
    await _drive(ctx, repo, store, decision="reject")

    synth = [t for t in repo.list_tasks("proj_1")
             if t.status is TaskStatus.REJECTED]
    assert synth, "a rejected decision must mark its task REJECTED"


async def test_timeline_tells_the_whole_story(flow, repo):
    runs = repo.list_runs("proj_1")
    assert runs == sorted(runs, key=lambda r: r.started_at)
    assert len(runs) >= 8
