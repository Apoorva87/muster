"""PRD: 'a workflow can sleep waiting for a human and resume from a button'."""
import asyncio

import pytest

from app.kernel.models import Task, TaskStatus
from app.kernel.runtime import ApprovalDecision


@pytest.fixture
def task(kernel, repo):
    t = Task(project_id="proj_1", type="synthesize", objective="Decide on X",
             assigned_agent="director")
    repo.save_task(t)
    return t


async def _park(kernel, task):
    """Start the approval wait and let it reach the suspend point."""
    pending = asyncio.create_task(
        kernel.request_approval(task=task, prompt="Invest in X?"))
    await asyncio.sleep(0)
    return pending


async def test_workflow_parks_in_waiting_for_human(kernel, repo, task):
    pending = await _park(kernel, task)
    assert repo.get_task(task.id).status is TaskStatus.WAITING_FOR_HUMAN
    pending.cancel()


async def test_awakeable_id_is_persisted_so_the_button_can_resume(kernel, repo, task):
    """Decision D3 — without this the UI has nothing to resolve."""
    pending = await _park(kernel, task)
    waiting = repo.list_waiting_runs("proj_1")
    assert len(waiting) == 1
    assert waiting[0].awakeable_id is not None
    assert waiting[0].awaiting_since is not None
    pending.cancel()


async def test_no_model_is_invoked_while_waiting(kernel, ctx, task):
    """PRD: consumes no model tokens while waiting."""
    pending = await _park(kernel, task)
    assert not any("llm" in name for name in ctx.journal_names())
    assert ctx.sends == [], "a parked workflow must not be dispatching work"
    pending.cancel()


async def test_approve_resumes_the_workflow(kernel, ctx, repo, task):
    pending = await _park(kernel, task)
    awakeable_id = repo.list_waiting_runs("proj_1")[0].awakeable_id

    ctx.resolve_awakeable(awakeable_id, {"decision": "approve"})
    decision = await pending

    assert isinstance(decision, ApprovalDecision)
    assert decision.approved
    assert repo.get_task(task.id).status is TaskStatus.COMPLETE


async def test_reject_resumes_and_marks_rejected(kernel, ctx, repo, task):
    pending = await _park(kernel, task)
    awakeable_id = repo.list_waiting_runs("proj_1")[0].awakeable_id

    ctx.resolve_awakeable(awakeable_id, {"decision": "reject", "note": "too rich"})
    decision = await pending

    assert not decision.approved
    assert decision.note == "too rich"
    assert repo.get_task(task.id).status is TaskStatus.REJECTED


async def test_resolved_run_leaves_the_waiting_queue(kernel, ctx, repo, task):
    pending = await _park(kernel, task)
    awakeable_id = repo.list_waiting_runs("proj_1")[0].awakeable_id
    ctx.resolve_awakeable(awakeable_id, {"decision": "approve"})
    await pending
    assert repo.list_waiting_runs("proj_1") == []


async def test_decision_is_recorded_on_the_timeline(kernel, ctx, repo, task):
    pending = await _park(kernel, task)
    awakeable_id = repo.list_waiting_runs("proj_1")[0].awakeable_id
    ctx.resolve_awakeable(awakeable_id, {"decision": "approve", "note": "ok"})
    await pending

    run = [r for r in repo.list_runs("proj_1")
           if r.event_type == "approval.requested"][0]
    assert run.status == "COMPLETE"
    assert run.output_refs["decision"] == "approve"
    assert run.duration_ms is not None


async def test_resolving_an_unknown_awakeable_raises(ctx):
    with pytest.raises(KeyError):
        ctx.resolve_awakeable("awk_nope", {"decision": "approve"})
