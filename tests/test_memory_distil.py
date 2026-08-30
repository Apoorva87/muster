"""Distillation: one approve/reject decision becomes exactly one memory note.

The V4 PRD calls the team's approve/reject stream the one labelled dataset it
already produces and throws away. These tests pin the properties that make
keeping it safe rather than merely possible:

* exactly one note per decision, and re-running distillation changes nothing;
* provenance on every note — a note with no sources is refused, not written;
* a scratchpad is never read, never quoted and never cited;
* no model is required: the deterministic path is the one that always works;
* ``MEMORY_BACKEND=none`` (``NullMemoryStore``) is a silent no-op.

No model endpoint and no Docker: SQLite in memory, a temp-dir artifact store,
the real filesystem memory backend, and ``StubLLMRunner`` where an LLM is
wanted.
"""

from __future__ import annotations

import pytest

from app.agents.base import StubLLMRunner
from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.memory import Confidence, MemoryKind, NullMemoryStore
from app.kernel.models import Artifact, RunRecord, Task, TaskStatus
from app.memory.distil import (Decision, confidence_for, decision_subject,
                               distil, distil_project, find_decisions)
from app.memory.filesystem import FilesystemMemoryStore

PROJECT = "proj_alpha"

PROPOSAL_BODY = "# Proposal — Acme Corp\nAcquire at 31x trailing earnings.\n"
CRITIQUE_BODY = "# Critique\nThe 31x multiple is cited with no peer comparables.\n"
HUMAN_REASON = "31x with no peer-group comparison; add a peer table first."

# If this string ever reaches a note, rule 5 is broken.
SCRATCHPAD_BODY = (
    "DIRECTOR_PRIVATE_REASONING: I want this approved and will bury the "
    "multiple in an appendix."
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def repo() -> Repository:
    repository = Repository.from_url("sqlite://")
    repository.init_schema()
    return repository


@pytest.fixture
def store(tmp_path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture
def memory(tmp_path) -> FilesystemMemoryStore:
    return FilesystemMemoryStore(root=tmp_path / "memory", team_id="investment")


class RecordingStore:
    """Wraps an artifact store and records every ID whose body was read.

    Proves the strong form of rule 5: a scratchpad is not merely absent from
    the note, it is never read at all.
    """

    def __init__(self, inner: FilesystemArtifactStore) -> None:
        self._inner = inner
        self.reads: list[str] = []

    async def put(self, **kwargs):
        return await self._inner.put(**kwargs)

    async def get(self, artifact_id: str) -> str:
        self.reads.append(artifact_id)
        return await self._inner.get(artifact_id)


# ------------------------------------------------------------------ builders


async def make_artifact(repo: Repository, store: FilesystemArtifactStore, *,
                        task: Task, type: str, content: str,
                        created_by: str = "director") -> Artifact:
    ref = await store.put(project_id=task.project_id, task_id=task.id,
                          created_by=created_by, content=content, type=type)
    return repo.save_artifact(Artifact(
        id=ref.id, project_id=task.project_id, task_id=task.id, type=type,
        path=str(store.path_for(ref.id) or ""), created_by=created_by))


def make_task(repo: Repository, objective: str, *,
              project_id: str = PROJECT) -> Task:
    return repo.save_task(Task(project_id=project_id, type="on:critique.complete",
                               objective=objective, assigned_agent="director"))


def record_decision(repo: Repository, *, task: Task, verdict: str,
                    note: str | None = None, prompt: str = "Approve?",
                    event_type: str = "approval.requested") -> RunRecord:
    """The run ``Kernel.request_approval`` leaves behind once a human answers."""
    run = repo.record_run(RunRecord(
        project_id=task.project_id, task_id=task.id, agent="director",
        event_type=event_type, status="WAITING_FOR_HUMAN",
        input_refs={"prompt": prompt}))
    repo.finish_run(run.id, status="COMPLETE",
                    output_refs={"decision": verdict, "note": note})
    return repo.get_run(run.id)


async def one_note(memory: FilesystemMemoryStore, ref):
    return await memory.get(ref.id)


def note_files(tmp_path) -> list:
    return sorted((tmp_path / "memory" / "decisions").glob("*.md"))


# ------------------------------------------------------------ find_decisions


async def test_find_decisions_finds_an_approval_a_rejection_and_ignores_the_rest(
        repo, store):
    task = make_task(repo, "Evaluate Acme Corp")
    proposal = await make_artifact(repo, store, task=task, type="proposal",
                                   content=PROPOSAL_BODY)
    critique = await make_artifact(repo, store, task=task, type="critique",
                                   content=CRITIQUE_BODY, created_by="critic")

    # Noise: an ordinary send, a publish, and an approval still parked.
    repo.record_run(RunRecord(project_id=PROJECT, task_id=task.id, agent="director",
                              event_type="task.sent", status="SENT"))
    repo.record_run(RunRecord(project_id=PROJECT, task_id=task.id, agent="director",
                              event_type="event.published", status="COMPLETE",
                              output_refs={"topic": "proposal.ready"}))
    repo.record_run(RunRecord(project_id=PROJECT, task_id=task.id, agent="director",
                              event_type="approval.requested",
                              status="WAITING_FOR_HUMAN",
                              input_refs={"prompt": "Approve?"}))
    # Another project's decision must not leak in.
    other = make_task(repo, "Evaluate Beta Ltd", project_id="proj_beta")
    record_decision(repo, task=other, verdict="approve")

    approved = record_decision(repo, task=task, verdict="approve")
    rejected = record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)

    decisions = find_decisions(repo, PROJECT)

    assert [d.run_id for d in decisions] == [approved.id, rejected.id]
    assert [d.verdict for d in decisions] == ["approve", "reject"]
    assert decisions[0].note is None
    assert decisions[1].note == HUMAN_REASON
    assert all(d.objective == "Evaluate Acme Corp" for d in decisions)
    assert all(d.project_id == PROJECT for d in decisions)
    assert all(d.proposal_id == proposal.id for d in decisions)
    assert all(d.critique_id == critique.id for d in decisions)


async def test_find_decisions_accepts_the_prds_event_name_and_past_tense_verdicts(
        repo):
    task = make_task(repo, "Evaluate Acme Corp")
    record_decision(repo, task=task, verdict="Rejected",
                    event_type="decision.completed")

    decisions = find_decisions(repo, PROJECT)

    assert [d.verdict for d in decisions] == ["reject"]


async def test_find_decisions_ignores_an_unrecognised_verdict(repo):
    task = make_task(repo, "Evaluate Acme Corp")
    record_decision(repo, task=task, verdict="maybe later")

    assert find_decisions(repo, PROJECT) == []


async def test_find_decisions_never_selects_a_private_artifact(repo, store):
    """Even when the approval prompt names it by ID."""
    task = make_task(repo, "Evaluate Acme Corp")
    scratchpad = await make_artifact(repo, store, task=task, type="scratchpad",
                                     content=SCRATCHPAD_BODY)
    record_decision(repo, task=task, verdict="approve",
                    prompt=f"Approve {scratchpad.id}?")

    decision = find_decisions(repo, PROJECT)[0]

    assert decision.proposal_id is None
    assert decision.critique_id is None


async def test_find_decisions_prefers_the_artifact_the_prompt_named(repo, store):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal",
                        content=PROPOSAL_BODY)
    synthesis = await make_artifact(repo, store, task=task, type="synthesis",
                                    content="# Synthesis\nGo ahead.\n")
    record_decision(repo, task=task, verdict="approve",
                    prompt=f"Approve {synthesis.id}?")

    decision = find_decisions(repo, PROJECT)[0]

    assert decision.proposal_id == synthesis.id


# -------------------------------------------------------------------- distil


async def test_distil_writes_one_grounded_note(repo, store, memory, tmp_path):
    task = make_task(repo, "Evaluate Acme Corp")
    proposal = await make_artifact(repo, store, task=task, type="proposal",
                                   content=PROPOSAL_BODY)
    critique = await make_artifact(repo, store, task=task, type="critique",
                                   content=CRITIQUE_BODY, created_by="critic")
    run = record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)

    assert ref is not None
    assert ref.kind is MemoryKind.DECISION
    assert ref.subject == decision_subject(decision)

    note = await one_note(memory, ref)
    assert note.sources == [run.id, proposal.id, critique.id]
    assert note.is_grounded
    assert len(note.timeline) == 1
    assert len(note_files(tmp_path)) == 1


async def test_the_humans_stated_reason_appears_in_the_summary(repo, store, memory):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)
    note = await one_note(memory, ref)

    assert HUMAN_REASON in note.summary
    assert "Rejected: Evaluate Acme Corp" in note.summary
    assert HUMAN_REASON in note.timeline[0].note


async def test_distilling_twice_is_idempotent(repo, store, memory, tmp_path):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]

    first = await distil(decision, repository=repo, artifacts=store, memory=memory)
    second = await distil(decision, repository=repo, artifacts=store, memory=memory)

    assert second is not None and second.id == first.id
    assert len(note_files(tmp_path)) == 1

    note = await one_note(memory, second)
    assert len(note.timeline) == 1                # no duplicated evidence
    assert note.sources == (await one_note(memory, first)).sources


async def test_a_shared_subject_still_gets_one_timeline_entry_per_decision(
        repo, store, memory, tmp_path):
    """Two decisions may be consolidated onto one topical subject — but each
    contributes exactly once, however often distillation is replayed."""
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    record_decision(repo, task=task, verdict="approve", note="peer table added")
    first, second = find_decisions(repo, PROJECT)

    for decision in (first, second, first, second):
        await distil(decision, repository=repo, artifacts=store, memory=memory,
                     subject="valuation-multiples")

    assert len(note_files(tmp_path)) == 1
    note = await memory.get((await memory.recall("valuation-multiples"))[0].id)
    assert len(note.timeline) == 2
    assert first.run_id in note.sources and second.run_id in note.sources


async def test_a_decision_with_no_artifacts_is_still_grounded_in_its_run(
        repo, memory, store):
    """Documented choice: the run id alone *is* provenance, so the note is
    written rather than refused — a verdict a human really gave is not a
    rumour. It just says so, and its confidence is capped."""
    task = make_task(repo, "Evaluate Acme Corp")
    run = record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]
    assert decision.proposal_id is None and decision.critique_id is None

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)
    note = await one_note(memory, ref)

    assert note.sources == [run.id]
    assert f"grounded in run {run.id}" in note.summary
    assert note.confidence is Confidence.MEDIUM


async def test_a_decision_with_no_provenance_at_all_is_refused(
        repo, store, memory, tmp_path):
    """A note with no sources is a rumour (PRD rule 3) — so it is not written."""
    rumour = Decision(run_id="", project_id=PROJECT, verdict="reject",
                      note=HUMAN_REASON, proposal_id=None, critique_id=None,
                      objective="Evaluate Acme Corp")

    assert await distil(rumour, repository=repo, artifacts=store,
                        memory=memory) is None
    assert note_files(tmp_path) == []
    assert await memory.recall("Evaluate Acme Corp") == []


async def test_an_unregistered_artifact_is_not_provenance(repo, store, memory):
    """Deny by default, as the context builder does: an artifact this project
    never registered cannot be cited."""
    task = make_task(repo, "Evaluate Acme Corp")
    run = record_decision(repo, task=task, verdict="approve")
    decision = Decision(run_id=run.id, project_id=PROJECT, verdict="approve",
                        note=None, proposal_id="art_never_registered",
                        critique_id=None, objective="Evaluate Acme Corp")

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)
    note = await one_note(memory, ref)

    assert note.sources == [run.id]


async def test_a_scratchpad_is_never_read_quoted_or_cited(repo, store, memory):
    """Rule 5: a critic's independence does not survive the strategist's
    scratchpad, whether it arrives fresh or from memory."""
    task = make_task(repo, "Evaluate Acme Corp")
    proposal = await make_artifact(repo, store, task=task, type="proposal",
                                   content=PROPOSAL_BODY)
    scratchpad = await make_artifact(repo, store, task=task, type="scratchpad",
                                     content=SCRATCHPAD_BODY)
    run = record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    recording = RecordingStore(store)

    # The caller hands distillation the scratchpad *as if* it were the critique.
    decision = Decision(run_id=run.id, project_id=PROJECT, verdict="reject",
                        note=HUMAN_REASON, proposal_id=proposal.id,
                        critique_id=scratchpad.id, objective="Evaluate Acme Corp")
    ref = await distil(decision, repository=repo, artifacts=recording,
                       memory=memory)
    note = await one_note(memory, ref)

    assert scratchpad.id not in recording.reads          # never read
    assert recording.reads == [proposal.id]
    assert scratchpad.id not in note.sources             # never cited
    assert "DIRECTOR_PRIVATE_REASONING" not in note.to_markdown()
    assert SCRATCHPAD_BODY not in note.to_markdown()


async def test_the_summary_never_inlines_an_artifact_body(repo, store, memory):
    task = make_task(repo, "Evaluate Acme Corp")
    body = "# Proposal\n" + ("Acme has a very long history. " * 300)
    await make_artifact(repo, store, task=task, type="proposal", content=body)
    record_decision(repo, task=task, verdict="approve")
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)
    note = await one_note(memory, ref)

    assert body not in note.summary
    assert len(note.summary) <= 900
    assert len(note.summary.splitlines()) <= 5


# ---------------------------------------------------------------- confidence


@pytest.mark.parametrize("verdict,reason,expected", [
    ("reject", HUMAN_REASON, Confidence.HIGH),
    ("reject", None, Confidence.MEDIUM),
    ("approve", "matches the mandate", Confidence.MEDIUM),
    ("approve", None, Confidence.LOW),
])
async def test_confidence_follows_the_documented_mapping(
        repo, store, memory, verdict, reason, expected):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict=verdict, note=reason)
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)

    assert ref.confidence is expected
    assert (await one_note(memory, ref)).confidence is expected


def test_confidence_is_capped_without_artifact_evidence():
    assert confidence_for("reject", HUMAN_REASON, has_artifacts=True) is Confidence.HIGH
    assert confidence_for("reject", HUMAN_REASON, has_artifacts=False) is Confidence.MEDIUM
    # The cap only bites at HIGH; a bare approval stays low either way.
    assert confidence_for("approve", None, has_artifacts=False) is Confidence.LOW


# ------------------------------------------------------------------ the LLM


async def test_the_deterministic_path_is_used_when_no_llm_is_supplied(
        repo, store, memory):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory)
    note = await one_note(memory, ref)

    assert note.summary.startswith("Rejected: Evaluate Acme Corp")
    assert "[distil]" not in note.summary


async def test_an_llm_summary_is_used_when_one_is_supplied(repo, store, memory):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory,
                       llm=StubLLMRunner())
    note = await one_note(memory, ref)

    assert note.summary.startswith("[distil]")
    # The facts still ground the prose, so the human's words cannot be lost.
    assert HUMAN_REASON in note.summary


async def test_a_broken_llm_falls_back_to_the_deterministic_summary(
        repo, store, memory):
    class BrokenLLM:
        async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
            raise RuntimeError("no model endpoint")

    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="approve")
    decision = find_decisions(repo, PROJECT)[0]

    ref = await distil(decision, repository=repo, artifacts=store, memory=memory,
                       llm=BrokenLLM())
    note = await one_note(memory, ref)

    assert note.summary.startswith("Approved: Evaluate Acme Corp")


# ------------------------------------------------------------ disabled + bulk


async def test_null_memory_store_makes_distillation_a_no_op(repo, store):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    decision = find_decisions(repo, PROJECT)[0]
    disabled = NullMemoryStore("investment")

    assert await distil(decision, repository=repo, artifacts=store,
                        memory=disabled) is None
    assert await distil_project(PROJECT, repository=repo, artifacts=store,
                                memory=disabled) == []


async def test_distil_project_handles_several_decisions(repo, store, memory,
                                                        tmp_path):
    task = make_task(repo, "Evaluate Acme Corp")
    await make_artifact(repo, store, task=task, type="proposal", content=PROPOSAL_BODY)
    await make_artifact(repo, store, task=task, type="critique",
                        content=CRITIQUE_BODY, created_by="critic")
    record_decision(repo, task=task, verdict="reject", note=HUMAN_REASON)
    record_decision(repo, task=task, verdict="approve", note="peer table added")
    record_decision(repo, task=task, verdict="approve")

    refs = await distil_project(PROJECT, repository=repo, artifacts=store,
                                memory=memory)

    assert len(refs) == 3
    assert len({ref.id for ref in refs}) == 3
    assert len(note_files(tmp_path)) == 3
    assert [ref.confidence for ref in refs] == [Confidence.HIGH, Confidence.MEDIUM,
                                                Confidence.LOW]

    # Running the whole project again adds nothing.
    again = await distil_project(PROJECT, repository=repo, artifacts=store,
                                 memory=memory)
    assert {ref.id for ref in again} == {ref.id for ref in refs}
    assert len(note_files(tmp_path)) == 3
    for ref in again:
        assert len((await memory.get(ref.id)).timeline) == 1


async def test_distil_project_is_empty_for_a_project_with_no_decisions(
        repo, store, memory):
    task = make_task(repo, "Evaluate Acme Corp")
    repo.set_task_status(task.id, TaskStatus.COMPLETE)

    assert await distil_project(PROJECT, repository=repo, artifacts=store,
                                memory=memory) == []


async def test_a_non_verdict_is_a_loud_error_not_a_silent_note(repo, store, memory):
    nonsense = Decision(run_id="run_x", project_id=PROJECT, verdict="ship it",
                        note=None, proposal_id=None, critique_id=None,
                        objective="Evaluate Acme Corp")

    with pytest.raises(ValueError, match="not a verdict"):
        await distil(nonsense, repository=repo, artifacts=store, memory=memory)
