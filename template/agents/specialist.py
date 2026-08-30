"""A specialist agent. Copy this and write your domain behaviour.

Notice what is NOT here: no Restate import, no retry handling, no scheduling,
no routing, no artifact paths, no database access. If you find yourself needing
any of those, the abstraction is wrong and should be fixed centrally rather
than worked around here.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

TEAM = "myteam"


@agent("specialist", team=TEAM)
async def specialist(ctx: AgentContext, task: Task) -> str:
    # Load only what was explicitly handed to you. Anything not in
    # task.input_refs is deliberately unavailable.
    inputs = {name: await ctx.artifacts.get(ref)
              for name, ref in task.input_refs.items()}

    result = await ctx.llm.run(
        instructions=ctx.prompt("specialist"),
        input=task.objective + ("\n\n" + "\n".join(inputs.values()) if inputs else ""),
        agent="specialist",
    )

    # Large output becomes an artifact; other agents get the reference.
    ref = await ctx.put_artifact(task=task, content=result, type="markdown")
    await ctx.publish("work.complete", {"artifact_id": ref.id})
    return ref.id
