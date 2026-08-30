"""Timed monitoring.

Wakes, runs a cheap deterministic check, publishes only if something material
changed, schedules its own next wakeup, and exits. No LLM ever polls.
"""

from __future__ import annotations

from datetime import timedelta

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

DEFAULT_INTERVAL = timedelta(hours=6)


@agent("monitor")
async def monitor(ctx: AgentContext, task: Task) -> dict:
    reading = await ctx.probe("market")

    if reading.get("changed"):
        await ctx.publish("market.changed", {k: v for k, v in reading.items()
                                             if k != "changed"})

    # Schedule the next check, then return so the process can exit.
    await ctx.wake_later(DEFAULT_INTERVAL, agent="monitor",
                         reason="recheck market conditions")
    return reading
