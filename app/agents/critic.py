"""Adversarial review.

Receives facts and the proposal artifact — never the proposer's reasoning.
That independence is the whole point of a separate critic agent.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task


@agent("critic")
async def critic(ctx: AgentContext, task: Task) -> str:
    proposal_id = task.input_refs.get("proposal") or task.input_refs.get("artifact_id")
    proposal = await ctx.artifacts.get(proposal_id) if proposal_id else task.objective

    objections = await ctx.llm.run(
        instructions=ctx.prompt("critic"),
        input=proposal,
        agent="critic",
    )
    ref = await ctx.put_artifact(task=task, content=f"# Critique\n\n{objections}\n", type="critique")
    await ctx.publish("critique.complete", {"artifact_id": ref.id})
    return ref.id
