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
        # Imported lazily and per-invocation on purpose: the agent registry is a
        # separate module, and this one must stay importable (for the protocol
        # conformance tests) whether or not the registry exists yet.
        try:
            from app.agents.base import dispatch
        except ImportError as exc:
            raise RuntimeError(
                "Cannot dispatch to agent "
                f"{agent_name!r}: importing `dispatch` from app.agents.base "
                f"failed ({exc}). app/runtime/durable.py only wires Restate to "
                "the agent registry; the registry itself must expose "
                "`async def dispatch(*, agent: str, ctx: KernelContext, "
                "payload: dict) -> dict`."
            ) from exc

        return await dispatch(
            agent=agent_name,
            ctx=RestateKernelContext(ctx),
            payload=payload,
        )

    return obj


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
