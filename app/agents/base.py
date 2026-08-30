"""Agent registry and the agent-facing context.

An agent implementation defines *only domain behaviour*. Task IDs, durable
invocation, retries, artifact plumbing and event routing come from the kernel.

The LLM seam lives here rather than in the kernel on purpose: agents are the
only thing that reasons, and the coordination kernel must not be coupled to a
model choice (V1 PRD, "Technology").
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from app.kernel.artifacts import ArtifactRef, ArtifactStore
from app.kernel.models import Artifact, Task
from app.kernel.runtime import ApprovalDecision, Kernel

AgentHandler = Callable[["AgentContext", Task], Awaitable[Any]]

#: Keyed by ``(team, name)``. An unscoped agent registers under team ``""`` and
#: is reachable from any team, which is what keeps V1 working unchanged. Two
#: teams in one process can therefore both have a ``director`` without colliding.
_REGISTRY: dict[tuple[str, str], AgentHandler] = {}


@runtime_checkable
class LLMRunner(Protocol):
    """Whatever actually reasons. Swapped for a stub offline and in tests."""

    async def run(self, *, instructions: str, input: str,
                  agent: str = "") -> str: ...


class StubLLMRunner:
    """Deterministic stand-in used when no model is configured.

    Keeps the demo and the whole test suite runnable with no model endpoint,
    which is what makes the coordination kernel independently verifiable.
    """

    async def run(self, *, instructions: str, input: str, agent: str = "") -> str:
        head = input.strip().splitlines()[0] if input.strip() else "(no input)"
        return f"[{agent or 'agent'}] {head[:200]}"


def agent(name: str, *, team: str = "") -> Callable[[AgentHandler], AgentHandler]:
    """Register a domain handler under a logical agent name.

    ``team`` scopes the registration so two teams sharing a process can each
    have their own ``director`` or ``critic``.
    """
    def decorator(fn: AgentHandler) -> AgentHandler:
        key = (team, name)
        if key in _REGISTRY:
            scope = f" in team {team!r}" if team else ""
            raise ValueError(f"agent already registered: {name!r}{scope}")
        _REGISTRY[key] = fn
        return fn
    return decorator


def get_agent(name: str, team: str = "") -> AgentHandler:
    """Resolve an agent, preferring the team-scoped registration."""
    for key in ((team, name), ("", name)):
        if key in _REGISTRY:
            return _REGISTRY[key]
    known = sorted(f"{t}/{n}" if t else n for t, n in _REGISTRY)
    raise KeyError(f"unknown agent: {name!r}"
                   + (f" in team {team!r}" if team else "")
                   + f"; registered: {known}")


def registered_agents(team: str | None = None) -> list[str]:
    """Agent names. ``team=None`` lists every registration."""
    if team is None:
        return sorted({n for _, n in _REGISTRY})
    return sorted(n for t, n in _REGISTRY if t == team)


def clear_registry() -> None:
    """Test helper only."""
    _REGISTRY.clear()


class AgentContext:
    """What an agent handler is given. Never contains another agent's reasoning."""

    def __init__(self, *, kernel: Kernel, llm: LLMRunner | None = None,
                 prompts_dir: Path | None = None,
                 probes: dict[str, Callable[[], Awaitable[dict[str, Any]]]] | None = None) -> None:
        self._kernel = kernel
        self._llm = llm or StubLLMRunner()
        self._prompts = prompts_dir or Path(__file__).parent.parent / "prompts"
        self._probes = probes or {}

    @property
    def project_id(self) -> str:
        return self._kernel.project_id

    @property
    def artifacts(self) -> ArtifactStore:
        return self._kernel.artifacts

    @property
    def llm(self) -> LLMRunner:
        return self._llm

    async def send(self, agent: str, task: str, **kwargs: Any) -> Task:
        return await self._kernel.send(agent=agent, task=task, **kwargs)

    async def publish(self, topic: str, payload: dict[str, Any] | None = None,
                      **kwargs: Any):
        return await self._kernel.publish(topic=topic, payload=payload, **kwargs)

    async def wake_later(self, delay: timedelta, *, agent: str | None = None,
                         reason: str = "", **kwargs: Any) -> Task:
        return await self._kernel.wake_later(
            agent=agent or "monitor", delay=delay, reason=reason, **kwargs)

    async def request_approval(self, task: Task, prompt: str) -> ApprovalDecision:
        return await self._kernel.request_approval(task=task, prompt=prompt)

    async def put_artifact(self, *, task: Task, content: Any,
                           type: str = "markdown", created_by: str = "") -> ArtifactRef:
        """Write an artifact AND register its metadata.

        Registration is not optional: the context builder denies by default, so
        an artifact that exists only on disk cannot be passed as an input ref.
        """
        author = created_by or task.assigned_agent
        # Minted through the journal: this id travels in the published event's
        # payload, so a replay that invented a new one would diverge from the
        # journalled send and Restate would fail the invocation.
        artifact_id = await self._kernel.mint("art", f"artifact:{task.id}:{type}")
        ref = await self.artifacts.put(
            project_id=self.project_id, task_id=task.id, created_by=author,
            content=content, type=type, artifact_id=artifact_id)

        path_for = getattr(self.artifacts, "path_for", None)
        path = path_for(ref.id) if path_for else None
        self._kernel.repository.save_artifact(Artifact(
            id=ref.id, project_id=self.project_id, task_id=task.id, type=type,
            path=str(path) if path else "", created_by=author))
        return ref

    async def probe(self, name: str) -> dict[str, Any]:
        """A cheap deterministic external check. No LLM involved."""
        fn = self._probes.get(name)
        return await fn() if fn else {"changed": False}

    async def project_tasks(self) -> list[Task]:
        """Selected small project state — not a transcript.

        Journalled: the result steers control flow, so a replay must see the
        same answer even if the table has moved on. The journal is JSON, so the
        step returns plain data and the models are rebuilt outside it.
        """
        async def _read() -> list[dict[str, Any]]:
            return [t.model_dump(mode="json")
                    for t in self._kernel.repository.list_tasks(self.project_id)]

        return [Task(**row) for row in await self._kernel.step("read:tasks", _read)]

    async def project_artifacts(self) -> list[Artifact]:
        """Journalled for the same reason as :meth:`project_tasks`."""
        async def _read() -> list[dict[str, Any]]:
            return [a.model_dump(mode="json")
                    for a in self._kernel.repository.list_artifacts(self.project_id)]

        return [Artifact(**row)
                for row in await self._kernel.step("read:artifacts", _read)]

    def record_external_artifact(self, *, task: Task, artifact_id: str,
                                 type: str, source: str) -> Artifact:
        """Register a reference to an artifact owned by another team.

        Only the reference is recorded — the bytes stay with the team that
        produced them. That is what keeps a bus message small and keeps each
        team's artifact store its own. Fetching a foreign body would need an
        explicit cross-team artifact read, which V2 does not yet provide.
        """
        return self._kernel.repository.save_artifact(Artifact(
            id=artifact_id, project_id=self.project_id, task_id=task.id,
            type=type, path="", created_by=source,
            meta={"external": True, "source": source}))

    def prompt(self, name: str) -> str:
        path = self._prompts / f"{name}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else f"You are the {name} agent."


async def dispatch(name: str, ctx: AgentContext, task: Task,
                   team: str = "") -> Any:
    """Entry point the Restate handler calls."""
    return await get_agent(name, team)(ctx, task)
