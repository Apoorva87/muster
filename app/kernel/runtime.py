"""The kernel: send, publish, wake_later, request_approval.

These are the operations agents call. They are deliberately boring — each one
resolves *agent semantics* (which agent, which topic, which task) and then hands
the *distributed-systems semantics* (durability, retry, timers) to Restate
through the ``KernelContext`` seam.

Replay safety
-------------
Restate re-enters a handler after a crash and replays it. Anything
non-deterministic must be journalled or the replay diverges. Two rules follow:

* IDs are minted through ``ctx.run_typed``, so a replay returns the ID the first
  attempt produced instead of a fresh UUID.
* Repository writes use upsert (``merge``), so replaying a write is a no-op
  rather than a duplicate row. This is why they need no journal entry.

A fresh ``Kernel`` is constructed per invocation; its step counter is what makes
journal names stable across a replay of the same code path.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel

from app.db.repository import Repository
from app.kernel.artifacts import ArtifactStore
from app.kernel.context import KernelContext
from app.kernel.ids import new_id
from app.kernel.models import Event, RunRecord, Task, TaskStatus
from app.kernel.subscriptions import SubscriptionRegistry

#: Every agent Virtual Object exposes exactly one handler.
AGENT_HANDLER = "handle"


class ApprovalDecision(BaseModel):
    decision: str  # "approve" | "reject"
    note: str | None = None
    decided_by: str = "human"

    @property
    def approved(self) -> bool:
        return self.decision == "approve"


class Kernel:
    """Bound to one durable invocation. Construct a new one per handler entry."""

    def __init__(self, *, ctx: KernelContext, repository: Repository,
                 subscriptions: SubscriptionRegistry, artifacts: ArtifactStore,
                 project_id: str | None = None) -> None:
        self._ctx = ctx
        self._repo = repository
        self._subs = subscriptions
        self._artifacts = artifacts
        self._project_id = project_id or ctx.key
        self._step = 0

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifacts

    @property
    def repository(self) -> Repository:
        return self._repo

    # ------------------------------------------------------------- commands

    async def send(self, *, agent: str, task: str, objective: str = "",
                   payload: dict[str, Any] | None = None,
                   input_refs: dict[str, str] | None = None,
                   parent_task_id: str | None = None,
                   delay: timedelta | None = None,
                   event_type: str = "task.sent") -> Task:
        """Durably wake ``agent`` with a bounded task. Does not block."""
        record = Task(
            id=await self._mint("task", f"{agent}:{task}"),
            project_id=self._project_id,
            type=task,
            objective=objective,
            assigned_agent=agent,
            parent_task_id=parent_task_id,
            input_refs=input_refs or {},
        )
        self._repo.save_task(record)

        self._ctx.send(
            agent=agent,
            handler=AGENT_HANDLER,
            key=self._project_id,
            payload={
                "task_id": record.id,
                "type": record.type,
                "objective": record.objective,
                "input_refs": record.input_refs,
                **(payload or {}),
            },
            delay=delay,
            # Restate dedups on this, so a duplicate delivery is not duplicate work.
            idempotency_key=record.id,
        )

        self._repo.record_run(RunRecord(
            id=await self._mint("run", f"send:{agent}:{task}"),
            project_id=self._project_id, task_id=record.id, agent=agent,
            event_type=event_type, status="SENT",
            input_refs=dict(record.input_refs),
        ))
        return record

    # --------------------------------------------------------------- events

    async def publish(self, *, topic: str, payload: dict[str, Any] | None = None,
                      task_id: str | None = None) -> Event:
        """Fan out to every current subscriber of ``topic``.

        Each subscriber is a distinct Virtual Object, so they execute in
        parallel; only repeat events to the *same* agent serialize (decision D2).
        """
        event = Event(
            id=await self._mint("evt", topic),
            topic=topic,
            project_id=self._project_id,
            payload=payload or {},
            task_id=task_id,
        )
        self._repo.save_event(event)

        subscribers = await self._ctx.run_typed(
            f"resolve:{topic}:{self._next_step()}", self._resolve, topic=topic)

        for agent in subscribers:
            await self.send(
                agent=agent,
                task=f"on:{topic}",
                objective=f"React to {topic}",
                payload={"event_id": event.id, "topic": topic, **(payload or {})},
                input_refs={k: v for k, v in (payload or {}).items()
                            if isinstance(v, str) and v.startswith("art_")},
                event_type="event.delivered",
            )

        self._repo.record_run(RunRecord(
            id=await self._mint("run", f"publish:{topic}"),
            project_id=self._project_id, task_id=task_id, agent="kernel",
            event_type="event.published", status="COMPLETE",
            output_refs={"topic": topic, "subscribers": subscribers,
                         "event_id": event.id},
        ))
        return event

    async def _resolve(self, *, topic: str) -> list[str]:
        return self._subs.subscribers_for(topic)

    # --------------------------------------------------------------- timers

    async def wake_later(self, *, agent: str, delay: timedelta,
                         payload: dict[str, Any] | None = None,
                         reason: str = "") -> Task:
        """Schedule a durable future invocation, then return immediately.

        The process exits and Restate owns the timer, so a dormant agent costs
        nothing while it waits. There is never a polling LLM.
        """
        return await self.send(
            agent=agent,
            task="wakeup",
            objective=reason or f"scheduled wakeup for {agent}",
            payload={"reason": reason, **(payload or {})},
            delay=delay,
            event_type="wakeup.scheduled",
        )

    # ------------------------------------------------------------- approval

    async def request_approval(self, *, task: Task, prompt: str,
                               agent: str = "director") -> ApprovalDecision:
        """Park this workflow until a human answers.

        Consumes no model tokens while waiting: the handler suspends on a
        durable promise, and the persisted ``awakeable_id`` is what the web UI's
        Approve button resolves (decision D3).
        """
        run = RunRecord(
            id=await self._mint("run", f"approval:{task.id}"),
            project_id=self._project_id, task_id=task.id, agent=agent,
            event_type="approval.requested", status="WAITING_FOR_HUMAN",
            input_refs={"prompt": prompt},
        )
        self._repo.record_run(run)

        awakeable_id, promise = self._ctx.awakeable()
        self._repo.set_awakeable(run.id, awakeable_id)
        self._repo.set_task_status(task.id, TaskStatus.WAITING_FOR_HUMAN)

        raw = await promise

        decision = (raw if isinstance(raw, ApprovalDecision)
                    else ApprovalDecision(**raw) if isinstance(raw, dict)
                    else ApprovalDecision(decision=str(raw)))

        self._repo.clear_awakeable(run.id)
        self._repo.finish_run(run.id, status="COMPLETE",
                              output_refs={"decision": decision.decision,
                                           "note": decision.note})
        self._repo.set_task_status(
            task.id,
            TaskStatus.COMPLETE if decision.approved else TaskStatus.REJECTED)
        return decision

    # --------------------------------------------------------------- internals

    def _next_step(self) -> int:
        self._step += 1
        return self._step

    async def _mint(self, prefix: str, label: str) -> str:
        """Mint an ID through the journal so a replay reuses it."""
        async def _generate() -> str:
            return new_id(prefix)

        return await self._ctx.run_typed(
            f"mint:{prefix}:{self._next_step()}:{label}", _generate)
