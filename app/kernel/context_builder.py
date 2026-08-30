"""Bounded context reconstruction — the project's core architectural rule.

Agents do NOT inherit a shared or global transcript. Every invocation builds a
fresh, bounded context from exactly five sources (V1 PRD, "Agent execution and
context"):

1. the agent's own instructions/role;
2. the current ``Task`` (its objective);
3. a small *selected* slice of project state — counts and lineage, never bodies;
4. the artifacts named in ``task.input_refs``, and nothing else;
5. optionally the latest directly relevant results (see the scoping rule below).

Two things are deliberately absent, and their absence is tested:

* **No other agent's scratchpad or reasoning.** Nothing here ever reads an
  artifact the task did not explicitly reference, and even a referenced
  artifact is refused when it is marked private (``type="scratchpad"`` and
  friends, or an input key named ``scratchpad``). Run records contribute a
  one-line envelope — agent, event type, status, non-private output refs —
  never an output body. The critic receives facts and the proposal, not the
  director's reasoning history.
* **No search and no full history.** There is no vector store, no "load every
  event in the project". ``recent`` is lineage-scoped and hard-capped.

Everything here is pure and deterministic: the same repository, store, task and
limits always produce a byte-identical ``AgentPrompt.render()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.db.repository import Repository
from app.kernel.artifacts import ArtifactStore
from app.kernel.models import Artifact, Event, RunRecord, Task, TaskStatus

# Artifact types that are private to the agent that produced them. These are an
# agent's own thinking; they never enter another agent's context, even when a
# task explicitly references them.
PRIVATE_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"scratchpad", "reasoning", "thinking", "chain_of_thought", "transcript"}
)

# Reference *names* that mean the same thing, for artifacts that were stored
# without a private type. Applies to ``task.input_refs`` keys and to
# ``RunRecord.output_refs`` keys.
PRIVATE_REF_KEYS: frozenset[str] = frozenset(
    {"scratchpad", "reasoning", "thinking", "chain_of_thought", "transcript",
     "monologue", "notes_private"}
)

_TRUNCATION_MARKER = "\n… [truncated]"
_MIN_BODY_CHARS = 64          # below this a slice is useless — drop and record instead
_MAX_EVENT_PAYLOAD_CHARS = 400
_MAX_LINEAGE_DEPTH = 16       # ancestor walk guard

_TERMINAL_STATUSES = {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.REJECTED}


@dataclass(frozen=True)
class ContextLimits:
    """Hard ceilings on a reconstructed context.

    ``max_chars`` bounds the rendered prompt; ``max_recent`` bounds how many
    prior results may be summarised. Both are enforced, not advisory.

    The agent's instructions and its objective are never truncated — they *are*
    the invocation. A ``max_chars`` smaller than those two cannot be met; the
    builder then drops everything else and still reports ``truncated=True``.
    """

    max_chars: int = 12_000
    max_recent: int = 3


class SkippedRef(BaseModel):
    """A referenced input that did not make it into the context, and why.

    Nothing is ever dropped silently — every ``input_refs`` entry ends up in
    either ``AgentPrompt.inputs`` or here.
    """

    name: str
    artifact_id: str
    reason: str


class AgentPrompt(BaseModel):
    """The complete context handed to one agent invocation.

    ``loaded_refs`` is the audit trail the PRD asks for: exactly the artifact
    IDs whose bodies were read and are present in ``inputs``.
    """

    agent: str
    instructions: str
    objective: str
    project_state: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)
    recent: list[str] = Field(default_factory=list)
    loaded_refs: list[str] = Field(default_factory=list)
    truncated: bool = False
    skipped_refs: list[SkippedRef] = Field(default_factory=list)

    # ---------------------------------------------------------------- render

    def render(self) -> str:
        """Deterministic plain text for the LLM harness.

        Dict-backed sections are emitted in sorted key order so that two
        equivalent prompts render byte-identically regardless of insertion
        order.
        """
        parts: list[str] = [f"# Agent: {self.agent}", "", "## Instructions",
                            self.instructions.strip(), "", "## Objective",
                            self.objective.strip()]

        if self.project_state:
            parts += ["", "## Project state"]
            parts += [f"- {key}: {_scalar(self.project_state[key])}"
                      for key in sorted(self.project_state)]

        if self.inputs:
            parts += ["", "## Inputs"]
            for name in sorted(self.inputs):
                parts += [f"### {name}", self.inputs[name]]

        if self.recent:
            parts += ["", "## Recent"]
            parts += [f"- {line}" for line in self.recent]

        if self.skipped_refs:
            parts += ["", "## Omitted references"]
            parts += [f"- {ref.name} ({ref.artifact_id}): {ref.reason}"
                      for ref in sorted(self.skipped_refs,
                                        key=lambda r: (r.name, r.artifact_id))]

        if self.truncated:
            parts += ["", "## Note", "This context was truncated to fit its budget."]

        return "\n".join(parts).rstrip() + "\n"


def _scalar(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


# --------------------------------------------------------------------- build


async def build_context(*, agent: str, task: Task, repository: Repository,
                        store: ArtifactStore, instructions: str,
                        limits: ContextLimits | None = None,
                        event: Event | None = None) -> AgentPrompt:
    """Reconstruct one agent's bounded context. Pure and deterministic.

    Reads exactly: the task's own fields, small project counts, the artifacts
    named in ``task.input_refs``, and lineage-scoped run envelopes. It never
    reads an artifact body that the task did not reference, so another agent's
    scratchpad cannot reach this prompt even when it sits in the same project.

    ``event`` is the optional "latest directly relevant event" of PRD item 5 —
    the caller passes the one event that triggered this invocation. It must
    belong to the same project.
    """
    limits = limits or ContextLimits()
    if event is not None and event.project_id != task.project_id:
        raise ValueError(
            f"event {event.id} belongs to project {event.project_id}, "
            f"not {task.project_id}"
        )

    artifacts = {a.id: a for a in repository.list_artifacts(task.project_id)}
    inputs, loaded, skipped = await _load_inputs(task, store, artifacts)

    prompt = AgentPrompt(
        agent=agent,
        instructions=instructions,
        objective=task.objective,
        project_state=_project_state(task, repository, artifacts),
        inputs=inputs,
        recent=_recent(task, repository, limits, event),
        loaded_refs=loaded,
        skipped_refs=skipped,
    )
    _enforce_char_budget(prompt, task, limits)
    return prompt


# --------------------------------------------------------------- (4) inputs


async def _load_inputs(
    task: Task, store: ArtifactStore, artifacts: dict[str, Artifact],
) -> tuple[dict[str, str], list[str], list[SkippedRef]]:
    """Load *only* the artifacts named in ``task.input_refs``.

    This is the whole of rule 1: the loop iterates the task's references, never
    the project's artifacts. ``artifacts`` is consulted for metadata only — to
    refuse a private or cross-project reference — and is never used to widen
    what gets read.
    """
    inputs: dict[str, str] = {}
    loaded: list[str] = []
    skipped: list[SkippedRef] = []

    for name in sorted(task.input_refs):
        artifact_id = task.input_refs[name]
        reason = _refusal_reason(name, artifact_id, task, artifacts)
        if reason is not None:
            skipped.append(SkippedRef(name=name, artifact_id=artifact_id,
                                      reason=reason))
            continue
        try:
            body = await store.get(artifact_id)
        except (KeyError, FileNotFoundError):
            skipped.append(SkippedRef(name=name, artifact_id=artifact_id,
                                      reason="missing-from-store"))
            continue
        inputs[name] = body
        loaded.append(artifact_id)

    return inputs, loaded, skipped


def _refusal_reason(name: str, artifact_id: str, task: Task,
                    artifacts: dict[str, Artifact]) -> str | None:
    """Why this explicitly-referenced artifact must not be loaded, if it must not.

    Deny by default: ``artifacts`` holds only this project's registered
    artifacts, so anything absent from it — an unregistered body, or one
    belonging to another project — is refused rather than read. A reference is
    not a capability.
    """
    if name.strip().lower() in PRIVATE_REF_KEYS:
        return f"private reference name {name!r} — another agent's reasoning"
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        return f"not a registered artifact of project {task.project_id}"
    if artifact.type.strip().lower() in PRIVATE_ARTIFACT_TYPES:
        return f"private artifact type {artifact.type!r} — another agent's reasoning"
    return None


# -------------------------------------------------------- (3) project state


def _project_state(task: Task, repository: Repository,
                   artifacts: dict[str, Artifact]) -> dict[str, Any]:
    """A handful of small selected fields — counts and lineage, never bodies.

    Objectives are commands, not reasoning, so the parent task's objective is
    included; no other task's content is.
    """
    tasks = repository.list_tasks(task.project_id)
    state: dict[str, Any] = {
        "project_id": task.project_id,
        "task_id": task.id,
        "task_type": task.type,
        "task_status": task.status.value,
        "open_tasks": sum(1 for t in tasks if t.status not in _TERMINAL_STATUSES),
        "artifact_count": len(artifacts),
    }
    if task.parent_task_id:
        state["parent_task_id"] = task.parent_task_id
        parent = repository.get_task(task.parent_task_id)
        if parent is not None and parent.project_id == task.project_id:
            state["parent_objective"] = parent.objective
    return state


# ---------------------------------------------------------------- (5) recent


def _recent(task: Task, repository: Repository, limits: ContextLimits,
            event: Event | None) -> list[str]:
    """The narrowly-scoped "latest directly relevant result" lines.

    **Scoping rule.** A run may be summarised only if it is in the same project
    *and* its ``task_id`` is in this task's immediate lineage:

    * the task itself;
    * its ancestors, walking ``parent_task_id`` upwards;
    * its siblings — tasks sharing the same ``parent_task_id``;
    * its direct children — tasks whose ``parent_task_id`` is this task.

    Nothing else in the project qualifies: no cousins, no unrelated branches,
    no whole-project history, no similarity search. The surviving runs are
    sorted by ``(started_at, id)`` and only the last ``max_recent`` are kept,
    so a busy project cannot inflate the context.

    Each line is an *envelope*: agent, event type, status, and non-private
    output artifact IDs. Run output bodies and error text never appear, so a
    run cannot smuggle in the reasoning its artifacts hold.
    """
    if limits.max_recent <= 0:
        return []

    lineage = _lineage_task_ids(task, repository)
    runs = [r for r in repository.list_runs(task.project_id)
            if r.task_id in lineage and r.finished_at is not None]
    runs.sort(key=lambda r: (r.started_at, r.id))

    budget = limits.max_recent - (1 if event is not None else 0)
    lines = [_run_line(r) for r in runs[-budget:]] if budget > 0 else []
    if event is not None:
        lines.append(_event_line(event))
    return lines


def _lineage_task_ids(task: Task, repository: Repository) -> set[str]:
    tasks = repository.list_tasks(task.project_id)
    lineage = {task.id}

    ancestor_id, seen = task.parent_task_id, set()
    for _ in range(_MAX_LINEAGE_DEPTH):
        if ancestor_id is None or ancestor_id in seen:
            break
        seen.add(ancestor_id)
        ancestor = repository.get_task(ancestor_id)
        if ancestor is None or ancestor.project_id != task.project_id:
            break
        lineage.add(ancestor.id)
        ancestor_id = ancestor.parent_task_id

    for other in tasks:
        if other.parent_task_id == task.id:
            lineage.add(other.id)                      # direct children
        elif task.parent_task_id and other.parent_task_id == task.parent_task_id:
            lineage.add(other.id)                      # siblings
    return lineage


def _run_line(run: RunRecord) -> str:
    line = (f"{run.agent} {run.event_type} on task {run.task_id} "
            f"-> {run.status}")
    refs = _public_refs(run.output_refs)
    if refs:
        line += f" [artifacts: {', '.join(refs)}]"
    return line


def _public_refs(output_refs: dict[str, Any]) -> list[str]:
    """Artifact IDs a downstream agent may know about — private keys removed."""
    return [str(output_refs[key]) for key in sorted(output_refs)
            if key.strip().lower() not in PRIVATE_REF_KEYS
            and isinstance(output_refs[key], str)]


def _event_line(event: Event) -> str:
    payload = {k: v for k, v in event.payload.items()
               if k.strip().lower() not in PRIVATE_REF_KEYS}
    body = json.dumps(payload, sort_keys=True, default=str)
    if len(body) > _MAX_EVENT_PAYLOAD_CHARS:
        body = body[:_MAX_EVENT_PAYLOAD_CHARS] + "…"
    return f"event {event.topic} (task {event.task_id}) {body}"


# ------------------------------------------------------------- (3) budgeting


def _enforce_char_budget(prompt: AgentPrompt, task: Task,
                         limits: ContextLimits) -> None:
    """Shrink the prompt until it fits ``max_chars``. Deterministic.

    Largest input body first (ties broken by name), trimmed to the exact
    overflow; a body that would be reduced below ``_MIN_BODY_CHARS`` is dropped
    whole and recorded in ``skipped_refs`` instead of being kept as a useless
    sliver. ``recent`` lines are dropped last, oldest first — they are the
    optional source. Instructions and objective are never touched.
    """
    overflow = len(prompt.render()) - limits.max_chars
    while overflow > 0 and prompt.inputs:
        name = max(sorted(prompt.inputs), key=lambda n: len(prompt.inputs[n]))
        keep = len(prompt.inputs[name]) - overflow - len(_TRUNCATION_MARKER)
        prompt.truncated = True
        if keep < _MIN_BODY_CHARS:
            artifact_id = task.input_refs[name]
            del prompt.inputs[name]
            if artifact_id in prompt.loaded_refs:
                prompt.loaded_refs.remove(artifact_id)
            prompt.skipped_refs.append(SkippedRef(
                name=name, artifact_id=artifact_id,
                reason="dropped — context character budget exhausted"))
        else:
            prompt.inputs[name] = prompt.inputs[name][:keep] + _TRUNCATION_MARKER
        overflow = len(prompt.render()) - limits.max_chars

    while overflow > 0 and prompt.recent:
        prompt.recent.pop(0)
        prompt.truncated = True
        overflow = len(prompt.render()) - limits.max_chars
