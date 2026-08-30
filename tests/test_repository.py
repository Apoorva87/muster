"""Semantic state. Postgres is canonical; SQLite backs these tests."""
import pytest

from app.db.repository import Repository
from app.kernel.models import Artifact, Event, RunRecord, Task, TaskStatus


@pytest.fixture
def repo():
    r = Repository.from_url("sqlite://")
    r.init_schema()
    return r


def test_task_round_trips(repo):
    t = Task(project_id="p1", type="analyze", objective="o", assigned_agent="finance")
    repo.save_task(t)
    loaded = repo.get_task(t.id)
    assert loaded == t


def test_get_unknown_task_returns_none(repo):
    assert repo.get_task("task_nope") is None


def test_status_update_persists(repo):
    t = Task(project_id="p1", type="a", objective="o", assigned_agent="critic")
    repo.save_task(t)
    repo.set_task_status(t.id, TaskStatus.WAITING_FOR_HUMAN)
    assert repo.get_task(t.id).status is TaskStatus.WAITING_FOR_HUMAN


def test_awakeable_id_persists_from_first_migration(repo):
    """Decision D3: the Approve button cannot resume without this."""
    t = Task(project_id="p1", type="a", objective="o", assigned_agent="director")
    repo.save_task(t)
    run = RunRecord(project_id="p1", task_id=t.id, agent="director",
                    event_type="approval.requested")
    repo.record_run(run)
    repo.set_awakeable(run.id, "awk_123")

    reloaded = repo.get_run(run.id)
    assert reloaded.awakeable_id == "awk_123"
    assert reloaded.awaiting_since is not None


def test_waiting_runs_are_discoverable_after_restart(repo):
    """A fresh Repository over the same engine still finds the parked work."""
    t = Task(project_id="p1", type="a", objective="o", assigned_agent="director")
    repo.save_task(t)
    run = RunRecord(project_id="p1", task_id=t.id, agent="director",
                    event_type="approval.requested")
    repo.record_run(run)
    repo.set_awakeable(run.id, "awk_abc")
    repo.set_task_status(t.id, TaskStatus.WAITING_FOR_HUMAN)

    revived = Repository(engine=repo.engine)
    waiting = revived.list_waiting_runs("p1")
    assert [w.awakeable_id for w in waiting] == ["awk_abc"]


def test_timeline_is_chronological(repo):
    for i in range(3):
        repo.record_run(RunRecord(project_id="p1", agent=f"a{i}", event_type="task.started"))
    runs = repo.list_runs("p1")
    assert [r.agent for r in runs] == ["a0", "a1", "a2"]
    assert runs == sorted(runs, key=lambda r: r.started_at)


def test_timeline_is_scoped_by_project(repo):
    repo.record_run(RunRecord(project_id="p1", agent="a", event_type="e"))
    repo.record_run(RunRecord(project_id="p2", agent="b", event_type="e"))
    assert [r.agent for r in repo.list_runs("p1")] == ["a"]


def test_finish_run_records_duration_and_status(repo):
    run = RunRecord(project_id="p1", agent="finance", event_type="task.started")
    repo.record_run(run)
    repo.finish_run(run.id, status="COMPLETE", output_refs={"artifact_id": "art_1"})
    done = repo.get_run(run.id)
    assert done.status == "COMPLETE"
    assert done.finished_at is not None
    assert done.duration_ms is not None and done.duration_ms >= 0
    assert done.output_refs == {"artifact_id": "art_1"}


def test_events_and_artifacts_persist(repo):
    repo.save_event(Event(topic="proposal.ready", project_id="p1",
                          payload={"artifact_id": "art_1"}))
    repo.save_artifact(Artifact(project_id="p1", task_id="t1", type="markdown",
                                path="/x.md", created_by="research"))
    assert repo.list_events("p1")[0].topic == "proposal.ready"
    assert repo.list_artifacts("p1")[0].created_by == "research"


def test_subscriptions_resolve_to_multiple_agents(repo):
    repo.seed_default_subscriptions()
    assert set(repo.subscribers_for("proposal.ready")) == {"critic", "finance"}
    assert set(repo.subscribers_for("market.changed")) == {"finance", "director"}
    assert repo.subscribers_for("nobody.listens") == []


def test_seeding_is_idempotent(repo):
    repo.seed_default_subscriptions()
    repo.seed_default_subscriptions()
    assert sorted(repo.subscribers_for("proposal.ready")) == ["critic", "finance"]
