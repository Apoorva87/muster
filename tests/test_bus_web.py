"""The V2 bus control page: session view, team detail, routing table.

The page is a pure read model over a ``TeamRegistry`` and V1's ``Repository``,
so these tests need neither Restate nor Docker — just a registry, an in-memory
database and a ``TestClient``.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.repository import Repository
from app.kernel.models import RunRecord
from bus.models.team import AgentDescriptor, Health, TeamDescriptor
from bus.routing.registry import TeamRegistry
from bus.web.app import create_app

T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)

INVESTMENT = "investment-team"
RESEARCH = "research-team"
SECURITY = "security-team"


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def repo():
    # ``Repository.from_url`` pins a StaticPool for SQLite, so the single
    # in-memory database is visible from the thread TestClient serves on.
    r = Repository.from_url("sqlite://")
    r.init_schema()
    return r


@pytest.fixture
def registry():
    """The PRD's example session, wired so one topic crosses team boundaries."""
    reg = TeamRegistry(session_id="workstation-01")
    reg.register(TeamDescriptor(
        team_id=INVESTMENT,
        description="Investment committee",
        agents=[
            AgentDescriptor(name="director", capabilities=["orchestrate"]),
            AgentDescriptor(name="finance", capabilities=["valuation", "modelling"]),
        ],
        subscriptions=[("market.changed", "finance"), ("research.complete", "director")],
        public_topics=["proposal.ready"],
        public_commands=["evaluate_company"],
    ))
    reg.register(TeamDescriptor(
        team_id=RESEARCH,
        description="Desk research",
        agents=[AgentDescriptor(name="web-researcher", capabilities=["web_search"])],
        # Second subscriber to market.changed — this is the cross-team wiring.
        subscriptions=[("market.changed", "web-researcher")],
        public_topics=["research.complete", "market.changed"],
        public_commands=["research_company"],
    ))
    reg.register(TeamDescriptor(
        team_id=SECURITY,
        agents=[AgentDescriptor(name="triage", capabilities=["assess_risk"])],
        subscriptions=[("proposal.ready", "triage")],
    ))
    return reg


@pytest.fixture
def client(registry, repo):
    return TestClient(create_app(registry, repo))


def _run(repo, project_id, *, agent="director", status="RUNNING",
         event_type="task.started", offset_s=0) -> RunRecord:
    return repo.record_run(RunRecord(
        project_id=project_id, agent=agent, event_type=event_type,
        status=status, started_at=T0 + timedelta(seconds=offset_s),
    ))


def _waiting_run(repo, project_id, **kwargs) -> RunRecord:
    run = _run(repo, project_id, **kwargs)
    repo.set_awakeable(run.id, f"awk_{run.id}")
    return run


@pytest.fixture
def seeded(repo):
    """The PRD's example numbers: 2 running + 1 waiting on the investment team."""
    _run(repo, INVESTMENT, agent="director", offset_s=0)
    _run(repo, INVESTMENT, agent="finance", offset_s=5)
    _waiting_run(repo, INVESTMENT, agent="critic", offset_s=10)
    _run(repo, INVESTMENT, agent="research", status="COMPLETE", offset_s=15)
    _run(repo, SECURITY, agent="triage", offset_s=20)
    return repo


# -------------------------------------------------------------- session view


def test_session_view_lists_every_registered_team(client, registry):
    body = client.get("/").text

    assert "workstation-01" in body
    for team_id in registry.team_ids():
        assert team_id in body
        assert f'href="/team/{team_id}"' in body


def test_session_view_counts_running_and_waiting_runs(client, seeded):
    body = client.get("/").text

    # investment-team: two RUNNING, one parked on a human decision.
    assert "2 running" in body
    assert "1 waiting" in body
    # research-team has no runs at all; security-team has one.
    assert "0 running" in body
    assert "1 running" in body


def test_session_view_counts_only_the_teams_own_runs(client, seeded):
    """Counting is keyed by project_id == team_id, so teams never bleed."""
    row = _row_for(client.get("/").text, RESEARCH)
    assert "0 running" in row
    assert "waiting" not in row


def test_session_view_renders_health_for_each_team(client, registry):
    registry.set_health(RESEARCH, Health.DEGRADED)
    registry.set_health(SECURITY, Health.UNREACHABLE)

    body = client.get("/").text

    assert "healthy" in _row_for(body, INVESTMENT)
    assert "degraded" in _row_for(body, RESEARCH)
    assert "unreachable" in _row_for(body, SECURITY)
    # Health is a visible indicator, not just text: it uses the pill vocabulary.
    assert 'class="pill h-degraded"' in body


def test_session_view_shows_agent_counts(client):
    assert "2 agents" in _row_for(client.get("/").text, INVESTMENT)
    assert "1 agent" in _row_for(client.get("/").text, RESEARCH)


def test_empty_registry_renders_without_crashing(repo):
    client = TestClient(create_app(TeamRegistry(session_id="empty-01"), repo))

    response = client.get("/")

    assert response.status_code == 200
    assert "empty-01" in response.text
    assert "No teams" in response.text


# -------------------------------------------------------------- team detail


def test_team_detail_shows_agents_and_capabilities(client):
    body = client.get(f"/team/{INVESTMENT}").text

    assert "director" in body
    assert "finance" in body
    assert "valuation" in body
    assert "modelling" in body


def test_team_detail_shows_subscriptions_topics_and_commands(client):
    body = client.get(f"/team/{INVESTMENT}").text

    assert "market.changed" in body        # subscription topic
    assert "proposal.ready" in body        # public topic
    assert "evaluate_company" in body      # public command


def test_team_detail_shows_health_and_links_to_the_v1_timeline(client, seeded, registry):
    registry.set_health(INVESTMENT, Health.DEGRADED)

    body = client.get(f"/team/{INVESTMENT}").text

    assert 'class="pill h-degraded"' in body
    assert "degraded" in body
    # project_id == team_id, so the timeline link is the V1 project route.
    assert f'href="/project/{INVESTMENT}"' in body
    assert "4 runs" in body


def test_team_detail_timeline_link_can_point_at_another_origin(registry, repo):
    client = TestClient(create_app(registry, repo, timeline_base="http://localhost:8000"))

    body = client.get(f"/team/{INVESTMENT}").text

    assert f'href="http://localhost:8000/project/{INVESTMENT}"' in body


def test_unknown_team_returns_404(client):
    response = client.get("/team/no-such-team")

    assert response.status_code == 404
    assert "no-such-team" in response.json()["detail"]


def test_team_with_no_agents_or_wiring_renders(repo):
    registry = TeamRegistry()
    registry.register(TeamDescriptor(team_id="bare-team"))
    client = TestClient(create_app(registry, repo))

    response = client.get("/team/bare-team")

    assert response.status_code == 200
    assert "registered no agents" in response.text


# ------------------------------------------------------------- routing table


def test_topics_page_shows_subscribers_from_more_than_one_team(client):
    body = client.get("/topics").text

    row = _row_for(body, "market.changed")
    assert f"team://{INVESTMENT}/finance" in row
    assert f"team://{RESEARCH}/web-researcher" in row
    assert "cross-team" in row


def test_topics_page_lists_every_wired_topic_with_its_publisher(client):
    body = client.get("/topics").text

    assert "research.complete" in body
    assert f"published by {RESEARCH}" in _row_for(body, "market.changed")
    # proposal.ready: published by investment, consumed by security.
    assert f"team://{SECURITY}/triage" in _row_for(body, "proposal.ready")


def test_topics_page_flags_a_topic_nobody_subscribes_to(repo):
    registry = TeamRegistry()
    registry.register(TeamDescriptor(team_id="lonely", public_topics=["shouted.into.void"]))
    client = TestClient(create_app(registry, repo))

    body = client.get("/topics").text

    assert "shouted.into.void" in body
    assert "no subscribers" in body


def test_topics_page_renders_with_an_empty_registry(repo):
    client = TestClient(create_app(TeamRegistry(), repo))

    response = client.get("/topics")

    assert response.status_code == 200
    assert "No topics" in response.text


# ------------------------------------------------------------------- helpers


def _row_for(body: str, needle: str) -> str:
    """The single ``<li>`` of the rendered list that mentions ``needle``.

    Assertions about one team's counts must not accidentally match another
    team's row, so they are scoped to the row itself.
    """
    rows = [chunk for chunk in body.split("<li") if needle in chunk]
    assert rows, f"no row mentioning {needle!r}"
    assert len(rows) == 1, f"{needle!r} appears in {len(rows)} rows"
    return rows[0]
