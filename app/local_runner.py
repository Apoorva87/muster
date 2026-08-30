"""Run a team in-process, with no Restate and no Docker.

This is the "see it work" path. It dispatches each durable send immediately
rather than handing it to Restate, so it is **not durable** — kill it and the
work is gone. Use it to watch the choreography, develop agents, and demo.

For the durable path, run `make dev` and invoke through the Restate ingress.
Same agents, same kernel, same team.yaml — only the context differs, which is
the whole point of the KernelContext seam.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.agents.base import AgentContext, LLMRunner, dispatch
from app.db.repository import Repository
from app.kernel.artifacts import FilesystemArtifactStore
from app.kernel.context import FakeKernelContext
from app.kernel.models import Task
from app.kernel.runtime import Kernel
from app.kernel.subscriptions import SubscriptionRegistry
from app.kernel.memory import MemoryStore
from app.kernel.team_spec import TeamSpec, load_team_spec
from app.memory import apply_permission, build_memory_store
from app.runtime.llm import LLMRegistry


class LocalRunner:
    """One team, executed in this process."""

    def __init__(self, team_dir: str | Path, *, repository: Repository,
                 artifact_root: Path, llm: LLMRunner | LLMRegistry | None = None,
                 bus: Any = None, session_id: str = "local",
                 project_id: str | None = None,
                 memory: MemoryStore | None = None,
                 memory_backend: str = "none",
                 recall_limit: int = 3) -> None:
        self.directory = Path(team_dir)
        self.spec: TeamSpec = load_team_spec(self.directory)
        self.spec.load_entrypoints()
        self.team_id = self.spec.team_id
        self.project_id = project_id or self.team_id

        self.repo = repository
        self.repo.init_schema()
        self.spec.seed_into(self.repo)

        self.store = FilesystemArtifactStore(root=artifact_root / self.team_id)
        self.ctx = FakeKernelContext(key=self.project_id)
        self.llm = llm if llm is not None else LLMRegistry(provider="stub")
        self.bus = bus
        self.session_id = session_id
        # Off by default: a team must behave exactly as it did in V3 unless
        # someone turns memory on.
        self.memory = memory or build_memory_store(
            backend=memory_backend, team_id=self.team_id,
            root=self.directory / "memory")
        self.recall_limit = recall_limit

    def kernel(self, ctx: FakeKernelContext | None = None) -> Kernel:
        return Kernel(ctx=ctx or self.ctx, repository=self.repo,
                      subscriptions=SubscriptionRegistry(self.repo),
                      artifacts=self.store, bus=self.bus,
                      team_id=self.team_id, session_id=self.session_id,
                      public_topics=self.spec.public.topics)

    def llm_for(self, agent: str) -> LLMRunner:
        """Resolve this agent's model, honouring any team.yaml override."""
        if not isinstance(self.llm, LLMRegistry):
            return self.llm
        provider, model = self.spec.llm_for(agent)
        return self.llm.for_agent(provider, model)

    def memory_for(self, agent: str) -> MemoryStore:
        """Narrow the team's memory to what this agent may do with it."""
        return apply_permission(self.memory, self.spec.memory_for(agent))

    def agent_context(self, agent: str = "") -> AgentContext:
        """One invocation, one journal — the same scoping Restate applies."""
        return AgentContext(kernel=self.kernel(self.ctx.invocation()),
                            llm=self.llm_for(agent),
                            prompts_dir=self.directory / "prompts",
                            memory=self.memory_for(agent),
                            recall_limit=self.recall_limit)

    def inbound_task(self, send) -> Task:
        """Reconstruct this team's own task from an incoming send.

        A team never inherits the sender's task row. Handles both shapes: a
        team-local send, and a bus envelope from another team.
        """
        payload = send.payload
        if "kind" in payload:                      # bus envelope
            inner = payload.get("payload") or {}
            topic = payload.get("topic")
            return Task(
                id=inner.get("task_id") or payload.get("task_id") or payload["id"],
                project_id=self.project_id,
                type=f"on:{topic}" if topic else inner.get("type", "handle"),
                objective=inner.get("objective") or f"react to {topic}",
                assigned_agent=send.agent,
                input_refs=payload.get("artifact_refs") or {},
                source=f"team://{payload['source_team']}/{payload['source_agent']}",
                correlation_id=payload.get("correlation_id"))
        return Task(
            id=payload["task_id"], project_id=self.project_id,
            type=payload.get("type", "handle"),
            objective=payload.get("objective", ""),
            assigned_agent=send.agent,
            input_refs=payload.get("input_refs", {}))

    def materialise(self, send) -> Task:
        task = self.inbound_task(send)
        return self.repo.get_task(task.id) or self.repo.save_task(task)


async def drive(runners: list[LocalRunner], *, auto_approve: str | None = "approve",
                timeout: float = 900.0, poll: float = 0.05) -> None:
    """Dispatch queued sends across every team until the system goes quiet.

    Waits on real in-flight work rather than spinning. A stubbed agent finishes
    within one event-loop tick, so a naive ``sleep(0)`` loop looks correct until
    a real model takes seconds — then it burns its rounds while work is still
    in flight and returns a half-finished project.

    ``auto_approve`` answers any workflow that parks on a human. Pass None to
    leave it parked, which is what you want when driving approvals from the UI.
    """
    seen: set[int] = set()
    inflight: list[asyncio.Task] = []
    deadline = time.monotonic() + timeout

    while True:
        progressed = False
        for runner in runners:
            for send in list(runner.ctx.sends):
                if id(send) in seen or send.delay is not None:
                    continue
                seen.add(id(send))
                task = runner.materialise(send)
                inflight.append(asyncio.create_task(
                    dispatch(send.agent, runner.agent_context(send.agent), task,
                             team=runner.team_id)))
                progressed = True

        if auto_approve is not None:
            for runner in runners:
                for run in runner.repo.list_waiting_runs(runner.project_id):
                    runner.ctx.resolve_awakeable(run.awakeable_id,
                                                 {"decision": auto_approve})

        pending = [t for t in inflight if not t.done()]
        if not progressed and not pending:
            break
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"team did not settle within {timeout}s; "
                f"{len(pending)} invocation(s) still running")

        if pending:
            # Wake on the first completion, but time out so a workflow parked on
            # a human is still noticed by the approval sweep above.
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED,
                               timeout=poll)
        else:
            await asyncio.sleep(0)

    if inflight:
        await asyncio.gather(*inflight)
