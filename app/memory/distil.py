"""Decision → memory note: the team's only automatic learning signal.

The team already produces a labelled dataset and V3 threw it away: **every
approve/reject decision, together with the proposal and critique that led to
it** (V4 PRD, "The learning signal"). A human pressing Reject with a reason is
the team being told what good looks like. V4 keeps exactly one durable note per
decision.

Everything here is deliberately small and deliberately boring:

* **No model is required.** With ``llm=None`` the summary is assembled from
  structured facts — the verdict, the objective, the human's stated reason and
  which artifacts were involved. That path always works, offline, with no
  endpoint. An ``llm`` only makes the prose nicer; it never becomes the thing
  the feature depends on.
* **Provenance or it did not happen** (PRD rule 3). Every note names the run and
  the artifacts it came from. A note with no sources is a rumour, so this module
  refuses to write one rather than inventing grounding.
* **Never store another agent's reasoning** (PRD rule 5). A scratchpad is never
  read, never quoted and never cited as a source — the same deny list
  ``app/kernel/context_builder.py`` enforces for context, imported from there so
  the two cannot drift. A critique and a proposal are another agent's *output*
  and are fine; a scratchpad is its thinking and is not.
* **Bounded by construction** (PRD rule 6). A summary is a few short lines. An
  artifact body is never inlined — at most one truncated line of it is quoted.

Idempotency
-----------
Distillation runs once per decision, not once per run, and re-running it must
not write a second note or a second timeline entry. Two things give that:

1. a **deterministic subject** derived from the decision
   (:func:`decision_subject`), so the same decision always addresses the same
   note — and ``MemoryStore.remember`` extends an existing ``(kind, subject)``
   rather than adding a near-duplicate;
2. a **source check** before writing: if a note on that subject already lists
   this run in its ``sources``, the existing reference is returned untouched.

Check (2) is what makes it safe to pass an explicit topical ``subject`` that
several decisions share: each decision still contributes exactly one timeline
entry to it, however many times distillation is replayed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.agents.base import LLMRunner
from app.db.repository import Repository
from app.kernel.artifacts import ArtifactStore
from app.kernel.context_builder import PRIVATE_ARTIFACT_TYPES, PRIVATE_REF_KEYS
from app.kernel.memory import (Confidence, MemoryKind, MemoryRef, MemoryStore,
                               NullMemoryStore)
from app.kernel.models import Artifact, RunRecord, Task

#: Run event types that record a human verdict. ``approval.requested`` is what
#: ``Kernel.request_approval`` writes; the others are the names the PRD's
#: ``decision.completed ──▶ distil`` diagram uses, accepted so a rename upstream
#: does not silently stop the team learning.
DECISION_EVENT_TYPES: frozenset[str] = frozenset(
    {"approval.requested", "approval.completed", "decision.completed"}
)

#: Spellings of the two verdicts, normalised to the two canonical ones.
VERDICTS: dict[str, str] = {"approve": "approve", "approved": "approve",
                            "reject": "reject", "rejected": "reject"}

_PAST_TENSE = {"approve": "Approved", "reject": "Rejected"}

#: Artifact types that count as the thing being judged, best first.
PROPOSAL_TYPES: tuple[str, ...] = ("proposal", "synthesis", "plan")

#: Artifact types that count as the objection to it, best first.
CRITIQUE_TYPES: tuple[str, ...] = ("critique", "review")

_ARTIFACT_ID = re.compile(r"art_[A-Za-z0-9_-]+")

# Bounds. A note is a few lines; an artifact body is never inlined wholesale.
MAX_OBJECTIVE_CHARS = 200
MAX_REASON_CHARS = 400
MAX_EXCERPT_CHARS = 120
MAX_SUMMARY_CHARS = 900
MAX_EVIDENCE_CHARS = 200
_TRUNCATION_MARKER = "…"

#: How many notes the idempotency probe may inspect. Bounded like everything
#: else: the deterministic subject puts the note we want at the top of a lexical
#: ranking, and a corpus that needs more than this to find it has a bug.
_LOOKUP_LIMIT = 20

DISTIL_INSTRUCTIONS = """\
You are writing one durable memory note from a human's approve/reject decision.
Write at most three short sentences of plain prose stating what the team should
do differently next time. State only what the facts below support. Do not invent
detail, do not speculate about anyone's reasoning, and do not quote at length.
"""


@dataclass(frozen=True)
class Decision:
    """One human verdict, with what it was about."""

    run_id: str
    project_id: str
    verdict: str              # "approve" | "reject"
    note: str | None          # the human's stated reason, if any
    proposal_id: str | None
    critique_id: str | None
    objective: str


# --------------------------------------------------------------- discovery


def find_decisions(repository: Repository, project_id: str) -> list[Decision]:
    """Every human verdict in a project, oldest first.

    A run qualifies only if it is a decision event *and* carries a recognised
    verdict in ``output_refs``. Everything else in the timeline — sends,
    publishes, wakeups, and approvals still parked on a human — is ignored.
    """
    artifacts = {a.id: a for a in repository.list_artifacts(project_id)}
    decisions: list[Decision] = []

    for run in repository.list_runs(project_id):
        if run.event_type not in DECISION_EVENT_TYPES:
            continue
        verdict = normalise_verdict(run.output_refs.get("decision"))
        if verdict is None:
            continue

        task = repository.get_task(run.task_id) if run.task_id else None
        proposal_id, critique_id = _judged_artifacts(run, task, artifacts)
        decisions.append(Decision(
            run_id=run.id,
            project_id=project_id,
            verdict=verdict,
            note=_clean(run.output_refs.get("note")),
            proposal_id=proposal_id,
            critique_id=critique_id,
            objective=_objective(run, task, repository),
        ))
    return decisions


def normalise_verdict(value: object) -> str | None:
    """``"Approved"`` → ``"approve"``. Anything unrecognised → ``None``."""
    if not isinstance(value, str):
        return None
    return VERDICTS.get(value.strip().lower())


def _objective(run: RunRecord, task: Task | None,
                repository: Repository | None = None) -> str:
    """What the decision was about — the objective a human wrote.

    Shares one lineage walk with the agent layer (app/kernel/lineage.py); two
    definitions drifting is how a note gets filed under the wrong subject.
    """
    from app.kernel.lineage import is_generated, meaningful_objective

    if repository is not None and task is not None:
        found = meaningful_objective(repository, task)
        if not is_generated(found):
            return found

    if task is not None and not is_generated(task.objective):
        return task.objective.strip()

    prompt = run.input_refs.get("prompt")
    return prompt if isinstance(prompt, str) else (task.objective if task else "")


def _judged_artifacts(run: RunRecord, task: Task | None,
                      artifacts: dict[str, Artifact],
                      ) -> tuple[str | None, str | None]:
    """The proposal and critique this verdict was passed on.

    Explicitly referenced artifacts win — the approval prompt names what the
    human looked at. Failing that, the latest artifact of the right type that
    existed *when the decision was taken*, so a second decision in the same
    project is not credited with a proposal written after it.
    """
    referenced = _referenced_artifact_ids(run, task)
    cutoff = run.finished_at or run.started_at

    def pick(types: Sequence[str]) -> str | None:
        for artifact_id in referenced:
            artifact = artifacts.get(artifact_id)
            if artifact is not None and _may_cite(artifact) \
                    and artifact.type.strip().lower() in types:
                return artifact.id
        candidates = [a for a in artifacts.values() if _may_cite(a)
                      and a.type.strip().lower() in types
                      and _at_or_before(a.created_at, cutoff)]
        if not candidates:
            return None
        best = min(candidates, key=lambda a: (types.index(a.type.strip().lower()),
                                              -a.created_at.timestamp(), a.id))
        return best.id

    return pick(PROPOSAL_TYPES), pick(CRITIQUE_TYPES)


def _at_or_before(created_at: datetime, cutoff: datetime | None) -> bool:
    return cutoff is None or created_at <= cutoff


def _referenced_artifact_ids(run: RunRecord, task: Task | None) -> list[str]:
    """Artifact IDs this decision explicitly names, in a stable order.

    References filed under a private key (``scratchpad`` and friends) are not
    even collected — the deny list applies before anything is looked up, let
    alone read.
    """
    found: list[str] = []
    sources: Iterable[dict] = [
        task.input_refs if task is not None else {},
        run.input_refs,
        run.output_refs,
    ]
    for refs in sources:
        for key in sorted(refs):
            if key.strip().lower() in PRIVATE_REF_KEYS:
                continue
            value = refs[key]
            if isinstance(value, str):
                found.extend(_ARTIFACT_ID.findall(value))
    return list(dict.fromkeys(found))


def _may_cite(artifact: Artifact) -> bool:
    """Deny by default for another agent's thinking (PRD rule 5)."""
    return artifact.type.strip().lower() not in PRIVATE_ARTIFACT_TYPES


# ------------------------------------------------------------ distillation


def decision_subject(decision: Decision) -> str:
    """The deterministic note subject for one decision.

    Derived only from the decision, so distilling it again addresses the same
    note. The run id keeps it unique per decision — "exactly one note per
    decision" is a property of the subject, not of a caller remembering to
    check.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", decision.objective.lower()).strip("-")
    stem = stem[:48].strip("-") or "decision"
    return f"decision-{stem}-{decision.run_id}"


def confidence_for(verdict: str, note: str | None, *, has_artifacts: bool,
                   ) -> Confidence:
    """How much this note deserves to be trusted later.

    A rejection with a stated reason is the strongest signal a team gets: the
    human said *no* and said *why*, so the note carries both the label and its
    explanation. A bare approval is the weakest — a click that may mean "good
    enough" as easily as "right".

    ==========  =============  ==========
    verdict     stated reason  confidence
    ==========  =============  ==========
    reject      yes            high
    reject      no             medium
    approve     yes            medium
    approve     no             low
    ==========  =============  ==========

    Then one cap: a note grounded in the run alone, with no proposal or
    critique behind it, never exceeds ``medium`` — the verdict is real but the
    evidence for it is thin, and poisoning is the failure mode this field
    exists to bound.
    """
    if verdict == "reject":
        level = Confidence.HIGH if note else Confidence.MEDIUM
    else:
        level = Confidence.MEDIUM if note else Confidence.LOW
    if not has_artifacts and level is Confidence.HIGH:
        return Confidence.MEDIUM
    return level


async def distil(decision: Decision, *, repository: Repository,
                 artifacts: ArtifactStore, memory: MemoryStore,
                 llm: LLMRunner | None = None,
                 subject: str | None = None) -> MemoryRef | None:
    """Turn one decision into one memory note. Returns ``None`` for a no-op.

    ``None`` means nothing was written, and there are exactly three reasons:

    * memory is disabled (``NullMemoryStore``) — a fully supported mode, so
      this is a silent no-op and never an error;
    * the decision has no provenance at all (no run id, no citable artifact) —
      refused, because a note with no sources is a rumour;
    * the decision was already distilled — the existing reference is returned
      instead, which is the idempotent path and not a ``None``.
    """
    if isinstance(memory, NullMemoryStore):
        return None

    verdict = normalise_verdict(decision.verdict)
    if verdict is None:
        raise ValueError(f"not a verdict: {decision.verdict!r}; "
                         f"expected one of {sorted(set(VERDICTS.values()))}")

    registered = {a.id: a for a in repository.list_artifacts(decision.project_id)}
    cited = _cited_artifacts(decision, registered)
    run_id = decision.run_id.strip()
    sources = list(dict.fromkeys(
        ([run_id] if run_id else []) + [artifact.id for _, artifact in cited]))
    if not sources:
        return None                      # provenance or it did not happen

    subject = subject or decision_subject(decision)
    already = await _already_distilled(memory, subject, decision.run_id)
    if already is not None:
        return already

    reason = _clean(decision.note)
    summary = await _summarise(decision, verdict, reason, cited,
                               artifacts=artifacts, llm=llm)
    return await memory.remember(
        kind=MemoryKind.DECISION,
        subject=subject,
        summary=summary,
        sources=sources,
        confidence=confidence_for(verdict, reason, has_artifacts=bool(cited)),
        evidence=_evidence_line(verdict, reason),
    )


async def distil_project(project_id: str, *, repository: Repository,
                         artifacts: ArtifactStore, memory: MemoryStore,
                         llm: LLMRunner | None = None) -> list[MemoryRef]:
    """Distil every decision in a project. Safe to re-run: see module docstring."""
    refs: list[MemoryRef] = []
    for decision in find_decisions(repository, project_id):
        ref = await distil(decision, repository=repository, artifacts=artifacts,
                           memory=memory, llm=llm)
        if ref is not None:
            refs.append(ref)
    return refs


# --------------------------------------------------------------- internals


def _cited_artifacts(decision: Decision, registered: dict[str, Artifact],
                     ) -> list[tuple[str, Artifact]]:
    """``[("Proposal", artifact), ...]`` — registered, in-project, not private.

    Deny by default, exactly as the context builder does: an artifact this
    project never registered is not evidence, and a scratchpad never becomes
    evidence at all.
    """
    cited: list[tuple[str, Artifact]] = []
    for role, artifact_id in (("Proposal", decision.proposal_id),
                              ("Critique", decision.critique_id)):
        artifact = registered.get(artifact_id or "")
        if artifact is None or artifact.project_id != decision.project_id:
            continue
        if not _may_cite(artifact):
            continue                      # another agent's reasoning
        cited.append((role, artifact))
    return cited


async def _already_distilled(memory: MemoryStore, subject: str,
                             run_id: str) -> MemoryRef | None:
    """The note this decision already wrote, if it wrote one.

    Recall is lexical, so the deterministic subject is used as the query: a note
    whose *subject* is that string outranks everything else. The answer is then
    confirmed against the note itself — the run must already be in its sources —
    so a merely similar note can never be mistaken for this decision's.
    """
    candidates = await memory.recall(subject, limit=_LOOKUP_LIMIT,
                                     kinds=[MemoryKind.DECISION])
    for ref in candidates:
        if ref.subject != subject:
            continue
        try:
            note = await memory.get(ref.id)
        except (KeyError, FileNotFoundError):
            continue
        if run_id and run_id in note.sources:
            return ref
    return None


async def _summarise(decision: Decision, verdict: str, reason: str | None,
                     cited: list[tuple[str, Artifact]], *,
                     artifacts: ArtifactStore,
                     llm: LLMRunner | None) -> str:
    """A few lines of compiled truth. Deterministic unless an LLM is supplied."""
    excerpts = [(role, artifact, await _excerpt(artifacts, artifact.id))
                for role, artifact in cited]
    facts = _fact_lines(decision, verdict, reason, excerpts)

    if llm is None:
        return _bound("\n".join(facts), MAX_SUMMARY_CHARS)

    try:
        prose = (await llm.run(instructions=DISTIL_INSTRUCTIONS,
                               input="\n".join(facts), agent="distil")).strip()
    except Exception:                     # noqa: BLE001 — the model is optional
        prose = ""
    if not prose:
        return _bound("\n".join(facts), MAX_SUMMARY_CHARS)

    # Prose first, then the flat facts that ground it. The human's own words
    # survive whatever the model chose to write.
    grounded = [prose, ""] + facts[:2]
    return _bound("\n".join(grounded), MAX_SUMMARY_CHARS)


def _fact_lines(decision: Decision, verdict: str, reason: str | None,
                excerpts: list[tuple[str, Artifact, str | None]]) -> list[str]:
    objective = _bound(_clean(decision.objective) or "(no objective recorded)",
                       MAX_OBJECTIVE_CHARS)
    lines = [f"{_PAST_TENSE[verdict]}: {objective}"]
    if reason:
        lines.append(f"Human's stated reason: {_bound(reason, MAX_REASON_CHARS)}")
    else:
        lines.append("The human gave no reason.")
    for role, artifact, excerpt in excerpts:
        tail = f" — {excerpt}" if excerpt else ""
        lines.append(f"{role} {artifact.id} ({artifact.type}){tail}")
    if not excerpts:
        lines.append(f"No artifact evidence; grounded in run {decision.run_id}.")
    return lines


async def _excerpt(artifacts: ArtifactStore, artifact_id: str) -> str | None:
    """One truncated line of an artifact, or ``None``.

    Never the body: a memory note that inlined an artifact would be the context
    leak the whole design exists to prevent. Callers must already have checked
    that the artifact is citable — a private one never reaches here, so its
    bytes are never read.
    """
    try:
        body = await artifacts.get(artifact_id)
    except (KeyError, FileNotFoundError, OSError):
        return None
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return _bound(text, MAX_EXCERPT_CHARS)
    return None


def _evidence_line(verdict: str, reason: str | None) -> str:
    """The dated timeline entry. One per decision, ever."""
    stated = _bound(reason, MAX_EVIDENCE_CHARS) if reason else "no reason given"
    return f"{_PAST_TENSE[verdict].lower()}: {stated}"


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _bound(text: str, limit: int) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit - len(_TRUNCATION_MARKER)].rstrip() + _TRUNCATION_MARKER
