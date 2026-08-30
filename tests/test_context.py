"""The KernelContext seam: what makes every other test runnable with no server."""
from datetime import timedelta

from app.kernel.context import FakeKernelContext, KernelContext


def test_fake_satisfies_the_protocol():
    assert isinstance(FakeKernelContext(key="proj_1"), KernelContext)


def test_fake_records_sends():
    ctx = FakeKernelContext(key="proj_1")
    ctx.send(agent="finance", handler="handle", key="proj_1", payload={"a": 1})
    assert len(ctx.sends) == 1
    sent = ctx.sends[0]
    assert (sent.agent, sent.handler, sent.key) == ("finance", "handle", "proj_1")
    assert sent.payload == {"a": 1}
    assert sent.delay is None


def test_fake_records_delay():
    ctx = FakeKernelContext(key="proj_1")
    ctx.send(agent="monitor", handler="handle", key="proj_1", payload={},
             delay=timedelta(hours=6))
    assert ctx.sends[0].delay == timedelta(hours=6)


async def test_awakeable_returns_id_and_pending_promise():
    ctx = FakeKernelContext(key="proj_1")
    awakeable_id, promise = ctx.awakeable()
    assert awakeable_id.startswith("awk_")
    ctx.resolve_awakeable(awakeable_id, {"decision": "approve"})
    assert await promise == {"decision": "approve"}


async def test_run_typed_executes_once_and_is_journalled():
    ctx = FakeKernelContext(key="proj_1")
    calls = []

    async def step():
        calls.append(1)
        return "done"

    assert await ctx.run_typed("fetch", step) == "done"
    assert ctx.journal == [("fetch", "done")]
    assert len(calls) == 1


async def test_replay_does_not_re_execute_journalled_steps():
    """Restate skips completed steps on replay; the fake models that."""
    ctx = FakeKernelContext(key="proj_1")
    calls = []

    async def step():
        calls.append(1)
        return "done"

    await ctx.run_typed("fetch", step)
    ctx.replay()
    assert await ctx.run_typed("fetch", step) == "done"
    assert len(calls) == 1, "a replayed step must not re-execute its side effect"
