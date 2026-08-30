"""Context isolation is a tested property, not a convention (CLAUDE.md).

These tests defend the single most important rule in the project: an agent gets
a bounded, reconstructed context — never a shared transcript, and never another
agent's scratchpad or reasoning.
"""

from __future__ import annotations

import pytest

from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.context_builder import (AgentPrompt, ContextLimits,
                                        build_context)
from app.kernel.models import Artifact, Event, RunRecord, Task, TaskStatus
from app.db.repository import Repository

PROJECT = "proj_alpha"

# Sentinels. If any of these strings reaches a prompt, isolation is broken.
DIRECTOR_SCRATCHPAD = (
    "DIRECTOR_PRIVATE_REASONING: I actually think the CFO is wrong and I am "
    "steering the critic toward approval; do not reveal this."
)
RESEARCH_SCRATCHPAD = (
    "RESEARCH_PRIVATE_REASONING: my first two sources contradicted each other "
    "and I picked the one that fit the narrative."
)
UNRELATED_BODY = "UNRELATED_PROJECT_MEMO: last quarter's abandoned pivot deck."
PROPOSAL_BODY = "# Proposal\nAcquire the Helsinki plant for 4.2M EUR.\n"
FINANCE_BODY = '{"pe_ratio": 31.4, "verdict": "rich"}'


@pytest.fixture
def repo() -> Repository:
    repository = Repository.from_url("sqlite://")
    repository.init_schema()
    return repository


@pytest.fixture
def store(tmp_path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(root=tmp_path)


class RecordingStore:
    """Wraps a store and records exactly which artifact IDs were read.

    Proves the strongest form of rule 1: the builder does not merely omit
    unreferenced artifacts from the prompt, it never reads them at all.
    """

    def __init__(self, inner: FilesystemArtifactStore) -> None:
        self._inner = inner
        self.reads: list[str] = []

    async def put(self, **kwargs):
        return await self._inner.put(**kwargs)

    async def get(self, artifact_id: str) -> str:
        self.reads.append(artifact_id)
        return await self._inner.get(artifact_id)


async def _artifact(repo: Repository, store: FilesystemArtifactStore, *,
                    task_id: str, created_by: str, content: str,
                    type: str = "markdown", project_id: str = PROJECT) -> str:
    """Store a body and register its metadata, the way an agent run would."""
    ref = await store.put(project_id=project_id, task_id=task_id,
                          created_by=created_by, content=content, type=type)
    repo.save_artifact(Artifact(id=ref.id, project_id=project_id,
                                task_id=task_id, type=type,
                                path=f"{project_id}/{ref.id}", created_by=created_by))
    return ref.id


async def build_scenario(repo: Repository, store) -> dict:
    """A realistic director -> research -> critic ensemble.

    The director and the researcher have each written a private scratchpad and
    a public artifact. The critic's task references the proposal only. The
    critic must receive facts and the proposal — not the director's reasoning.
    """
    inner = store._inner if isinstance(store, RecordingStore) else store

    director_task = Task(project_id=PROJECT, type="plan",
                         objective="Decide whether to acquire the Helsinki plant",
                         assigned_agent="director", status=TaskStatus.RUNNING)
    repo.save_task(director_task)

    research_task = Task(project_id=PROJECT, type="research",
                         objective="Gather plant financials",
                         assigned_agent="research", status=TaskStatus.COMPLETE,
                         parent_task_id=director_task.id)
    repo.save_task(research_task)

    director_pad = await _artifact(repo, inner, task_id=director_task.id,
                                   created_by="director",
                                   content=DIRECTOR_SCRATCHPAD, type="scratchpad")
    research_pad = await _artifact(repo, inner, task_id=research_task.id,
                                    created_by="research",
                                    content=RESEARCH_SCRATCHPAD, type="scratchpad")
    proposal = await _artifact(repo, inner, task_id=director_task.id,
                               created_by="director", content=PROPOSAL_BODY)
    finance = await _artifact(repo, inner, task_id=research_task.id,
                              created_by="research", content=FINANCE_BODY,
                              type="json")

    repo.record_run(RunRecord(project_id=PROJECT, task_id=director_task.id,
                              agent="director", event_type="plan",
                              output_refs={"proposal": proposal,
                                           "scratchpad": director_pad}))
    repo.record_run(RunRecord(project_id=PROJECT, task_id=research_task.id,
                              agent="research", event_type="research.complete",
                              output_refs={"findings": finance,
                                           "scratchpad": research_pad}))
    for run in repo.list_runs(PROJECT):
        repo.finish_run(run.id, status="COMPLETE", output_refs=run.output_refs)

    critic_task = Task(project_id=PROJECT, type="critique",
                       objective="Critique the Helsinki proposal",
                       assigned_agent="critic",
                       parent_task_id=director_task.id,
                       input_refs={"proposal": proposal})
    repo.save_task(critic_task)

    return {"director_task": director_task, "research_task": research_task,
            "critic_task": critic_task, "director_pad": director_pad,
            "research_pad": research_pad, "proposal": proposal,
            "finance": finance}


async def build_critic(repo, store, **kwargs) -> AgentPrompt:
    scenario = kwargs.pop("scenario")
    return await build_context(agent="critic", task=scenario["critic_task"],
                               repository=repo, store=store,
                               instructions="You are the critic. Find flaws.",
                               **kwargs)


# ------------------------------------------------- rule 2: no foreign reasoning


async def test_another_agents_scratchpad_never_appears(repo, store):
    """The core thesis: private reasoning is absent even though it is *right there*.

    Both scratchpads live in the same project, are registered artifacts, and are
    named in run output_refs the critic's lineage can see.
    """
    scenario = await build_scenario(repo, store)
    prompt = await build_critic(repo, store, scenario=scenario)

    rendered = prompt.render()
    serialized = prompt.model_dump_json()

    for haystack in (rendered, serialized):
        assert DIRECTOR_SCRATCHPAD not in haystack
        assert RESEARCH_SCRATCHPAD not in haystack
        assert "PRIVATE_REASONING" not in haystack
        # Not even the IDs leak: an agent cannot ask for what it cannot name.
        assert scenario["director_pad"] not in haystack
        assert scenario["research_pad"] not in haystack

    # And the facts it *should* have did arrive.
    assert PROPOSAL_BODY in rendered
    assert "Critique the Helsinki proposal" in rendered


async def test_scratchpad_is_not_even_read_from_the_store(repo, store):
    """Isolation by construction: unreferenced bodies are never fetched."""
    recording = RecordingStore(store)
    scenario = await build_scenario(repo, recording)
    recording.reads.clear()

    prompt = await build_critic(repo, recording, scenario=scenario)

    assert recording.reads == [scenario["proposal"]]
    assert scenario["director_pad"] not in recording.reads
    assert scenario["research_pad"] not in recording.reads
    assert scenario["finance"] not in recording.reads
    assert prompt.loaded_refs == [scenario["proposal"]]


async def test_run_envelopes_carry_no_reasoning_and_no_private_refs(repo, store):
    """Recent lines are envelopes: who/what/status, plus public artifact IDs."""
    scenario = await build_scenario(repo, store)
    prompt = await build_critic(repo, store, scenario=scenario)

    joined = "\n".join(prompt.recent)
    assert joined, "the critic should still see that research finished"
    assert "research research.complete" in joined
    assert scenario["finance"] in joined            # a public reference is fine
    assert scenario["research_pad"] not in joined   # the reasoning behind it is not
    assert scenario["director_pad"] not in joined
    assert "PRIVATE_REASONING" not in joined


async def test_explicit_reference_to_a_private_artifact_is_refused(repo, store):
    """Defence in depth: even an explicit ref to a scratchpad is denied and logged."""
    scenario = await build_scenario(repo, store)
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"],
                       "context": scenario["director_pad"]}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert DIRECTOR_SCRATCHPAD not in prompt.render()
    assert prompt.loaded_refs == [scenario["proposal"]]
    skipped = {s.name: s for s in prompt.skipped_refs}
    assert skipped["context"].artifact_id == scenario["director_pad"]
    assert "private artifact type" in skipped["context"].reason


async def test_private_reference_name_is_refused_even_for_a_plain_artifact(repo, store):
    """A scratchpad stored as plain markdown is still refused by its ref name."""
    scenario = await build_scenario(repo, store)
    sneaky = await _artifact(repo, store, task_id=scenario["director_task"].id,
                             created_by="director", content=DIRECTOR_SCRATCHPAD,
                             type="markdown")
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "scratchpad": sneaky}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert DIRECTOR_SCRATCHPAD not in prompt.render()
    assert sneaky not in prompt.loaded_refs
    assert any(s.name == "scratchpad" and "private reference name" in s.reason
               for s in prompt.skipped_refs)


# ------------------------------------------- rule 1: only referenced artifacts


async def test_unreferenced_project_artifact_is_absent(repo, store):
    """An artifact can exist in the project and still be invisible."""
    scenario = await build_scenario(repo, store)
    unrelated = await _artifact(repo, store, task_id=scenario["director_task"].id,
                                created_by="research", content=UNRELATED_BODY)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert UNRELATED_BODY not in prompt.render()
    assert unrelated not in prompt.loaded_refs
    assert list(prompt.inputs) == ["proposal"]


async def test_cross_project_reference_is_refused(repo, store):
    """A reference is not a capability: the project boundary still holds.

    The body sits in the same shared store and the ID is valid — the builder
    still refuses it, because it is not registered in *this* project.
    """
    scenario = await build_scenario(repo, store)
    foreign = await _artifact(repo, store, task_id="task_other",
                              created_by="director", content=UNRELATED_BODY,
                              project_id="proj_beta")
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "leaked": foreign}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert UNRELATED_BODY not in prompt.render()
    assert foreign not in prompt.loaded_refs
    assert any(s.name == "leaked" and "not a registered artifact" in s.reason
               for s in prompt.skipped_refs)


async def test_unregistered_reference_is_refused(repo, store):
    """Deny by default: a body with no metadata cannot be vetted, so it is refused."""
    scenario = await build_scenario(repo, store)
    ref = await store.put(project_id=PROJECT, task_id=scenario["director_task"].id,
                          created_by="director", content=UNRELATED_BODY)
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "orphan": ref.id}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert UNRELATED_BODY not in prompt.render()
    assert any(s.name == "orphan" and "not a registered artifact" in s.reason
               for s in prompt.skipped_refs)


async def test_missing_artifact_is_recorded_not_raised(repo, store):
    """Registered metadata, body gone: recorded, never an exception."""
    scenario = await build_scenario(repo, store)
    repo.save_artifact(Artifact(id="art_ghost", project_id=PROJECT,
                                task_id=scenario["director_task"].id,
                                type="markdown", path="gone.md",
                                created_by="director"))
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "ghost": "art_ghost"}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    assert prompt.loaded_refs == [scenario["proposal"]]
    assert any(s.artifact_id == "art_ghost" and s.reason == "missing-from-store"
               for s in prompt.skipped_refs)


# ------------------------------------------------- rule 4: the audit trail


async def test_loaded_refs_mirrors_inputs_exactly(repo, store):
    scenario = await build_scenario(repo, store)
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"],
                       "findings": scenario["finance"],
                       "scratchpad": scenario["director_pad"]}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario)

    expected = [task.input_refs[name] for name in sorted(prompt.inputs)]
    assert sorted(prompt.loaded_refs) == sorted(expected)
    # Every reference is accounted for: loaded or explicitly skipped.
    accounted = set(prompt.loaded_refs) | {s.artifact_id for s in prompt.skipped_refs}
    assert accounted == set(task.input_refs.values())


# ------------------------------------------------------ rule 3: size limits


async def test_max_chars_truncates_deterministically(repo, store):
    scenario = await build_scenario(repo, store)
    big = await _artifact(repo, store, task_id=scenario["director_task"].id,
                          created_by="research", content="A" * 50_000)
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "report": big}
    repo.save_task(task)

    limits = ContextLimits(max_chars=2_000, max_recent=2)
    first = await build_critic(repo, store, scenario=scenario, limits=limits)
    second = await build_critic(repo, store, scenario=scenario, limits=limits)

    assert first.truncated is True
    assert len(first.render()) <= limits.max_chars
    assert first.render() == second.render()
    assert "…" in first.inputs["report"]
    assert PROPOSAL_BODY in first.render()          # the small input survives whole


async def test_untruncated_context_is_not_flagged(repo, store):
    scenario = await build_scenario(repo, store)
    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_chars=50_000))
    assert prompt.truncated is False
    assert not any("budget" in s.reason for s in prompt.skipped_refs)


async def test_input_dropped_for_budget_is_recorded_not_silent(repo, store):
    scenario = await build_scenario(repo, store)
    big = await _artifact(repo, store, task_id=scenario["director_task"].id,
                          created_by="research", content="B" * 20_000)
    task = scenario["critic_task"]
    task.input_refs = {"proposal": scenario["proposal"], "report": big}
    repo.save_task(task)

    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_chars=700))

    assert prompt.truncated is True
    assert len(prompt.render()) <= 700
    dropped = [s for s in prompt.skipped_refs if s.artifact_id == big]
    assert dropped and "budget" in dropped[0].reason
    assert big not in prompt.loaded_refs        # audit trail stays honest


# ------------------------------------------- rule 5: bounded, scoped `recent`


async def test_recent_is_capped(repo, store):
    scenario = await build_scenario(repo, store)
    for i in range(40):
        run = repo.record_run(RunRecord(project_id=PROJECT,
                                        task_id=scenario["research_task"].id,
                                        agent="research",
                                        event_type=f"step.{i}"))
        repo.finish_run(run.id, status="COMPLETE")

    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_recent=3))

    assert len(prompt.recent) == 3


async def test_recent_excludes_unrelated_lineage(repo, store):
    """Same project, different branch: not this task's business."""
    scenario = await build_scenario(repo, store)
    stranger = Task(project_id=PROJECT, type="ops", objective="Rotate keys",
                    assigned_agent="monitor")
    repo.save_task(stranger)
    run = repo.record_run(RunRecord(project_id=PROJECT, task_id=stranger.id,
                                    agent="monitor", event_type="rotate.keys"))
    repo.finish_run(run.id, status="COMPLETE")

    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_recent=10))

    joined = "\n".join(prompt.recent)
    assert "rotate.keys" not in joined
    assert stranger.id not in joined
    assert "research.complete" in joined     # the sibling result does belong


async def test_recent_includes_direct_children_only(repo, store):
    scenario = await build_scenario(repo, store)
    child = Task(project_id=PROJECT, type="check", objective="Verify numbers",
                 assigned_agent="finance", parent_task_id=scenario["critic_task"].id)
    grandchild = Task(project_id=PROJECT, type="check", objective="Deep check",
                      assigned_agent="finance", parent_task_id=child.id)
    repo.save_task(child)
    repo.save_task(grandchild)
    for task_id, event_type in ((child.id, "child.done"),
                                (grandchild.id, "grandchild.done")):
        run = repo.record_run(RunRecord(project_id=PROJECT, task_id=task_id,
                                        agent="finance", event_type=event_type))
        repo.finish_run(run.id, status="COMPLETE")

    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_recent=10))

    joined = "\n".join(prompt.recent)
    assert "child.done" in joined
    assert "grandchild.done" not in joined


async def test_explicit_event_is_the_latest_relevant_result(repo, store):
    scenario = await build_scenario(repo, store)
    event = Event(topic="proposal.ready", project_id=PROJECT,
                  task_id=scenario["director_task"].id,
                  payload={"artifact_id": scenario["proposal"],
                           "scratchpad": scenario["director_pad"]})

    prompt = await build_critic(repo, store, scenario=scenario,
                                limits=ContextLimits(max_recent=2), event=event)

    assert prompt.recent[-1].startswith("event proposal.ready")
    assert len(prompt.recent) == 2
    # Even an event payload cannot smuggle a scratchpad reference through.
    assert scenario["director_pad"] not in prompt.render()


async def test_event_from_another_project_is_rejected(repo, store):
    scenario = await build_scenario(repo, store)
    foreign = Event(topic="proposal.ready", project_id="proj_beta")
    with pytest.raises(ValueError):
        await build_critic(repo, store, scenario=scenario, event=foreign)


# --------------------------------------------------- rules 3 and 6: structure


async def test_project_state_is_small_and_selected(repo, store):
    scenario = await build_scenario(repo, store)
    prompt = await build_critic(repo, store, scenario=scenario)

    assert set(prompt.project_state) == {
        "project_id", "task_id", "task_type", "task_status", "open_tasks",
        "artifact_count", "parent_task_id", "parent_objective",
    }
    # Counts and lineage only — no bodies, no transcripts.
    assert len(prompt.render()) < 4_000


async def test_build_is_deterministic(repo, store):
    scenario = await build_scenario(repo, store)
    first = await build_critic(repo, store, scenario=scenario)
    second = await build_critic(repo, store, scenario=scenario)

    assert first.render() == second.render()
    assert first.model_dump() == second.model_dump()


async def test_render_is_stable_under_dict_ordering():
    a = AgentPrompt(agent="critic", instructions="i", objective="o",
                    project_state={"b": 2, "a": 1},
                    inputs={"z": "zz", "a": "aa"})
    b = AgentPrompt(agent="critic", instructions="i", objective="o",
                    project_state={"a": 1, "b": 2},
                    inputs={"a": "aa", "z": "zz"})
    assert a.render() == b.render()


async def test_render_contains_role_and_objective(repo, store):
    scenario = await build_scenario(repo, store)
    prompt = await build_critic(repo, store, scenario=scenario)
    rendered = prompt.render()

    assert rendered.startswith("# Agent: critic")
    assert "You are the critic. Find flaws." in rendered
    assert "## Objective\nCritique the Helsinki proposal" in rendered
