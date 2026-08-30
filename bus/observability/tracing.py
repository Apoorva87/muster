"""Cross-team tracing — W3C context propagation with optional OpenTelemetry.

V2 PRD, "Observability V2": *add OpenTelemetry instrumentation without requiring
a heavyweight local backend*. The acceptance criterion this module exists to
satisfy is criterion 10 — "trace/correlation IDs connect the cross-team path".

Three ideas, and nothing else:

1. :class:`TraceContext` is the eight-field carrier the PRD lists. It serialises
   to **W3C** ``traceparent`` / ``baggage`` headers, so a Muster trace is a real
   distributed trace the moment someone points it at Jaeger or Tempo — no
   bespoke header dialect to translate later.
2. :func:`inject` / :func:`extract` move that context across a team boundary via
   :class:`~bus.models.message.Message`. A message that already carries a
   ``trace_id`` *keeps* it: crossing a boundary continues a trace, it never
   starts one. Each hop gets a fresh ``span_id`` whose parent is the previous
   hop's span, which is what makes two hops render as one connected path.
3. :func:`span` records work. It emits a real OTel span when the library is
   installed and always appends to an in-memory buffer, so the behaviour under
   test is the same either way.

Optional dependency
-------------------
OpenTelemetry is an optional extra (``uv sync --extra otel``), following
``app/runtime/durable.py``. Unlike that module we do not raise when the SDK is
absent — tracing degrades to the in-memory recorder, because losing a trace must
never take down an agent run. The unit suite runs with no otel, no Docker and no
collector.

Default export is ``console``, which needs no backend running. ``otlp`` is opt-in
for users who later add Jaeger/Tempo/Grafana.

Muster ids stay authoritative
-----------------------------
When OTel is present, this module still generates and propagates its own ids and
stamps them onto the OTel span as ``muster.*`` attributes, rather than deferring
to the SDK's id generator. That keeps one id scheme across both paths — the
timeline, the run records, the bus messages and the tests all agree whether or
not the SDK is installed. Correlating a Muster trace with a vendor trace is then
an attribute join, which is a deliberate trade for determinism.
"""

from __future__ import annotations

import os
import secrets
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from types import ModuleType
from typing import TYPE_CHECKING, Any, Iterator, Mapping, MutableMapping
from urllib.parse import quote, unquote

from app.kernel.models import RunRecord
from bus.models.message import Message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.db.repository import Repository

# --------------------------------------------------------------- optional dep

try:  # pragma: no cover - exercised by whichever extra is installed
    from opentelemetry import trace as otel_trace
except ImportError:  # pragma: no cover
    otel_trace = None  # type: ignore[assignment]

try:  # pragma: no cover
    from opentelemetry.sdk import trace as otel_sdk_trace
    from opentelemetry.sdk.trace import export as otel_export
except ImportError:  # pragma: no cover
    otel_sdk_trace = None  # type: ignore[assignment]
    otel_export = None  # type: ignore[assignment]


def otel_available() -> bool:
    """True when the OpenTelemetry API is importable.

    Callers use this to skip assertions that only hold on the SDK path; the
    module itself works either way.
    """
    return otel_trace is not None


def otel_sdk_available() -> bool:
    """True when the OTel *SDK* (providers and exporters) is importable."""
    return otel_sdk_trace is not None and otel_export is not None


def _otlp_exporter_class() -> type | None:
    """The OTLP span exporter, if its (separately distributed) package is here.

    Imported lazily: ``opentelemetry-exporter-otlp`` ships apart from the SDK,
    so its absence must not make ``console`` export unavailable.
    """
    for module_name in (
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    ):
        try:  # pragma: no cover - depends on installed extras
            module: ModuleType = __import__(module_name, fromlist=["OTLPSpanExporter"])
        except ImportError:  # pragma: no cover
            continue
        return getattr(module, "OTLPSpanExporter", None)
    return None  # pragma: no cover


# ------------------------------------------------------------------ W3C ids

#: Only version ``00`` of the traceparent format is specified today.
TRACEPARENT_VERSION = "00"
TRACEPARENT_HEADER = "traceparent"
BAGGAGE_HEADER = "baggage"

#: Baggage is a shared namespace, so Muster's own keys are prefixed.
BAGGAGE_PREFIX = "muster."

_INVALID_TRACE_ID = "0" * 32
_INVALID_SPAN_ID = "0" * 16

#: The ``baggage`` members that carry the PRD's non-W3C fields, in header order.
BAGGAGE_FIELDS: tuple[str, ...] = (
    "session_id",
    "team_id",
    "agent_id",
    "project_id",
    "task_id",
    "message_id",
)


def new_trace_id() -> str:
    """A fresh W3C trace id: 32 lowercase hex characters, never all-zero."""
    while (value := secrets.token_hex(16)) == _INVALID_TRACE_ID:  # pragma: no cover
        continue
    return value


def new_span_id() -> str:
    """A fresh W3C span id: 16 lowercase hex characters, never all-zero."""
    while (value := secrets.token_hex(8)) == _INVALID_SPAN_ID:  # pragma: no cover
        continue
    return value


def _is_hex(value: str, length: int) -> bool:
    if len(value) != length:
        return False
    return all(c in "0123456789abcdef" for c in value)


def is_valid_trace_id(value: str | None) -> bool:
    return bool(value) and _is_hex(value, 32) and value != _INVALID_TRACE_ID  # type: ignore[arg-type]


def is_valid_span_id(value: str | None) -> bool:
    return bool(value) and _is_hex(value, 16) and value != _INVALID_SPAN_ID  # type: ignore[arg-type]


# -------------------------------------------------------------- TraceContext


@dataclass(frozen=True)
class TraceContext:
    """The eight propagated fields, plus the parent link that connects hops.

    Frozen because a context is a value: a new hop produces a *new* context via
    :meth:`child`, it never mutates the caller's. ``trace_id`` and ``span_id``
    are generated when omitted, so ``TraceContext()`` is already a valid,
    exportable, fresh trace.
    """

    trace_id: str = field(default_factory=new_trace_id)
    span_id: str = field(default_factory=new_span_id)
    session_id: str | None = None
    team_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    message_id: str | None = None
    #: The span this one continues from. Not a PRD field; it is what makes a
    #: two-hop path a tree rather than eight unrelated spans.
    parent_span_id: str | None = None
    sampled: bool = True

    def __post_init__(self) -> None:
        # Tolerate a caller passing "" or a malformed id rather than failing an
        # agent run over telemetry: repair it into something exportable.
        if not is_valid_trace_id(self.trace_id):
            object.__setattr__(self, "trace_id", new_trace_id())
        if not is_valid_span_id(self.span_id):
            object.__setattr__(self, "span_id", new_span_id())

    # ------------------------------------------------------------- derivation

    def child(self, **overrides: Any) -> "TraceContext":
        """A new span in the *same* trace, parented to this one.

        This is the only sanctioned way to advance a trace. ``trace_id`` is
        carried through untouched; passing it in ``overrides`` is refused, since
        that would silently fork the trace and break the cross-team path.
        """
        if "trace_id" in overrides:
            raise ValueError(
                "child() may not change trace_id — a child span stays in its "
                "parent's trace. Build a new TraceContext to start a new trace."
            )
        overrides.setdefault("span_id", new_span_id())
        overrides.setdefault("parent_span_id", self.span_id)
        return replace(self, **overrides)

    # ----------------------------------------------------------------- W3C

    @property
    def trace_flags(self) -> str:
        return "01" if self.sampled else "00"

    @property
    def traceparent(self) -> str:
        """``00-<32 hex trace id>-<16 hex span id>-<2 hex flags>``."""
        return (
            f"{TRACEPARENT_VERSION}-{self.trace_id}-{self.span_id}-{self.trace_flags}"
        )

    @property
    def baggage(self) -> str:
        """The W3C ``baggage`` member list for the six non-W3C fields."""
        members = [
            f"{BAGGAGE_PREFIX}{name}={quote(str(value), safe='')}"
            for name in BAGGAGE_FIELDS
            if (value := getattr(self, name)) is not None
        ]
        return ",".join(members)

    def to_headers(self) -> dict[str, str]:
        """W3C headers carrying all eight fields.

        ``traceparent`` carries ``trace_id``/``span_id``; the other six ride in
        ``baggage``. Both are standard, so an OTel-instrumented peer that knows
        nothing about Muster still joins the trace. ``baggage`` is omitted when
        empty rather than sent blank — an empty member list is not valid.
        """
        headers = {TRACEPARENT_HEADER: self.traceparent}
        if baggage := self.baggage:
            headers[BAGGAGE_HEADER] = baggage
        return headers

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "TraceContext":
        """Parse W3C headers back into a context.

        Lookup is case-insensitive (HTTP header names are). A missing or
        malformed ``traceparent`` yields a *fresh* context rather than an error:
        an unparseable upstream header means we start a trace here, not that the
        request fails.
        """
        lowered = {str(k).lower(): v for k, v in headers.items()}
        trace_id, span_id, sampled = _parse_traceparent(lowered.get(TRACEPARENT_HEADER))
        fields = _parse_baggage(lowered.get(BAGGAGE_HEADER))
        return cls(
            trace_id=trace_id or new_trace_id(),
            span_id=span_id or new_span_id(),
            sampled=sampled,
            **fields,
        )


def _parse_traceparent(raw: str | None) -> tuple[str | None, str | None, bool]:
    """Return ``(trace_id, span_id, sampled)``; ids are None when unusable."""
    if not raw:
        return None, None, True
    parts = raw.strip().split("-")
    if len(parts) < 4:
        return None, None, True
    version, trace_id, span_id, flags = parts[0], parts[1], parts[2], parts[3]
    if not _is_hex(version, 2) or version == "ff":
        return None, None, True
    if not is_valid_trace_id(trace_id) or not is_valid_span_id(span_id):
        return None, None, True
    sampled = _is_hex(flags, 2) and bool(int(flags, 16) & 0x01)
    return trace_id, span_id, sampled


def _parse_baggage(raw: str | None) -> dict[str, str]:
    """Pull the ``muster.*`` members out of a W3C baggage header."""
    if not raw:
        return {}
    known = set(BAGGAGE_FIELDS)
    out: dict[str, str] = {}
    for member in raw.split(","):
        key, sep, value = member.strip().partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key.startswith(BAGGAGE_PREFIX):
            continue  # someone else's baggage; carry-through is not our job
        name = key[len(BAGGAGE_PREFIX):]
        if name in known:
            # Baggage values may carry ";properties"; Muster emits none.
            out[name] = unquote(value.split(";", 1)[0].strip())
    return out


# ------------------------------------------------------- ambient current span

_CURRENT: ContextVar[TraceContext | None] = ContextVar("muster_trace_context", default=None)


def current_context() -> TraceContext | None:
    """The context bound by the innermost :func:`span` / :func:`use_context`."""
    return _CURRENT.get()


@contextmanager
def use_context(ctx: TraceContext) -> Iterator[TraceContext]:
    """Bind ``ctx`` as the ambient context for the duration of the block."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


# ------------------------------------------------------------------ recording


@dataclass
class RecordedSpan:
    """One finished span in the in-memory buffer."""

    name: str
    context: TraceContext
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    @property
    def trace_id(self) -> str:
        return self.context.trace_id

    @property
    def span_id(self) -> str:
        return self.context.span_id

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds() * 1000))


#: Bounded so a long-lived process cannot leak memory through telemetry.
MAX_RECORDED_SPANS = 1000

_RECORDED: list[RecordedSpan] = []


def recorded_spans() -> list[RecordedSpan]:
    """A snapshot of the in-memory buffer, in the order spans *finished*.

    Finish order, not start order — the same order a real span processor sees
    them, so a nested inner span appears before its parent.
    """
    return list(_RECORDED)


def reset_recorded_spans() -> None:
    """Drop everything recorded so far. Tests call this; production need not."""
    _RECORDED.clear()


def _record(entry: RecordedSpan) -> None:
    _RECORDED.append(entry)
    if len(_RECORDED) > MAX_RECORDED_SPANS:
        del _RECORDED[:-MAX_RECORDED_SPANS]


def _otel_attributes(ctx: TraceContext, attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Muster's ids as span attributes, plus whatever the caller passed."""
    out: dict[str, Any] = {
        f"muster.{name}": value
        for name in ("trace_id", "span_id", *BAGGAGE_FIELDS)
        if (value := getattr(ctx, name)) is not None
    }
    for key, value in attributes.items():
        out[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
    return out


@contextmanager
def span(name: str, /, *, parent: TraceContext | None = None,
         **attributes: Any) -> Iterator[TraceContext]:
    """Record a unit of work, yielding the :class:`TraceContext` for that span.

    The yielded context is what you hand to :func:`inject` when the work sends a
    message onward, so the receiving team's spans hang off this one.

    Always appends to the in-memory buffer; additionally emits a real OTel span
    when the API is installed. ``parent`` is a reserved keyword — every other
    keyword becomes a span attribute.
    """
    base = parent if parent is not None else current_context()
    ctx = base.child() if base is not None else TraceContext()

    entry = RecordedSpan(name=name, context=ctx, attributes=dict(attributes))
    token = _CURRENT.set(ctx)

    if otel_trace is None:
        try:
            yield ctx
        except BaseException as exc:
            entry.status = "ERROR"
            entry.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry.ended_at = datetime.now(timezone.utc)
            _record(entry)
            _CURRENT.reset(token)
        return

    tracer = _tracer()
    try:
        with tracer.start_as_current_span(
            name, attributes=_otel_attributes(ctx, attributes)
        ) as otel_span:
            try:
                yield ctx
            except BaseException as exc:
                entry.status = "ERROR"
                entry.error = f"{type(exc).__name__}: {exc}"
                otel_span.record_exception(exc)
                otel_span.set_status(
                    otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc))
                )
                raise
    finally:
        entry.ended_at = datetime.now(timezone.utc)
        _record(entry)
        _CURRENT.reset(token)


# ------------------------------------------------------- message propagation


def inject(message: Message, ctx: TraceContext) -> Message:
    """Stamp ``ctx`` onto an outbound message. Returns a new ``Message``.

    A message that already carries a ``trace_id`` **keeps** it. Crossing a team
    boundary continues whatever trace the work already belongs to; re-stamping
    would sever the cross-team path, which is exactly the thing V2 asks us to
    keep intact. ``project_id`` / ``task_id`` are backfilled from the context
    only when the message has none.
    """
    updates: dict[str, Any] = {
        "trace_id": message.trace_id if is_valid_trace_id(message.trace_id) else ctx.trace_id,
        "span_id": ctx.span_id,
    }
    if message.project_id is None and ctx.project_id is not None:
        updates["project_id"] = ctx.project_id
    if message.task_id is None and ctx.task_id is not None:
        updates["task_id"] = ctx.task_id
    return message.model_copy(update=updates)


def extract(message: Message) -> TraceContext:
    """Derive the receiving side's context from an inbound message.

    The trace continues: ``trace_id`` is the message's when it has a valid one,
    and a **new** ``span_id`` is minted for the receiver's work, parented to the
    sending span. The other six fields come off the envelope — ``team_id`` and
    ``agent_id`` from the message's source, ``message_id`` from its id — so a
    span recorded on the receiving side names who sent the work.
    """
    trace_id = message.trace_id if is_valid_trace_id(message.trace_id) else new_trace_id()
    parent = message.span_id if is_valid_span_id(message.span_id) else None
    return TraceContext(
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent,
        session_id=message.session_id,
        team_id=message.source_team,
        agent_id=message.source_agent,
        project_id=message.project_id,
        task_id=message.task_id,
        message_id=message.id,
    )


def context_for(message: Message, **overrides: Any) -> TraceContext:
    """The *sending* side's context for a message — same fields, same span.

    Unlike :func:`extract` this does not advance the span: it describes the
    message as it stands, which is what you want before calling :func:`inject`
    on a freshly built envelope.
    """
    fields: dict[str, Any] = {
        "session_id": message.session_id,
        "team_id": message.source_team,
        "agent_id": message.source_agent,
        "project_id": message.project_id,
        "task_id": message.task_id,
        "message_id": message.id,
    }
    if is_valid_trace_id(message.trace_id):
        fields["trace_id"] = message.trace_id
    if is_valid_span_id(message.span_id):
        fields["span_id"] = message.span_id
    fields.update(overrides)
    return TraceContext(**fields)


# ------------------------------------------------------------------ exporters


class ExporterKind(str, Enum):
    """What :func:`configure_tracing` should send spans to."""

    NONE = "none"
    CONSOLE = "console"
    FILE = "file"
    OTLP = "otlp"


#: Console needs nothing running, so it is the default the PRD asks for.
DEFAULT_EXPORTER = ExporterKind.CONSOLE
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
DEFAULT_TRACE_FILE = "data/traces/muster-traces.jsonl"

ENV_EXPORTER = "MUSTER_OTEL_EXPORTER"
ENV_OTLP_ENDPOINT = "MUSTER_OTLP_ENDPOINT"
ENV_TRACE_FILE = "MUSTER_OTEL_FILE"


@dataclass(frozen=True)
class ExporterConfig:
    kind: ExporterKind = DEFAULT_EXPORTER
    endpoint: str | None = None
    path: str | None = None

    @property
    def requires_backend(self) -> bool:
        """True only for OTLP — everything else runs on a bare laptop.

        The V2 PRD's constraint is "no heavyweight local backend by default";
        this property is how that constraint stays checkable.
        """
        return self.kind is ExporterKind.OTLP


def exporter_config(env: Mapping[str, str] | None = None) -> ExporterConfig:
    """Read the exporter selection out of the environment.

    ``MUSTER_OTEL_EXPORTER`` in ``{none, console, file, otlp}`` (default
    ``console``); ``MUSTER_OTLP_ENDPOINT`` for OTLP; ``MUSTER_OTEL_FILE`` for the
    file sink. An unknown value raises rather than silently picking a default —
    a typo in the exporter name should be loud at startup, not discovered later
    when no traces appear.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    # Strip before the fallback: an env var set to whitespace means "unset".
    raw = (source.get(ENV_EXPORTER) or "").strip().lower() or DEFAULT_EXPORTER.value
    try:
        kind = ExporterKind(raw)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in ExporterKind)
        raise ValueError(
            f"{ENV_EXPORTER}={raw!r} is not a known exporter. Choose one of: {allowed}."
        ) from exc

    endpoint = None
    path = None
    if kind is ExporterKind.OTLP:
        endpoint = (source.get(ENV_OTLP_ENDPOINT) or DEFAULT_OTLP_ENDPOINT).strip()
    elif kind is ExporterKind.FILE:
        path = (source.get(ENV_TRACE_FILE) or DEFAULT_TRACE_FILE).strip()
    return ExporterConfig(kind=kind, endpoint=endpoint, path=path)


class _LiveStdout:
    """A stdout proxy resolved at write time, not at construction time.

    ``ConsoleSpanExporter`` holds whatever file object it was handed. A span
    exported from the batch processor's background thread can therefore land on
    a ``sys.stdout`` that has since been swapped or closed — pytest's capture
    and uvicorn's reloader both do that — which raises inside the exporter.
    Resolving ``sys.stdout`` per write makes the console sink survive it.
    """

    def write(self, data: str) -> int:
        try:
            return sys.stdout.write(data)
        except (ValueError, OSError):  # closed or detached; drop the span
            return 0

    def flush(self) -> None:
        try:
            sys.stdout.flush()
        except (ValueError, OSError):
            pass


def build_exporter(config: ExporterConfig | None = None) -> Any | None:
    """Build the OTel span exporter for ``config``, or ``None``.

    ``None`` means "nothing to export through" — either the SDK is absent or the
    selection is ``none``. Callers treat that as a no-op, never as an error.
    The file sink is the console exporter pointed at an append-mode file; the
    SDK already writes one JSON span per line, so there is nothing to reinvent.
    """
    config = config or exporter_config()
    if config.kind is ExporterKind.NONE or not otel_sdk_available():
        return None

    if config.kind is ExporterKind.CONSOLE:
        return otel_export.ConsoleSpanExporter(out=_LiveStdout())

    if config.kind is ExporterKind.FILE:
        path = config.path or DEFAULT_TRACE_FILE
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        return otel_export.ConsoleSpanExporter(out=open(path, "a", encoding="utf-8"))

    exporter_cls = _otlp_exporter_class()
    if exporter_cls is None:  # pragma: no cover - depends on installed extras
        raise RuntimeError(
            f"{ENV_EXPORTER}=otlp needs the OTLP exporter package, which ships "
            "separately from the SDK. Install it with:  "
            "uv sync --extra otel  (or: uv add opentelemetry-exporter-otlp)"
        )
    return exporter_cls(endpoint=config.endpoint or DEFAULT_OTLP_ENDPOINT)


_CONFIGURED: ExporterConfig | None = None
_PROVIDER: Any | None = None

#: OTel's global provider can only be set once per process. These are the names
#: it uses for "nobody has set one yet".
_UNSET_PROVIDERS = frozenset({
    "ProxyTracerProvider", "NoOpTracerProvider", "DefaultTracerProvider",
})


def _tracer() -> Any:
    """The tracer :func:`span` emits through.

    Prefers the provider :func:`configure_tracing` built. Falling back to the
    global one means Muster spans join a host application's existing pipeline
    when it configured OTel itself and we never did.
    """
    if _PROVIDER is not None:
        return _PROVIDER.get_tracer("muster.bus")
    return otel_trace.get_tracer("muster.bus")


def configure_tracing(env: Mapping[str, str] | None = None, *,
                      force: bool = False, exporter: Any | None = None) -> ExporterConfig:
    """Build the tracer provider for the configured exporter.

    Idempotent: repeat calls return the existing configuration unless ``force``.
    Returns the resolved :class:`ExporterConfig` whether or not OTel is present,
    so callers can log what *would* have been used on a machine without the SDK.

    ``exporter`` overrides the env-selected sink, which is how a test — or an
    embedding application — points Muster spans somewhere specific.

    The global OTel provider is only installed when nothing has claimed it yet:
    a host application that configured its own pipeline keeps it, and Muster
    still emits through the provider built here.
    """
    global _CONFIGURED, _PROVIDER
    if _CONFIGURED is not None and not force:
        return _CONFIGURED

    config = exporter_config(env)
    sink = exporter if exporter is not None else build_exporter(config)
    if sink is not None and otel_sdk_available():
        provider = otel_sdk_trace.TracerProvider()
        provider.add_span_processor(otel_export.BatchSpanProcessor(sink))
        _PROVIDER = provider
        if type(otel_trace.get_tracer_provider()).__name__ in _UNSET_PROVIDERS:
            otel_trace.set_tracer_provider(provider)
    _CONFIGURED = config
    return config


def flush_tracing(timeout_millis: int = 5000) -> bool:
    """Force-export anything still buffered. Safe with no provider configured."""
    if _PROVIDER is None:
        return True
    return bool(_PROVIDER.force_flush(timeout_millis))


def reset_tracing() -> None:
    """Forget the configured exporter. For tests; production configures once."""
    global _CONFIGURED, _PROVIDER
    if _PROVIDER is not None:
        _PROVIDER.shutdown()
    _PROVIDER = None
    _CONFIGURED = None


# --------------------------------------------------------- run record linkage


def stamp_run(run: RunRecord, ctx: TraceContext | None = None) -> RunRecord:
    """Return ``run`` with ``trace_id``/``span_id`` filled in.

    This is the join between the V1 timeline and the V2 trace: every run row the
    UI shows carries the ids of the span that produced it, so one project
    timeline and one distributed trace describe the same work. Like
    :func:`inject`, an existing ``trace_id`` is preserved — a run that was
    already attributed to a trace is not re-attributed.

    ``ctx`` defaults to the ambient span context, which is why the usual call
    inside a ``with span(...)`` block needs no argument at all.
    """
    ctx = ctx or current_context() or TraceContext(
        project_id=run.project_id, task_id=run.task_id
    )
    trace_id = run.trace_id if is_valid_trace_id(run.trace_id) else ctx.trace_id
    return run.model_copy(update={"trace_id": trace_id, "span_id": ctx.span_id})


def record_traced_run(repository: "Repository", run: RunRecord,
                      ctx: TraceContext | None = None) -> RunRecord:
    """Stamp trace ids onto ``run`` and persist it. Returns the stamped record.

    A thin convenience over ``stamp_run`` + ``Repository.record_run`` so callers
    cannot persist a run and then forget the stamp.
    """
    return repository.record_run(stamp_run(run, ctx))


__all__ = [
    "BAGGAGE_FIELDS",
    "BAGGAGE_HEADER",
    "TRACEPARENT_HEADER",
    "ExporterConfig",
    "ExporterKind",
    "RecordedSpan",
    "TraceContext",
    "build_exporter",
    "configure_tracing",
    "context_for",
    "current_context",
    "exporter_config",
    "extract",
    "flush_tracing",
    "inject",
    "is_valid_span_id",
    "is_valid_trace_id",
    "new_span_id",
    "new_trace_id",
    "otel_available",
    "otel_sdk_available",
    "record_traced_run",
    "recorded_spans",
    "reset_recorded_spans",
    "reset_tracing",
    "span",
    "stamp_run",
    "use_context",
]
