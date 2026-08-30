"""The research team's only agent.

One agent, one capability. V3's topology rule: add an agent when it needs a
different role, different tools, independent context, parallelism or a
different wakeup lifecycle — not to simulate a job title.

Note what is absent: no Restate import, no retry logic, no routing, no timer
code. This is the whole point of the template — a team owner writes domain
behaviour and nothing else.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

TEAM = "research"


@agent("web-researcher", team=TEAM)
async def web_researcher(ctx: AgentContext, task: Task) -> str:
    """Research a company and publish the report by reference."""
    report = await ctx.llm.run(
        instructions=ctx.prompt("web-researcher"),
        input=task.objective,
        agent="web-researcher",
    )
    ref = await ctx.put_artifact(
        task=task, created_by="web-researcher", type="research",
        content=f"# Research report\n\n**Subject:** {task.objective}\n\n{report}\n")

    # Namespaced so it is meaningful outside this team.
    await ctx.publish("research.report.ready", {"artifact_id": ref.id})
    return ref.id
