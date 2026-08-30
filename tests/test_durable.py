"""Unit tests for the Restate wiring.

These run with **no Docker, no Restate server and no Restate SDK installed**.
That is the point of the ``KernelContext`` seam: the adapter is checked against
the protocol structurally, and against a stub ``ObjectContext`` that records
exactly what the real SDK would have been asked to do.

Anything that needs a live Restate server is marked ``@pytest.mark.integration``
and is excluded by the default pytest args in ``pyproject.toml``.
"""

import inspect
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

import app.kernel as kernel_pkg
from app.kernel.context import FakeKernelContext, KernelContext
from app.runtime import durable
from app.runtime.durable import (
    AGENT_NAMES,
    HANDLER_NAME,
    RestateKernelContext,
    agent_objects,
    build_agent_object,
    create_app,
    decode_payload,
    encode_payload,
)

PROTOCOL_MEMBERS = ("key", "send", "awakeable", "run_typed", "sleep")


# --------------------------------------------------------------------- stubs


class StubSendHandle:
    """Stand-in for the ``SendHandle`` ``generic_send`` returns."""


class StubObjectContext:
    """Records what the real ``restate.ObjectContext`` would have been told.

    Mirrors the SDK signatures verified Aug 2026 — argument names matter,
    because the adapter passes them as keywords.
    """

    def __init__(self, key: str = "proj_1") -> None:
        self._key = key
        self.sends: list[dict] = []
        self.runs: list[tuple[str, dict]] = []
        self.sleeps: list[timedelta] = []
        self.awakeable_type_hints: list[object] = []

    def key(self) -> str:
        return self._key

    def generic_send(self, service, handler, arg, key=None, send_delay=None,
                     idempotency_key=None, **extra):
        self.sends.append({
            "service": service,
            "handler": handler,
            "arg": arg,
            "key": key,
            "send_delay": send_delay,
            "idempotency_key": idempotency_key,
            "extra": extra,
        })
        return StubSendHandle()

    def awakeable(self, serde=None, type_hint=None):
        self.awakeable_type_hints.append(type_hint)
        return "awk_stub", self._resolved({"decision": "approve"})

    def run_typed(self, name, action, *args, **kwargs):
        self.runs.append((name, dict(kwargs)))
        return self._invoke(action, *args, **kwargs)

    def sleep(self, delta, name=None):
        self.sleeps.append(delta)
        return self._resolved(None)

    async def _resolved(self, value):
        return value

    async def _invoke(self, action, *args, **kwargs):
        result = action(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


@pytest.fixture
def stub() -> StubObjectContext:
    return StubObjectContext()


@pytest.fixture
def ctx(stub: StubObjectContext) -> RestateKernelContext:
    return RestateKernelContext(stub)


# ------------------------------------------------------- protocol conformance


def test_restate_kernel_context_satisfies_kernel_context(ctx):
    """The adapter is a ``KernelContext`` structurally, without inheriting it."""
    assert isinstance(ctx, KernelContext)
    assert KernelContext not in type(ctx).__mro__


def test_adapter_and_fake_expose_the_same_protocol_surface(ctx):
    """Both implementations satisfy the same protocol, so tests can swap them."""
    assert isinstance(FakeKernelContext(), KernelContext)
    for member in PROTOCOL_MEMBERS:
        assert hasattr(ctx, member), member


def _parameter_shape(func):
    """Names, kinds and defaults — annotations are compared separately.

    ``app/kernel/context.py`` uses ``from __future__ import annotations`` and
    this module deliberately does not, so the raw annotation objects differ in
    representation while describing the same contract.
    """
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(func).parameters.values()
    ]


@pytest.mark.parametrize("name", ["send", "run_typed", "sleep", "awakeable"])
def test_adapter_signatures_match_the_protocol(name):
    """Signature drift here would silently break every kernel primitive."""
    assert (_parameter_shape(getattr(RestateKernelContext, name))
            == _parameter_shape(getattr(KernelContext, name)))


def test_key_is_a_property_adapting_restates_method(stub, ctx):
    """Restate exposes ``key()``; the kernel protocol wants an attribute."""
    assert isinstance(type(ctx).key, property)
    assert ctx.key == "proj_1"
    assert callable(stub.key)


# ------------------------------------------------------------- payload codec


def test_encode_payload_produces_deterministic_json_bytes():
    encoded = encode_payload({"b": 2, "a": 1})
    assert isinstance(encoded, bytes)
    assert encoded == b'{"a":1,"b":2}'
    assert encode_payload({"a": 1, "b": 2}) == encoded


def test_payload_round_trips():
    payload = {"task_id": "task_1", "objective": "price it", "refs": ["art_1"]}
    assert decode_payload(encode_payload(payload)) == payload


def test_decode_payload_treats_an_empty_body_as_an_empty_payload():
    assert decode_payload(b"") == {}
    assert decode_payload(None) == {}


# ---------------------------------------------------------------------- send


def test_send_serializes_payload_to_json_bytes(stub, ctx):
    ctx.send(agent="finance", handler=HANDLER_NAME, key="proj_1",
             payload={"task_id": "task_7", "objective": "price it"})

    (sent,) = stub.sends
    assert isinstance(sent["arg"], bytes)
    assert json.loads(sent["arg"].decode("utf-8")) == {
        "task_id": "task_7", "objective": "price it",
    }


def test_send_targets_the_object_named_after_the_agent(stub, ctx):
    """D2: object name == agent name, so runtime strings can address it."""
    ctx.send(agent="critic", handler=HANDLER_NAME, key="proj_9", payload={})

    (sent,) = stub.sends
    assert sent["service"] == "critic"
    assert sent["handler"] == "handle"
    assert sent["key"] == "proj_9"


def test_send_passes_delay_through_as_send_delay(stub, ctx):
    """``wake_later()`` is Restate's timer; Muster owns no timer code."""
    ctx.send(agent="monitor", handler=HANDLER_NAME, key="proj_1",
             payload={"check": "budget"}, delay=timedelta(hours=6))

    (sent,) = stub.sends
    assert sent["send_delay"] == timedelta(hours=6)


def test_send_omits_delay_when_none(stub, ctx):
    ctx.send(agent="research", handler=HANDLER_NAME, key="proj_1", payload={})
    assert stub.sends[0]["send_delay"] is None


def test_send_passes_idempotency_key_through(stub, ctx):
    ctx.send(agent="research", handler=HANDLER_NAME, key="proj_1",
             payload={}, idempotency_key="effect_42")

    assert stub.sends[0]["idempotency_key"] == "effect_42"


def test_send_returns_none_so_callers_cannot_hold_a_restate_handle(stub, ctx):
    """A ``SendHandle`` escaping here would leak an SDK type into the kernel."""
    assert ctx.send(agent="director", handler=HANDLER_NAME, key="proj_1",
                    payload={}) is None


def test_send_is_fire_and_forget_not_a_request_response(stub, ctx):
    """Only ``generic_send`` may be used — never ``generic_call``."""
    assert not hasattr(stub, "generic_call")
    ctx.send(agent="director", handler=HANDLER_NAME, key="proj_1", payload={})
    assert len(stub.sends) == 1


# ------------------------------------------------- awakeable / run_typed / sleep


async def test_awakeable_returns_an_id_and_an_awaitable(stub, ctx):
    awakeable_id, future = ctx.awakeable()

    assert awakeable_id == "awk_stub"
    assert isinstance(awakeable_id, str)  # D3: persisted on the run record
    assert await future == {"decision": "approve"}


async def test_awakeable_asks_for_a_dict_type_hint(stub, ctx):
    _, future = ctx.awakeable()
    await future

    assert stub.awakeable_type_hints == [dict]


async def test_run_typed_forwards_name_and_kwargs_and_awaits_the_result(stub, ctx):
    async def price(*, amount: int) -> int:
        return amount * 2

    result = await ctx.run_typed("price", price, amount=21)

    assert result == 42
    assert stub.runs == [("price", {"amount": 21})]


async def test_sleep_forwards_the_delta_and_returns_none(stub, ctx):
    assert await ctx.sleep(timedelta(minutes=5)) is None
    assert stub.sleeps == [timedelta(minutes=5)]


# ------------------------------------------------------- agent object wiring


def test_agent_names_are_the_five_v1_agents():
    assert AGENT_NAMES == ("director", "research", "finance", "critic", "monitor")


def test_handler_name_is_part_of_the_wire_contract():
    assert HANDLER_NAME == "handle"


# ------------------------------------------------- optional-dependency guard


def test_module_imports_without_the_restate_sdk():
    """Importing the wiring must never require the optional extra."""
    assert durable.__name__ == "app.runtime.durable"


@pytest.mark.skipif(durable.restate is not None,
                    reason="restate SDK is installed; the guard cannot trigger")
@pytest.mark.parametrize("factory", [
    pytest.param(lambda: build_agent_object("research"), id="build_agent_object"),
    pytest.param(agent_objects, id="agent_objects"),
    pytest.param(create_app, id="create_app"),
])
def test_factories_explain_how_to_install_the_sdk(factory):
    with pytest.raises(RuntimeError) as excinfo:
        factory()

    message = str(excinfo.value)
    assert "uv sync --extra durable" in message
    assert "not installed" in message


# ------------------------------------------------------ no SDK types leak out


IMPORTS_RESTATE = re.compile(r"^\s*(?:import\s+restate|from\s+restate[.\s])",
                             re.MULTILINE)


def test_no_kernel_module_imports_the_restate_sdk():
    """CLAUDE.md: agent code calls the kernel, never Restate directly.

    Prose mentions of Restate in docstrings are fine and expected; an actual
    import is what would drag SDK types into the public surface.
    """
    offenders = [
        path.name
        for path in sorted(Path(kernel_pkg.__file__).parent.glob("*.py"))
        if IMPORTS_RESTATE.search(path.read_text())
    ]
    assert offenders == []


def test_no_restate_type_in_any_public_adapter_signature():
    """``__init__`` takes the SDK context — that is the adapter's whole job.

    Every *other* public member is kernel-facing and must stay SDK-free.
    """
    for name in PROTOCOL_MEMBERS:
        member = inspect.getattr_static(RestateKernelContext, name)
        func = member.fget if isinstance(member, property) else member
        annotations = dict(getattr(func, "__annotations__", {}))
        rendered = " ".join(str(value) for value in annotations.values())
        assert "restate" not in rendered.lower(), f"{name}: {rendered}"


def test_the_adapter_constructor_is_the_only_sdk_boundary():
    hint = RestateKernelContext.__init__.__annotations__["ctx"]
    assert hint == "restate.ObjectContext"


# ------------------------------------------------------------- integration


@pytest.mark.integration
def test_agent_objects_build_against_the_real_sdk():
    """Needs `uv sync --extra durable`; no server required to construct them."""
    objects = agent_objects()
    assert len(objects) == len(AGENT_NAMES)
    assert [obj.name for obj in objects] == list(AGENT_NAMES)


@pytest.mark.integration
def test_create_app_registers_every_agent():
    """Needs `uv sync --extra durable`."""
    assert create_app() is not None
