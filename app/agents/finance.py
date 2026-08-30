"""Valuation and financial analysis. Also reacts to market.changed."""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task


@agent("finance")
async def finance(ctx: AgentContext, task: Task) -> str:
    # finance also subscribes to proposal.ready and market.changed. Only the
    # analysis it was directly asked for announces finance.complete — otherwise
    # director re-proposes and the team loops forever.
    if task.type != "analyze":
        return {"ignored": task.type}

    analysis = await ctx.llm.run(
        instructions=ctx.prompt("finance"),
        input=task.objective,
        agent="finance",
    )
    ref = await ctx.put_artifact(task=task, content=f"# Valuation\n\n{analysis}\n", type="valuation")
    await ctx.publish("finance.complete", {"artifact_id": ref.id})
    return ref.id
