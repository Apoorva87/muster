"""Local timeline + human approval UI.

The web layer never talks to Restate. It calls an injected ``ApprovalResolver``,
which is what lets these tests run with no durable stack at all.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.repository import Repository
from app.kernel.models import RunRecord
from app.web.app import create_app

T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeResolver:
    """Stands in for the Restate awakeable resolver."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, awakeable_id: str, decision: str) -> None:
        self.calls.append((awakeable_id, decision))


@pytest.fixture
def repo():
    # In-memory, but shared-cache: TestClient serves the app on its own thread
    # and SQLAlchemy hands each thread a separate connection, so a plain
    # ``sqlite://`` database would look empty from inside a request. Naming the
    # memory database keeps it visible across those connections; a fresh name
    # per test keeps them isolated.
    r = Repository.from_url(
        f"sqlite:///file:{uuid4().hex}?mode=memory&cache=shared&uri=true"
    )
    r.init_schema()
    return r


@pytest.fixture
def resolver():
    return FakeResolver()


@pytest.fixture
def client(repo, resolver):
    return TestClient(create_app(repo, resolver))


def _run(repo, *, project_id="p1", agent="director", event_type="task.started",
         offset_s=0, **kwargs) -> RunRecord:
    run = RunRecord(project_id=project_id, agent=agent, event_type=event_type,
                    started_at=T0 + timedelta(seconds=offset_s), **kwargs)
    return repo.record_run(run)


def _order(body: str, *ids: str) -> list[int]:
    return [body.index(i) for i in ids]


# ------------------------------------------------------------------ timeline

def test_timeline_lists_runs_in_chronological_order(repo, client):
    third = _run(repo, agent="critic", offset_s=30)
    first = _run(repo, agent="research", offset_s=0)
    second = _run(repo, agent="finance", offset_s=10)

    body = client.get("/project/p1").text

    positions = _order(body, first.id, second.id, third.id)
    assert positions == sorted(positions)
    for agent in ("research", "finance", "critic"):
        assert agent in body


def test_timeline_shows_agent_event_type_status_and_duration(repo, client):
    run = _run(repo, agent="finance", event_type="analyze.complete",
               status="COMPLETE", duration_ms=1500)

    body = client.get("/project/p1").text

    assert "finance" in body
    assert "analyze.complete" in body
    assert "COMPLETE" in body
    assert "1.5s" in body
    assert f'/run/{run.id}' in body


def test_timeline_is_scoped_to_one_project(repo, client):
    mine = _run(repo, project_id="p1", agent="director")
    theirs = _run(repo, project_id="p2", agent="intruder")

    body = client.get("/project/p1").text

    assert mine.id in body
    assert theirs.id not in body
    assert "intruder" not in body


def test_index_shows_the_timeline_when_there_is_one_project(repo, client):
    run = _run(repo, project_id="only", agent="director")

    body = client.get("/").text

    assert run.id in body
    assert "director" in body


def test_index_lists_projects_when_there_are_several(repo, client):
    _run(repo, project_id="alpha")
    _run(repo, project_id="beta")

    body = client.get("/").text

    assert "/project/alpha" in body
    assert "/project/beta" in body


# -------------------------------------------------------------- run detail

def test_run_detail_shows_input_output_refs_and_error(repo, client):
    run = _run(repo, agent="finance", event_type="analyze",
               status="FAILED", duration_ms=420,
               input_refs={"brief": "art_brief_1"},
               output_refs={"report": "art_report_9"},
               error="ValueError: model refused")

    body = client.get(f"/run/{run.id}").text

    assert "brief" in body and "art_brief_1" in body
    assert "report" in body and "art_report_9" in body
    assert "ValueError: model refused" in body
    assert "420ms" in body


def test_run_detail_shows_trace_and_span_ids_when_set(repo, client):
    run = _run(repo, trace_id="trace_abc", span_id="span_def")

    body = client.get(f"/run/{run.id}").text

    assert "trace_abc" in body
    assert "span_def" in body


def test_run_detail_lists_artifacts_for_the_runs_task(repo, client):
    from app.kernel.models import Artifact, Task

    task = Task(project_id="p1", type="analyze", objective="o",
                assigned_agent="finance")
    repo.save_task(task)
    repo.save_artifact(Artifact(project_id="p1", task_id=task.id, type="report",
                                path="/artifacts/p1/report.md", created_by="finance"))
    other = Task(project_id="p1", type="x", objective="o", assigned_agent="critic")
    repo.save_task(other)
    repo.save_artifact(Artifact(project_id="p1", task_id=other.id, type="note",
                                path="/artifacts/p1/unrelated.md", created_by="critic"))
    run = _run(repo, agent="finance", task_id=task.id)

    body = client.get(f"/run/{run.id}").text

    assert "/artifacts/p1/report.md" in body
    assert "unrelated.md" not in body


def test_unknown_run_detail_returns_404(client):
    assert client.get("/run/run_nope").status_code == 404


# ---------------------------------------------------------------- approvals

def test_waiting_run_renders_approve_and_reject_controls(repo, client):
    run = _run(repo, agent="director", event_type="approval.requested")
    repo.set_awakeable(run.id, "awk_1")

    body = client.get("/project/p1").text

    assert f'action="/approve/{run.id}"' in body
    assert f'action="/reject/{run.id}"' in body
    assert "Approve" in body
    assert "Reject" in body


def test_finished_run_renders_no_approval_controls(repo, client):
    run = _run(repo, status="COMPLETE", duration_ms=10)

    body = client.get("/project/p1").text

    assert "/approve/" not in body
    assert "Approve" not in body
    assert run.id in body


def test_approve_resolves_the_awakeable_and_clears_it(repo, client, resolver):
    run = _run(repo, agent="director", event_type="approval.requested")
    repo.set_awakeable(run.id, "awk_approve_me")

    response = client.post(f"/approve/{run.id}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/project/p1"
    assert resolver.calls == [("awk_approve_me", "approve")]
    assert repo.get_run(run.id).awakeable_id is None


def test_reject_passes_the_reject_decision(repo, client, resolver):
    run = _run(repo, agent="director", event_type="approval.requested")
    repo.set_awakeable(run.id, "awk_reject_me")

    response = client.post(f"/reject/{run.id}", follow_redirects=False)

    assert response.status_code == 303
    assert resolver.calls == [("awk_reject_me", "reject")]
    assert repo.get_run(run.id).awakeable_id is None


def test_approve_without_an_awakeable_returns_409(repo, client, resolver):
    run = _run(repo, status="RUNNING")

    response = client.post(f"/approve/{run.id}")

    assert response.status_code == 409
    assert resolver.calls == []


def test_approve_on_an_unknown_run_returns_404(client, resolver):
    assert client.post("/approve/run_nope").status_code == 404
    assert resolver.calls == []


# ------------------------------------------------------------------ offline

def test_pages_reference_no_external_assets(repo, client):
    run = _run(repo)
    repo.set_awakeable(run.id, "awk_1")

    for path in ("/", "/project/p1", f"/run/{run.id}"):
        body = client.get(path).text
        assert "http://" not in body
        assert "https://" not in body
        assert "//cdn" not in body
