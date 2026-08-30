"""publish() — logical fan-out. PRD: 'one topic wakes multiple subscribed agents'."""


async def test_publish_wakes_every_subscriber(kernel, ctx):
    await kernel.publish(topic="proposal.ready", payload={"artifact_id": "art_1"})
    assert {s.agent for s in ctx.sends} == {"critic", "finance"}


async def test_fanout_targets_distinct_objects_so_they_run_in_parallel(kernel, ctx):
    """Decision D2: distinct agent objects, same key -> concurrent, not serialized."""
    await kernel.publish(topic="proposal.ready", payload={})
    agents = [s.agent for s in ctx.sends]
    assert len(agents) == len(set(agents)), "no agent woken twice for one event"
    assert {s.key for s in ctx.sends} == {"proj_1"}


async def test_market_changed_wakes_at_least_two_subscribers(kernel, ctx):
    """PRD demo requirement: proves fan-out."""
    await kernel.publish(topic="market.changed", payload={"delta": 0.12})
    assert len(ctx.sends) >= 2


async def test_unknown_topic_is_not_an_error(kernel, ctx):
    event = await kernel.publish(topic="nobody.listens", payload={})
    assert ctx.sends == []
    assert event.topic == "nobody.listens"


async def test_event_is_persisted_even_with_no_subscribers(kernel, repo):
    await kernel.publish(topic="nobody.listens", payload={})
    assert [e.topic for e in repo.list_events("proj_1")] == ["nobody.listens"]


async def test_each_subscriber_gets_its_own_task(kernel, repo, ctx):
    await kernel.publish(topic="proposal.ready", payload={})
    task_ids = [s.payload["task_id"] for s in ctx.sends]
    assert len(set(task_ids)) == 2
    assert {repo.get_task(t).assigned_agent for t in task_ids} == {"critic", "finance"}


async def test_event_payload_is_small(kernel, ctx):
    """PRD: events carry metadata and references, not LLM output."""
    await kernel.publish(topic="proposal.ready", payload={"artifact_id": "art_1"})
    assert all(len(str(s.payload)) < 1024 for s in ctx.sends)


async def test_publish_is_replay_safe(make_kernel, ctx):
    first = await make_kernel().publish(topic="proposal.ready", payload={})
    first_tasks = sorted(s.payload["task_id"] for s in ctx.sends)
    ctx.replay()
    second = await make_kernel().publish(topic="proposal.ready", payload={})
    assert first.id == second.id
    assert sorted(s.payload["task_id"] for s in ctx.sends) == first_tasks


async def test_publish_records_the_event_on_the_timeline(kernel, repo):
    await kernel.publish(topic="proposal.ready", payload={})
    assert "event.published" in {r.event_type for r in repo.list_runs("proj_1")}
