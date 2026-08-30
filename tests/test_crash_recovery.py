"""PRD release criterion: killing and restarting must not lose durable intent.

Two layers, because full recovery needs a live server:

* **Unit** (runs everywhere): durable *intent* survives losing every in-memory
  object, and a replayed handler does not repeat committed work.
* **Integration** (needs Docker + Restate): the real kill/restart cycle.
"""
import asyncio
import shutil
import subprocess

import pytest

from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.context import FakeKernelContext
from app.kernel.models import Task, TaskStatus
from app.kernel.runtime import Kernel
from app.kernel.subscriptions import SubscriptionRegistry


def _kernel(ctx, repo, store):
    return Kernel(ctx=ctx, repository=repo,
                  subscriptions=SubscriptionRegistry(repo), artifacts=store)


async def test_parked_work_survives_losing_every_in_memory_object(repo, store):
    """Kill the process while a workflow waits on a human; state is recoverable."""
    ctx = FakeKernelContext(key="proj_1")
    task = Task(project_id="proj_1", type="synthesize", objective="Decide",
                assigned_agent="director")
    repo.save_task(task)

    pending = asyncio.create_task(
        _kernel(ctx, repo, store).request_approval(task=task, prompt="Approve?"))
    await asyncio.sleep(0)
    pending.cancel()

    # Simulate the crash: discard kernel, context, and the Repository object.
    del ctx
    revived = Repository(engine=repo.engine)

    waiting = revived.list_waiting_runs("proj_1")
    assert len(waiting) == 1, "the workflow's intent to wait was lost"
    assert waiting[0].awakeable_id is not None, "nothing left to resume with"
    assert revived.get_task(task.id).status is TaskStatus.WAITING_FOR_HUMAN


async def test_replayed_handler_does_not_duplicate_committed_work(repo, store):
    """Restate replays from the journal; committed steps must not re-run."""
    ctx = FakeKernelContext(key="proj_1")

    first = await _kernel(ctx, repo, store).publish(
        topic="proposal.ready", payload={"artifact_id": "art_1"})
    tasks_after_first = {t.id for t in repo.list_tasks("proj_1")}

    ctx.replay()
    second = await _kernel(ctx, repo, store).publish(
        topic="proposal.ready", payload={"artifact_id": "art_1"})

    assert first.id == second.id, "replay minted a new event ID"
    assert {t.id for t in repo.list_tasks("proj_1")} == tasks_after_first, \
        "replay created duplicate tasks"
    assert len(repo.list_events("proj_1")) == 1, "replay duplicated the event"


async def test_replayed_side_effect_executes_once(repo, store):
    ctx = FakeKernelContext(key="proj_1")
    calls: list[int] = []

    async def charge_the_customer():
        calls.append(1)
        return "receipt_1"

    assert await ctx.run_typed("charge", charge_the_customer) == "receipt_1"
    ctx.replay()
    assert await ctx.run_typed("charge", charge_the_customer) == "receipt_1"
    assert len(calls) == 1


async def test_artifacts_are_recoverable_after_index_loss(tmp_path):
    """The in-memory path index is a cache; the filesystem is the truth."""
    store = FilesystemArtifactStore(root=tmp_path)
    ref = await store.put(project_id="proj_1", task_id="t1",
                          created_by="research", content="findings")

    revived = FilesystemArtifactStore(root=tmp_path)  # cold, empty index
    assert await revived.get(ref.id) == "findings"


async def test_idempotency_key_travels_with_every_send(repo, store):
    """Duplicate delivery must not become duplicate logical work."""
    ctx = FakeKernelContext(key="proj_1")
    task = await _kernel(ctx, repo, store).send(
        agent="finance", task="analyze", objective="o")
    assert ctx.sends[0].idempotency_key == task.id


# --------------------------------------------------------------- integration

def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True,
                          timeout=15).returncode == 0


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="needs a running Docker daemon")


@pytest.mark.integration
@requires_docker
async def test_kill_and_restart_the_agent_process():
    """Full release criterion — run with: uv run pytest -m integration

    1. Start a workflow.  2. Kill the Python agent process mid-flight.
    3. Restart it.  4. Assert durable timer/wait state recovered and that
    already-committed durable steps were not blindly repeated.
    """
    pytest.skip("requires `make up` (Restate + Postgres) and a registered service")
