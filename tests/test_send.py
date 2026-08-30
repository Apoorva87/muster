"""send() — a targeted command. PRD: 'one command durably wakes another agent'."""
from app.kernel.models import TaskStatus


async def test_send_issues_exactly_one_durable_invocation(kernel, ctx):
    await kernel.send(agent="finance", task="analyze", objective="Value X")
    assert len(ctx.sends) == 1


async def test_send_targets_the_named_agent_keyed_by_project(kernel, ctx):
    await kernel.send(agent="finance", task="analyze", objective="Value X")
    sent = ctx.sends[0]
    assert sent.agent == "finance"
    assert sent.key == "proj_1", "Virtual Object key is the project (decision D2)"


async def test_send_persists_a_task(kernel, repo):
    task = await kernel.send(agent="finance", task="analyze", objective="Value X")
    stored = repo.get_task(task.id)
    assert stored is not None
    assert stored.assigned_agent == "finance"
    assert stored.type == "analyze"
    assert stored.status is TaskStatus.PENDING


async def test_send_payload_carries_references_not_transcripts(kernel, ctx):
    await kernel.send(agent="critic", task="review", objective="Challenge it",
                      input_refs={"proposal": "art_1"})
    payload = ctx.sends[0].payload
    assert payload["task_id"].startswith("task_")
    assert payload["input_refs"] == {"proposal": "art_1"}
    assert "transcript" not in payload and "scratchpad" not in payload


async def test_send_records_a_timeline_run(kernel, repo):
    task = await kernel.send(agent="finance", task="analyze", objective="o")
    runs = repo.list_runs("proj_1")
    assert [r.event_type for r in runs] == ["task.sent"]
    assert runs[0].task_id == task.id


async def test_send_is_replay_safe(make_kernel, ctx):
    """Restate replays handlers; a replayed send must not mint a new task ID."""
    first = await make_kernel().send(agent="finance", task="analyze", objective="o")
    ctx.replay()
    second = await make_kernel().send(agent="finance", task="analyze", objective="o")
    assert first.id == second.id


async def test_send_carries_idempotency_key(kernel, ctx):
    task = await kernel.send(agent="finance", task="analyze", objective="o")
    assert ctx.sends[0].idempotency_key == task.id


async def test_send_supports_parent_lineage(kernel, repo):
    parent = await kernel.send(agent="director", task="coordinate", objective="o")
    child = await kernel.send(agent="research", task="dig", objective="o",
                              parent_task_id=parent.id)
    assert repo.get_task(child.id).parent_task_id == parent.id
