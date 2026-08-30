"""The launch path: the one way work begins.

Three surfaces need it — the web button, a Buzz chat command, an external
trigger — so it must be one API, not three.
"""
import pytest

from app.kernel.models import TaskStatus
from app.launcher import Launcher, LaunchResult, UnknownTeam


@pytest.fixture
def launcher(tmp_path):
    return Launcher(teams=["teams/investment"], artifact_root=tmp_path)


async def test_launch_runs_a_project_and_returns_a_result(launcher):
    result = await launcher.launch("Evaluate Acme", auto_approve="approve")
    assert isinstance(result, LaunchResult)
    assert result.objective == "Evaluate Acme"
    assert result.team_id == "investment"
    assert result.runs, "a launch must produce a timeline"


async def test_launch_without_auto_approve_parks_on_a_human(launcher):
    """The web button and a chat command both need the workflow left waiting."""
    result = await launcher.launch("Evaluate Acme", auto_approve=None)
    assert len(result.waiting) == 1
    assert result.waiting[0].awakeable_id is not None


async def test_resolve_completes_a_parked_workflow(launcher):
    result = await launcher.launch("Evaluate Acme", auto_approve=None)
    await launcher.resolve(result.waiting[0].id, "approve")
    assert launcher.waiting() == []


async def test_reject_marks_the_task_rejected(launcher):
    result = await launcher.launch("Evaluate Acme", auto_approve=None)
    await launcher.resolve(result.waiting[0].id, "reject")
    repo = launcher.repository_for("investment")
    assert any(t.status is TaskStatus.REJECTED
               for t in repo.list_tasks(result.project_id))


async def test_two_launches_do_not_share_a_timeline(launcher):
    """Otherwise a second request would append to the first project's runs."""
    first = await launcher.launch("First", auto_approve="approve")
    second = await launcher.launch("Second", auto_approve="approve")
    assert first.project_id != second.project_id
    assert {r.id for r in first.runs}.isdisjoint({r.id for r in second.runs})


async def test_unknown_team_lists_what_exists(launcher):
    with pytest.raises(UnknownTeam, match="investment"):
        await launcher.launch("x", team="nosuchteam")


async def test_summary_reads_well_enough_to_post_in_chat(launcher):
    result = await launcher.launch("Evaluate Acme", auto_approve="approve")
    summary = result.summary()
    assert "Evaluate Acme" in summary
    assert any(agent in summary for agent in ("director", "research", "finance"))


async def test_artifacts_are_recorded(launcher):
    result = await launcher.launch("Evaluate Acme", auto_approve="approve")
    assert {a.type for a in result.artifacts} >= {"research", "valuation", "proposal"}


async def test_cross_team_launch_reaches_the_second_team(tmp_path):
    launcher = Launcher(teams=["teams/investment", "teams/research"],
                        artifact_root=tmp_path, cross_team=True)
    result = await launcher.launch("Evaluate Acme", task="evaluate_company_delegated",
                                   auto_approve="approve")
    # Each launch gets its own project id per team (investment-abc / research-abc),
    # so a second launch never appends to the first one's timeline.
    research_runner = launcher.runners["research"]
    tasks = research_runner.repo.list_tasks(research_runner.project_id)
    assert any(t.assigned_agent == "web-researcher" for t in tasks), \
        "second team never woken"
    assert research_runner.project_id != result.project_id
    assert research_runner.project_id.endswith(result.project_id.split("-")[-1]), \
        "both teams should share the launch suffix so a run is traceable across them"
    assert result.runs
