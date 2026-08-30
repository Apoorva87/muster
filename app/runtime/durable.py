"""Restate wiring — the real-SDK half of the ``KernelContext`` seam.

D1 collapsed ``app/runtime/restate.py`` and ``app/runtime/llm.py`` into this one
module. With Restate's Pydantic AI integration providing durable LLM execution,
what is left is configuration, not a subsystem.

Two things live here and nothing else:

* :class:`RestateKernelContext` — adapts ``restate.ObjectContext`` to the
  ``KernelContext`` protocol in ``app/kernel/context.py``. This is the *only*
  module in Muster allowed to touch the Restate SDK.
* :func:`build_agent_object` / :func:`agent_objects` — D2's registration: one
  Virtual Object type **per agent**, each keyed by ``project_id``.

Object name == agent name
-------------------------
``send(agent="research", ...)`` becomes
``ctx.generic_send("research", "handle", key=project_id, arg=...)``.
Subscriber names arrive from the subscriptions table as runtime strings, so the
identity mapping is what keeps ``publish()`` fan-out expressible at all (D2).
The typed ``object_send`` needs a compile-time handler reference and cannot.

Optional dependency
-------------------
The Restate SDK is an optional extra (``uv sync --extra durable``) so the unit
suite runs with no Docker, no Restate and no Postgres. Importing this module
therefore always succeeds; the factory functions raise a clear, actionable
error when the SDK is absent.

API verified against the Restate Python SDK, Aug 2026:
``generic_send(service, handler, arg, key=None, send_delay=None,
idempotency_key=None, ...)``, ``awakeable(serde, type_hint)``,
``run_typed(name, action, options, /, *args, **kwargs)``,
``sleep(delta, name=None)``, ``ObjectContext.key()``.

NOTE: this module deliberately does **not** use ``from __future__ import
annotations``. The SDK reads handler type hints to pick a serde, so the
annotations on the object handlers must stay real objects, not strings.
"""

import json
from datetime import timedelta
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # pragma: no cover - exercised by whichever extra is installed
    import restate
except ImportError:  # pragma: no cover
    restate = None  # type: ignore[assignment]


#: V1 ensemble. One Virtual Object type is registered per name (D2).
AGENT_NAMES: tuple[str, ...] = (
    "director",
    "research",
    "finance",
    "critic",
    "monitor",
)

#: Every agent object exposes exactly one handler. The kernel addresses it by
#: this string, so it is part of the wire contract, not an implementation
#: detail.
HANDLER_NAME = "handle"

_SDK_MISSING = (
    "The Restate SDK is not installed. Muster keeps it an optional extra so the "
    "unit suite runs with no Docker, no Restate and no Postgres.\n"
    "Install the durable stack with:  uv sync --extra durable\n"
    "(equivalently: uv add 'restate-sdk[serde]' pydantic-ai)"
)


def _require_restate() -> Any:
    """Return the Restate SDK module or explain how to get it."""
    if restate is None:
        raise RuntimeError(_SDK_MISSING)
    return restate


def encode_payload(payload: dict[str, Any]) -> bytes:
    """Kernel payload -> the JSON bytes ``generic_send`` puts on the wire.

    Keys are sorted so the same logical payload always produces the same bytes;
    replay and idempotency comparisons stay stable.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_payload(arg: bytes | None) -> dict[str, Any]:
    """Inverse of :func:`encode_payload`. An empty body is an empty payload."""
    if not arg:
        return {}
    return json.loads(arg.decode("utf-8"))


class RestateKernelContext:
    """Adapts ``restate.ObjectContext`` to the ``KernelContext`` protocol.

    Structural, not nominal: nothing here inherits from ``KernelContext``, and
    no Restate type appears in any method signature. That is what lets kernel
    code — and V2's ``BusAdapter`` — stay ignorant of Restate.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: "restate.ObjectContext") -> None:
        self._ctx = ctx

    @property
    def key(self) -> str:
        """The Virtual Object key: the ``project_id`` this invocation is for.

        Restate exposes it as a method; the kernel protocol wants an attribute.
        Adapting that is this class's whole job.
        """
        return self._ctx.key()

    def send(self, *, agent: str, handler: str, key: str,
             payload: dict[str, Any], delay: timedelta | None = None,
             idempotency_key: str | None = None) -> None:
        """Fire-and-forget durable invocation of another agent's object.

        Runtime string dispatch (``generic_send``) is mandatory here: agent
        names come from the subscriptions table, so there is no compile-time
        handler reference to hand to the typed ``object_send``. ``delay`` maps
        onto Restate's ``send_delay``, which is also how ``wake_later()`` gets
        its durable timer — Muster owns no timer code.
        """
        self._ctx.generic_send(
            agent,
            handler,
            arg=encode_payload(payload),
            key=key,
            send_delay=delay,
            idempotency_key=idempotency_key,
        )

    def awakeable(self) -> tuple[str, Awaitable[Any]]:
        """A durable promise for the human pause/resume path.

        The returned ID is persisted on the run record from the first migration
        (D3) — without it the Approve button cannot resume the workflow.
        """
        awakeable_id, future = self._ctx.awakeable(type_hint=dict)
        return awakeable_id, future

    async def run_typed(self, name: str, fn: Callable[..., Awaitable[Any]],
                        /, **kwargs: Any) -> Any:
        """Execute a side effect exactly once across replays."""
        return await self._ctx.run_typed(name, fn, **kwargs)

    async def sleep(self, delta: timedelta) -> None:
        """Durable sleep. Consumes no model tokens while parked."""
        await self._ctx.sleep(delta)


def build_agent_object(agent_name: str) -> Any:
    """Build the Virtual Object type for one agent, keyed by ``project_id``.

    Returns a configured ``restate.VirtualObject`` with a single ``handle``
    handler. Distinct object types per agent keep different agents running in
    parallel for the same project, while two events for the *same* agent and
    project still serialize (D2).
    """
    sdk = _require_restate()
    obj = sdk.VirtualObject(agent_name)

    @obj.handler(name=HANDLER_NAME)
    async def handle(ctx: "restate.ObjectContext", payload: dict) -> dict:
        # Imported lazily and per-invocation on purpose: this module must stay
        # importable for the protocol conformance tests whether or not the
        # agent registry has been loaded.
        from app.agents.base import AgentContext, dispatch
        from app.kernel.models import Task
        from app.kernel.runtime import Kernel

        deps = team_deps()
        project_id = ctx.key()

        task = deps.task_from(payload, project_id=project_id, agent=agent_name)

        kernel = Kernel(
            ctx=RestateKernelContext(ctx),
            repository=deps.repository,
            subscriptions=deps.subscriptions,
            artifacts=deps.artifacts,
            project_id=project_id,
            team_id=deps.team_id,
            public_topics=deps.public_topics,
        )
        agent_ctx = AgentContext(kernel=kernel, llm=deps.llm_for(agent_name),
                                 prompts_dir=deps.prompts_dir)

        result = await dispatch(agent_name, agent_ctx, task, team=deps.team_id)
        return result if isinstance(result, dict) else {"result": result}

    return obj


@dataclass
class TeamDeps:
    """Everything a handler needs, built once per process.

    Restate invokes handlers concurrently, so this must be cheap to reuse and
    must not hold per-invocation state — the ``KernelContext`` carries that.
    """

    team_id: str
    repository: Any
    subscriptions: Any
    artifacts: Any
    prompts_dir: Path
    public_topics: tuple[str, ...]
    spec: Any
    _llm: Any

    def llm_for(self, agent: str):
        provider, model = self.spec.llm_for(agent)
        return self._llm.for_agent(provider, model)

    @staticmethod
    def task_from(payload: dict, *, project_id: str, agent: str):
        """Rebuild this team's own bounded task from the invocation payload.

        A team never inherits a sender's task row. Handles both shapes: a
        team-local send, and a bus envelope from another team.
        """
        from app.kernel.ids import new_id
        from app.kernel.models import Task

        if "kind" in payload:                      # a bus envelope
            inner = payload.get("payload") or {}
            topic = payload.get("topic")
            return Task(
                # The MESSAGE id — a team mints its own task rather than
                # reusing the sender's, which would collide with the sender's
                # own record. Origin survives in source/correlation_id.
                id=payload["id"],
                project_id=project_id,
                type=f"on:{topic}" if topic else inner.get("type", "handle"),
                objective=inner.get("objective") or f"react to {topic}",
                assigned_agent=agent,
                input_refs=payload.get("artifact_refs") or {},
                source=f"team://{payload.get('source_team')}/{payload.get('source_agent')}",
                correlation_id=payload.get("correlation_id"))

        return Task(
            id=payload.get("task_id") or new_id("task"),
            project_id=project_id,
            type=payload.get("type", "handle"),
            objective=payload.get("objective", ""),
            assigned_agent=agent,
            input_refs=payload.get("input_refs") or {})


_DEPS: TeamDeps | None = None


def team_deps(team_dir: str | None = None) -> TeamDeps:
    """Build (once) the repository, subscriptions, artifact store and models."""
    global _DEPS
    if _DEPS is not None:
        return _DEPS

    from app.config import load_settings
    from app.db.repository import Repository
    from app.kernel.artifacts import FilesystemArtifactStore
    from app.kernel.subscriptions import SubscriptionRegistry
    from app.kernel.team_spec import load_team_spec
    from app.runtime.llm import registry_from_settings

    settings = load_settings()
    directory = Path(team_dir or os.environ.get("MUSTER_TEAM_DIR", "teams/investment"))
    spec = load_team_spec(directory)
    spec.load_entrypoints()

    repository = Repository.from_url(settings.database_url)
    repository.init_schema()
    spec.seed_into(repository)

    _DEPS = TeamDeps(
        team_id=spec.team_id,
        repository=repository,
        subscriptions=SubscriptionRegistry(repository),
        artifacts=FilesystemArtifactStore(root=settings.artifact_root),
        prompts_dir=directory / "prompts",
        public_topics=tuple(spec.public.topics),
        spec=spec,
        _llm=registry_from_settings(settings),
    )
    return _DEPS


def reset_deps() -> None:
    """Test helper — drop the cached bundle."""
    global _DEPS
    _DEPS = None


def agent_objects(names: tuple[str, ...] = AGENT_NAMES) -> list[Any]:
    """One Virtual Object per V1 agent, in registration order."""
    return [build_agent_object(name) for name in names]


def create_app(names: tuple[str, ...] = AGENT_NAMES) -> Any:
    """The ASGI app Restate discovers.

    A factory rather than a module-level ``app`` so that importing this module
    without the SDK installed still succeeds. Serve it with::

        uvicorn --factory app.runtime.durable:create_app --port 9080

    Uvicorn is HTTP/1.1 only, so register the deployment with
    ``use_http_11: true`` (``make register`` does).
    """
    sdk = _require_restate()
    return sdk.app(services=agent_objects(names))
