"""Coordinates the team and owns the human decision point.

Holds no transcript. Progress is inferred from persisted semantic state
(which tasks completed, which artifacts exist), not from a running conversation.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

#: Both must land before a proposal can be assembled.
REQUIRED_INPUTS = ("research", "finance")


@agent("director")
async def director(ctx: AgentContext, task: Task) -> dict:
    if task.type in ("evaluate_company", "coordinate"):
        return await _kick_off(ctx, task)
    if task.type == "on:research.complete" or task.type == "on:finance.complete":
        return await _maybe_propose(ctx, task)
    if task.type == "on:critique.complete":
        return await _synthesize(ctx, task)
    return {"ignored": task.type}


async def _kick_off(ctx: AgentContext, task: Task) -> dict:
    for specialist in REQUIRED_INPUTS:
        await ctx.send(specialist, "analyze", objective=task.objective,
                       parent_task_id=task.id)
    return {"dispatched": list(REQUIRED_INPUTS)}


async def _maybe_propose(ctx: AgentContext, task: Task) -> dict:
    """Assemble a proposal once both specialists have produced an artifact."""
    artifacts = ctx.project_artifacts()

    # Idempotent at the semantic level: proposal.ready fans out to finance, so
    # without this guard a second finance.complete would re-propose endlessly.
    existing = next((a for a in artifacts if a.type == "proposal"), None)
    if existing is not None:
        return {"already_proposed": existing.id}

    by_agent = {a.created_by: a for a in artifacts if a.type in ("research", "valuation")}
    if not all(name in by_agent for name in REQUIRED_INPUTS):
        return {"waiting_for": [n for n in REQUIRED_INPUTS if n not in by_agent]}

    proposal = await ctx.put_artifact(
        task=task, created_by="director", type="proposal",
        content="# Proposal\n\n" + "\n".join(
            f"- {name}: {by_agent[name].id}" for name in REQUIRED_INPUTS))

    # The critic receives the proposal by reference — not the reasoning behind it.
    await ctx.publish("proposal.ready", {"artifact_id": proposal.id})
    return {"proposal": proposal.id}


async def _synthesize(ctx: AgentContext, task: Task) -> dict:
    synthesis = await ctx.llm.run(
        instructions=ctx.prompt("director"),
        input=task.objective,
        agent="director",
    )
    ref = await ctx.put_artifact(task=task, created_by="director",
                                 type="synthesis",
                                 content=f"# Synthesis\n\n{synthesis}\n")
    decision = await ctx.request_approval(task, prompt=f"Approve {ref.id}?")
    return {"synthesis": ref.id, "decision": decision.decision}
