"""Coordinates the team and owns the human decision point.

Holds no transcript. Progress is inferred from persisted semantic state
(which tasks completed, which artifacts exist), not from a running conversation.
"""

from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

#: Both must land before a proposal can be assembled. Keyed on artifact *type*,
#: not author, so a research report is equally acceptable from this team's own
#: research agent or from another team over the bus. That substitutability is
#: what makes V2 delegation work without changing this logic.
REQUIRED_INPUT_TYPES = ("research", "valuation")

#: Which local specialist produces each required input.
LOCAL_PRODUCERS = {"research": "research", "valuation": "finance"}


@agent("director")
async def director(ctx: AgentContext, task: Task) -> dict:
    if task.type in ("evaluate_company", "coordinate"):
        return await _kick_off(ctx, task)
    if task.type == "evaluate_company_delegated":
        return await _kick_off_delegated(ctx, task)
    if task.type in ("on:research.complete", "on:finance.complete",
                     "on:research.report.ready"):
        return await _maybe_propose(ctx, task)
    if task.type == "on:critique.complete":
        return await _synthesize(ctx, task)
    return {"ignored": task.type}


async def _kick_off(ctx: AgentContext, task: Task) -> dict:
    dispatched = []
    for specialist in LOCAL_PRODUCERS.values():
        await ctx.send(specialist, "analyze", objective=task.objective,
                       parent_task_id=task.id)
        dispatched.append(specialist)
    return {"dispatched": dispatched}


async def _kick_off_delegated(ctx: AgentContext, task: Task) -> dict:
    """Same work, but research comes from another team over the bus.

    The only difference from _kick_off is the address. No retry, routing or
    transport code appears here — that is what the BusAdapter seam buys.
    """
    await ctx.send("finance", "analyze", objective=task.objective,
                   parent_task_id=task.id)
    await ctx.send("team://research/web-researcher", "research_company",
                   objective=task.objective, parent_task_id=task.id)
    return {"dispatched": ["finance", "team://research/web-researcher"]}


#: Cross-team topics whose artifact satisfies one of our required inputs.
EXTERNAL_INPUTS = {"on:research.report.ready": "research"}


async def _maybe_propose(ctx: AgentContext, task: Task) -> dict:
    """Assemble a proposal once both required inputs exist.

    An input may arrive from this team's own specialist or, by reference, from
    another team over the bus. The director does not care which.
    """
    kind = EXTERNAL_INPUTS.get(task.type)
    if kind and task.input_refs:
        for artifact_id in task.input_refs.values():
            ctx.record_external_artifact(
                task=task, artifact_id=artifact_id, type=kind,
                source=task.source or task.assigned_agent)

    artifacts = ctx.project_artifacts()

    # Idempotent at the semantic level: proposal.ready fans out to finance, so
    # without this guard a second finance.complete would re-propose endlessly.
    existing = next((a for a in artifacts if a.type == "proposal"), None)
    if existing is not None:
        return {"already_proposed": existing.id}

    by_type = {a.type: a for a in artifacts if a.type in REQUIRED_INPUT_TYPES}
    if not all(kind in by_type for kind in REQUIRED_INPUT_TYPES):
        return {"waiting_for": [k for k in REQUIRED_INPUT_TYPES if k not in by_type]}

    proposal = await ctx.put_artifact(
        task=task, created_by="director", type="proposal",
        content="# Proposal\n\n" + "\n".join(
            f"- {kind} ({by_type[kind].created_by}): {by_type[kind].id}"
            for kind in REQUIRED_INPUT_TYPES))

    # The critic receives the proposal by reference — not the reasoning behind it.
    await ctx.publish("proposal.ready", {"artifact_id": proposal.id})
    return {"proposal": proposal.id}


async def _synthesize(ctx: AgentContext, task: Task) -> dict:
    synthesis = await ctx.llm.run(
        instructions=ctx.prompt("director"),
        input=task.objective,
        agent="director",
    )
    ref = await ctx.put_artifact(task=task, created_by="director",
                                 type="synthesis",
                                 content=f"# Synthesis\n\n{synthesis}\n")
    decision = await ctx.request_approval(task, prompt=f"Approve {ref.id}?")
    return {"synthesis": ref.id, "decision": decision.decision}
