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

_REGISTRY: dict[str, AgentHandler] = {}


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


def agent(name: str) -> Callable[[AgentHandler], AgentHandler]:
    """Register a domain handler under a logical agent name."""
    def decorator(fn: AgentHandler) -> AgentHandler:
        if name in _REGISTRY:
            raise ValueError(f"agent already registered: {name}")
        _REGISTRY[name] = fn
        return fn
    return decorator


def get_agent(name: str) -> AgentHandler:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown agent: {name!r}; registered: {sorted(_REGISTRY)}") from None


def registered_agents() -> list[str]:
    return sorted(_REGISTRY)


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
        ref = await self.artifacts.put(
            project_id=self.project_id, task_id=task.id,
            created_by=author, content=content, type=type)

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

    def project_tasks(self) -> list[Task]:
        """Selected small project state — not a transcript."""
        return self._kernel.repository.list_tasks(self.project_id)

    def project_artifacts(self):
        return self._kernel.repository.list_artifacts(self.project_id)

    def prompt(self, name: str) -> str:
        path = self._prompts / f"{name}.md"
        return path.read_text(encoding="utf-8") if path.is_file() else f"You are the {name} agent."


async def dispatch(name: str, ctx: AgentContext, task: Task) -> Any:
    """Entry point the Restate handler calls."""
    return await get_agent(name)(ctx, task)
