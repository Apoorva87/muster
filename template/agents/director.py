"""A coordinator. Include one only if the team genuinely needs coordination.

A two-agent team often does not: if the work is a single step, delete this file
and let the specialist be the whole team.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

TEAM = "myteam"


@agent("director", team=TEAM)
async def director(ctx: AgentContext, task: Task) -> dict:
    if task.type == "do_the_thing":
        await ctx.send("specialist", "work", objective=task.objective,
                       parent_task_id=task.id)
        return {"dispatched": ["specialist"]}

    if task.type == "on:work.complete":
        # Park on a human only where the decision actually warrants it.
        decision = await ctx.request_approval(task, prompt="Accept this result?")
        return {"decision": decision.decision}

    return {"ignored": task.type}
