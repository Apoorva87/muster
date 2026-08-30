"""Gathers facts. Writes findings as an artifact and announces completion."""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task


@agent("research")
async def research(ctx: AgentContext, task: Task) -> str:
    if task.type != "analyze":
        return {"ignored": task.type}

    findings = await ctx.llm.run(
        instructions=ctx.prompt("research"),
        input=task.objective,
        agent="research",
    )
    ref = await ctx.put_artifact(task=task, content=f"# Research\n\n{findings}\n", type="research")
    await ctx.publish("research.complete", {"artifact_id": ref.id})
    return ref.id
