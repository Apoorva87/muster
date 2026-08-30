"""wake_later() — durable future invocation. PRD: 'a timer wakes a dormant agent'."""
from datetime import timedelta


async def test_wake_later_schedules_a_delayed_send(kernel, ctx):
    await kernel.wake_later(agent="monitor", delay=timedelta(hours=6))
    assert len(ctx.sends) == 1
    assert ctx.sends[0].delay == timedelta(hours=6)


async def test_wake_later_targets_the_named_agent(kernel, ctx):
    await kernel.wake_later(agent="monitor", delay=timedelta(minutes=5))
    assert ctx.sends[0].agent == "monitor"


async def test_wake_later_does_not_block(kernel, ctx):
    """The process must exit; Restate owns the timer. No sleeping in-handler."""
    await kernel.wake_later(agent="monitor", delay=timedelta(days=1))
    assert ctx.journal_names().count("sleep") == 0


async def test_wake_later_preserves_the_exact_delay(kernel, ctx):
    for delta in (timedelta(seconds=30), timedelta(hours=6), timedelta(days=7)):
        ctx.sends.clear()
        await kernel.wake_later(agent="monitor", delay=delta)
        assert ctx.sends[0].delay == delta


async def test_wake_later_records_the_reason(kernel, ctx, repo):
    await kernel.wake_later(agent="monitor", delay=timedelta(hours=6),
                            reason="recheck market conditions")
    assert ctx.sends[0].payload["reason"] == "recheck market conditions"
    runs = repo.list_runs("proj_1")
    assert runs[0].event_type == "wakeup.scheduled"


async def test_agent_can_schedule_its_own_next_wakeup(kernel, ctx):
    """PRD acceptance: 'an agent can schedule its own future wakeup'."""
    await kernel.wake_later(agent="monitor", delay=timedelta(hours=1))
    assert ctx.sends[0].agent == "monitor" and ctx.sends[0].delay is not None


async def test_wake_later_is_replay_safe(make_kernel, ctx):
    await make_kernel().wake_later(agent="monitor", delay=timedelta(hours=6))
    first = ctx.sends[0].payload["task_id"]
    ctx.replay()
    await make_kernel().wake_later(agent="monitor", delay=timedelta(hours=6))
    assert ctx.sends[0].payload["task_id"] == first
