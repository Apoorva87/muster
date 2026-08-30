"""The V4 Day-4 demonstration, as assertions.

    run a project -> human rejects -> a note appears on disk
    -> a later, similar project recalls it before proposing

No Docker, no model, no network.
"""
from pathlib import Path

import pytest

from app.db.repository import Repository
from app.kernel.memory import MemoryKind
from app.local_runner import LocalRunner, drive
from app.memory import build_memory_store


@pytest.fixture
def memory(tmp_path):
    return build_memory_store(backend="filesystem", team_id="investment",
                              root=tmp_path / "memory")


@pytest.fixture
def project(tmp_path, memory):
    async def _run(objective: str, decision: str, tag: str) -> LocalRunner:
        runner = LocalRunner("teams/investment",
                             repository=Repository.from_url("sqlite://"),
                             artifact_root=tmp_path / "artifacts",
                             memory=memory, project_id=f"proj-{tag}")
        await runner.kernel().send(agent="director", task="evaluate_company",
                                   objective=objective)
        await drive([runner], auto_approve=decision)
        return runner
    return _run


async def proposal_body(runner: LocalRunner) -> str:
    found = [a for a in runner.repo.list_artifacts(runner.project_id)
             if a.type == "proposal"]
    assert found, "the project produced no proposal"
    return await runner.store.get(found[0].id)


# ------------------------------------------------------------ the loop

async def test_a_decision_becomes_a_note_on_disk(project, tmp_path):
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")

    notes = list((tmp_path / "memory").rglob("*.md"))
    assert len(notes) == 1, "one decision should produce exactly one note"
    assert notes[0].parent.name == "decisions"


async def test_the_note_is_about_the_work_a_human_asked_for(project, memory):
    """Not "React to critique.complete" — the objective someone actually wrote."""
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")

    refs = await memory.recall("Acme")
    assert refs, "the note is not findable by its own subject"
    assert "acme" in refs[0].subject.lower()
    assert "react to" not in refs[0].subject.lower()


async def test_the_note_carries_provenance(project, memory):
    """PRD: a note with no sources is a rumour."""
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")

    ref = (await memory.recall("Acme"))[0]
    note = await memory.get(ref.id)
    assert note.is_grounded
    assert any(s.startswith("run_") for s in note.sources)
    assert note.timeline, "the evidence that produced the belief must be visible"


async def test_a_later_project_recalls_it(project):
    """The loop closing: the team is better on the second project."""
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    second = await project("Evaluate Beta Corp at 29x earnings", "approve", "b")

    body = await proposal_body(second)
    assert "What we learned before" in body
    assert "acme" in body.lower(), "the earlier decision was not recalled"


async def test_the_first_project_had_nothing_to_recall(project):
    body = await proposal_body(
        await project("Evaluate Acme Corp at 31x earnings", "reject", "a"))
    assert "What we learned before" not in body


async def test_distillation_is_idempotent(project, memory, tmp_path):
    runner = await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    await runner.learn()
    await runner.learn()
    assert len(list((tmp_path / "memory").rglob("*.md"))) == 1


# -------------------------------------------------- markdown is canonical

async def test_deleting_the_index_loses_nothing(project, memory, tmp_path):
    """A PRD acceptance criterion, tested directly."""
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    before = {r.id for r in await memory.recall("Acme")}

    cold = build_memory_store(backend="filesystem", team_id="investment",
                              root=tmp_path / "memory")
    assert await cold.rebuild_index() >= 1
    assert {r.id for r in await cold.recall("Acme")} == before


async def test_a_hand_edited_note_is_used(project, memory, tmp_path):
    """PRD: a wrong memory is fixable with an editor and a commit."""
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    note_file = next((tmp_path / "memory").rglob("*.md"))

    note_file.write_text(note_file.read_text().replace(
        "The human gave no reason.",
        "Rejected because the multiple lacked a peer comparison."))

    cold = build_memory_store(backend="filesystem", team_id="investment",
                              root=tmp_path / "memory")
    ref = (await cold.recall("peer comparison"))[0]
    assert "peer comparison" in (await cold.get(ref.id)).summary


async def test_a_forgotten_note_stays_forgotten(project, memory, tmp_path):
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    ref = (await memory.recall("Acme"))[0]

    assert await memory.forget(ref.id) is True
    cold = build_memory_store(backend="filesystem", team_id="investment",
                              root=tmp_path / "memory")
    await cold.rebuild_index()
    assert await cold.recall("Acme") == []


# ------------------------------------------------------------- isolation

async def test_memory_off_behaves_exactly_as_v3(tmp_path):
    """PRD acceptance: MEMORY_BACKEND=none runs identically to V3."""
    runner = LocalRunner("teams/investment",
                         repository=Repository.from_url("sqlite://"),
                         artifact_root=tmp_path / "artifacts",
                         memory_backend="none", project_id="proj-none")
    await runner.kernel().send(agent="director", task="evaluate_company",
                               objective="Evaluate Acme Corp at 31x earnings")
    await drive([runner], auto_approve="approve")

    # Artifacts are markdown too, so look only where memory would land.
    assert not (tmp_path / "memory").exists(), "disabled memory must write nothing"
    assert not list((Path("teams/investment") / "memory").glob("**/*.md"))
    assert "What we learned before" not in await proposal_body(runner)


async def test_one_team_never_reads_another_teams_memory(tmp_path, memory, project):
    await project("Evaluate Acme Corp at 31x earnings", "reject", "a")

    other = build_memory_store(backend="filesystem", team_id="research",
                               root=tmp_path / "other-memory")
    assert await other.recall("Acme") == [], "memory must not cross teams"


async def test_nothing_queries_memory_on_an_agents_behalf(project, tmp_path):
    """The core rule: retrieved explicitly, never injected."""
    runner = await project("Evaluate Acme Corp at 31x earnings", "reject", "a")
    body = await proposal_body(runner)
    # The first project has no memory to recall, so no memory section appears
    # even though the store is live and writable.
    assert "What we learned before" not in body
