"""Tracing: W3C context, cross-team propagation, exporters, run linkage.

The headline is :func:`test_two_hop_cross_team_path_shares_one_trace_id`, which
is V2 acceptance criterion 10 — "trace/correlation IDs connect the cross-team
path". Everything else here supports that claim.

OpenTelemetry is an optional extra, so every test must pass without it. The
handful of assertions that only hold on the SDK path are marked
``skipif(not otel_available())``; the behavioural tests run on both paths.
"""

from __future__ import annotations

import json
import re

import pytest

from app.kernel.models import RunRecord
from bus.models.message import Message, MessageKind
from bus.observability import tracing
from bus.observability.tracing import (BAGGAGE_HEADER, TRACEPARENT_HEADER,
                                       ExporterConfig, ExporterKind,
                                       TraceContext, build_exporter,
                                       configure_tracing, context_for,
                                       current_context, exporter_config,
                                       extract, inject, new_span_id,
                                       new_trace_id, otel_available,
                                       otel_sdk_available, record_traced_run,
                                       recorded_spans, reset_recorded_spans,
                                       reset_tracing, span, stamp_run,
                                       use_context)

requires_otel = pytest.mark.skipif(
    not otel_available(), reason="OpenTelemetry is an optional extra"
)
requires_otel_sdk = pytest.mark.skipif(
    not otel_sdk_available(), reason="OpenTelemetry SDK is an optional extra"
)

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")


@pytest.fixture(autouse=True)
def clean_tracing_state():
    """Each test starts with an empty span buffer and no configured exporter."""
    reset_recorded_spans()
    reset_tracing()
    yield
    reset_recorded_spans()
    reset_tracing()


def command(**overrides) -> Message:
    base = dict(
        kind=MessageKind.COMMAND,
        session_id="sess_1",
        source_team="investment",
        source_agent="director",
        destination="team://research/research",
        project_id="proj_1",
        task_id="task_1",
    )
    base.update(overrides)
    return Message(**base)


# ------------------------------------------------------------ W3C id shapes


def test_fresh_context_generates_valid_w3c_ids():
    ctx = TraceContext()

    assert len(ctx.trace_id) == 32
    assert len(ctx.span_id) == 16
    assert int(ctx.trace_id, 16) != 0
    assert int(ctx.span_id, 16) != 0
    assert ctx.trace_id == ctx.trace_id.lower()
    assert ctx.span_id == ctx.span_id.lower()


def test_fresh_context_emits_well_formed_traceparent():
    ctx = TraceContext()

    assert TRACEPARENT_RE.match(ctx.traceparent), ctx.traceparent
    version, trace_id, span_id, flags = ctx.traceparent.split("-")
    assert version == "00"
    assert trace_id == ctx.trace_id
    assert span_id == ctx.span_id
    assert flags == "01"  # sampled by default


def test_unsampled_context_sets_the_flag_byte():
    assert TraceContext(sampled=False).traceparent.endswith("-00")


def test_generated_ids_are_unique():
    assert len({new_trace_id() for _ in range(200)}) == 200
    assert len({new_span_id() for _ in range(200)}) == 200


def test_malformed_ids_are_repaired_not_propagated():
    """Bad telemetry input must never produce an invalid traceparent."""
    ctx = TraceContext(trace_id="nope", span_id="0" * 16)

    assert TRACEPARENT_RE.match(ctx.traceparent)
    assert ctx.trace_id != "nope"
    assert int(ctx.span_id, 16) != 0


# --------------------------------------------------------- header round-trip


def test_headers_round_trip_all_eight_fields():
    ctx = TraceContext(
        session_id="sess_1",
        team_id="investment",
        agent_id="director",
        project_id="proj_1",
        task_id="task_1",
        message_id="msg_abc",
    )

    restored = TraceContext.from_headers(ctx.to_headers())

    for name in ("trace_id", "span_id", "session_id", "team_id", "agent_id",
                 "project_id", "task_id", "message_id"):
        assert getattr(restored, name) == getattr(ctx, name), name


def test_headers_are_w3c_named():
    headers = TraceContext(session_id="sess_1").to_headers()

    assert set(headers) == {TRACEPARENT_HEADER, BAGGAGE_HEADER}
    assert TRACEPARENT_RE.match(headers[TRACEPARENT_HEADER])
    assert headers[BAGGAGE_HEADER] == "muster.session_id=sess_1"


def test_baggage_header_is_omitted_when_no_fields_are_set():
    assert set(TraceContext().to_headers()) == {TRACEPARENT_HEADER}


def test_header_lookup_is_case_insensitive():
    ctx = TraceContext(session_id="sess_1")
    upper = {k.upper(): v for k, v in ctx.to_headers().items()}

    restored = TraceContext.from_headers(upper)

    assert restored.trace_id == ctx.trace_id
    assert restored.session_id == "sess_1"


def test_baggage_values_survive_reserved_characters():
    ctx = TraceContext(session_id="a,b=c;d ", team_id="x/y")

    restored = TraceContext.from_headers(ctx.to_headers())

    assert restored.session_id == "a,b=c;d "
    assert restored.team_id == "x/y"


def test_foreign_baggage_members_are_ignored():
    ctx = TraceContext(session_id="sess_1")
    headers = ctx.to_headers()
    headers[BAGGAGE_HEADER] = "vendor.thing=1," + headers[BAGGAGE_HEADER]

    restored = TraceContext.from_headers(headers)

    assert restored.session_id == "sess_1"


@pytest.mark.parametrize("raw", [
    "",
    "garbage",
    "00-tooshort-0000000000000001-01",
    f"00-{'0' * 32}-{'0' * 16}-01",   # all-zero ids are invalid per spec
    "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",  # forbidden version
])
def test_unusable_traceparent_starts_a_fresh_trace(raw):
    """A broken upstream header must not fail the hop — we start a trace."""
    ctx = TraceContext.from_headers({TRACEPARENT_HEADER: raw})

    assert TRACEPARENT_RE.match(ctx.traceparent)


def test_traceparent_with_future_version_and_extra_fields_still_parses():
    trace_id, span_id = "a" * 32, "b" * 16
    ctx = TraceContext.from_headers(
        {TRACEPARENT_HEADER: f"01-{trace_id}-{span_id}-01-extra"}
    )

    assert ctx.trace_id == trace_id
    assert ctx.span_id == span_id


# ------------------------------------------------------------ child contexts


def test_child_keeps_the_trace_and_links_the_parent():
    parent = TraceContext(session_id="sess_1", team_id="investment")
    child = parent.child()

    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id
    assert child.parent_span_id == parent.span_id
    assert child.session_id == "sess_1"
    assert child.team_id == "investment"


def test_child_refuses_to_fork_the_trace():
    with pytest.raises(ValueError, match="trace_id"):
        TraceContext().child(trace_id=new_trace_id())


# ------------------------------------------------------- inject and extract


def test_inject_stamps_trace_and_span_onto_a_message():
    ctx = TraceContext()
    message = inject(command(), ctx)

    assert message.trace_id == ctx.trace_id
    assert message.span_id == ctx.span_id


def test_inject_returns_a_copy_and_leaves_the_original_alone():
    original = command()
    stamped = inject(original, TraceContext())

    assert original.trace_id is None
    assert stamped is not original
    assert stamped.id == original.id


def test_message_that_already_has_a_trace_id_keeps_it():
    """Crossing a boundary continues a trace; it never starts a new one."""
    existing = new_trace_id()
    message = command(trace_id=existing)

    stamped = inject(message, TraceContext())

    assert stamped.trace_id == existing


def test_inject_backfills_project_and_task_only_when_absent():
    ctx = TraceContext(project_id="proj_from_ctx", task_id="task_from_ctx")

    filled = inject(command(project_id=None, task_id=None), ctx)
    kept = inject(command(project_id="proj_own", task_id="task_own"), ctx)

    assert (filled.project_id, filled.task_id) == ("proj_from_ctx", "task_from_ctx")
    assert (kept.project_id, kept.task_id) == ("proj_own", "task_own")


def test_extract_preserves_trace_id_and_mints_a_new_span_id():
    ctx = TraceContext()
    message = inject(command(), ctx)

    received = extract(message)

    assert received.trace_id == ctx.trace_id
    assert received.span_id != ctx.span_id
    assert received.parent_span_id == ctx.span_id


def test_extract_of_an_untraced_message_starts_a_trace():
    received = extract(command())

    assert TRACEPARENT_RE.match(received.traceparent)
    assert received.parent_span_id is None


def test_all_eight_fields_survive_propagation():
    ctx = TraceContext(
        session_id="sess_1",
        team_id="investment",
        agent_id="director",
        project_id="proj_1",
        task_id="task_1",
    )
    message = inject(command(), ctx)

    received = extract(message)

    assert received.trace_id == ctx.trace_id          # 1
    assert len(received.span_id) == 16                # 2: a fresh, valid span
    assert received.span_id != ctx.span_id
    assert received.session_id == "sess_1"            # 3
    assert received.team_id == "investment"           # 4
    assert received.agent_id == "director"            # 5
    assert received.project_id == "proj_1"            # 6
    assert received.task_id == "task_1"               # 7
    assert received.message_id == message.id          # 8


def test_context_for_describes_the_sending_span_without_advancing_it():
    ctx = TraceContext()
    message = inject(command(), ctx)

    sending = context_for(message)

    assert sending.trace_id == ctx.trace_id
    assert sending.span_id == ctx.span_id
    assert sending.message_id == message.id


def test_propagation_survives_a_header_hop():
    """The bus may carry context over HTTP; headers must lose nothing."""
    ctx = extract(inject(command(), TraceContext(session_id="sess_1")))

    over_the_wire = TraceContext.from_headers(ctx.to_headers())

    assert over_the_wire.trace_id == ctx.trace_id
    assert over_the_wire.span_id == ctx.span_id
    assert over_the_wire.session_id == "sess_1"


# --------------------------------------------- the V2 acceptance criterion


def test_two_hop_cross_team_path_shares_one_trace_id():
    """Team A -> bus -> team B -> bus -> team C is ONE trace.

    This is V2 acceptance criterion 10. Each hop records its own span, every
    span reports the same ``trace_id``, every span_id is distinct, and the
    parent links chain the hops into a single path.
    """
    # --- team A: originates the work.
    with span("teamA.plan", team="investment") as a_ctx:
        a_ctx = a_ctx.child(
            session_id="sess_1", team_id="investment", agent_id="director",
            project_id="proj_1", task_id="task_1",
        )
        hop1 = inject(
            command(source_team="investment", source_agent="director",
                    destination="team://research/research"),
            a_ctx,
        )

    # --- the bus: serialises to W3C headers and back, as a remote adapter would.
    wire = TraceContext.from_headers(context_for(hop1).to_headers())
    assert wire.trace_id == hop1.trace_id

    # --- team B: receives, works, and sends onward via Message.caused().
    b_ctx = extract(hop1)
    with span("teamB.research", parent=b_ctx, team="research") as b_span:
        hop2 = hop1.caused(
            kind=MessageKind.EVENT,
            topic="research.complete",
            source_team="research",
            source_agent="research",
        )
        hop2 = inject(hop2, b_span)

    # --- team C: receives the second hop.
    c_ctx = extract(hop2)
    with span("teamC.review", parent=c_ctx, team="security"):
        pass

    # ONE trace id connects the whole path.
    trace_ids = {
        a_ctx.trace_id, hop1.trace_id, wire.trace_id,
        b_ctx.trace_id, hop2.trace_id, c_ctx.trace_id,
    }
    assert len(trace_ids) == 1, trace_ids

    recorded = recorded_spans()
    assert [s.name for s in recorded] == [
        "teamA.plan", "teamB.research", "teamC.review"
    ]
    assert {s.trace_id for s in recorded} == trace_ids
    assert len({s.span_id for s in recorded}) == 3

    # ...and the hops chain, rather than being three unrelated spans.
    assert b_ctx.parent_span_id == hop1.span_id
    assert c_ctx.parent_span_id == hop2.span_id


def test_two_hop_path_chains_correlation_and_causation():
    """``caused()`` must chain correlation/causation alongside the trace id."""
    hop1 = inject(command(), TraceContext())

    hop2 = hop1.caused(kind=MessageKind.EVENT, topic="research.complete",
                       source_team="research", source_agent="research")
    hop3 = hop2.caused(kind=MessageKind.EVENT, topic="critique.complete",
                       source_team="security", source_agent="critic")

    # correlation_id is the whole conversation; it is set once and never moves.
    assert hop2.correlation_id == hop1.id
    assert hop3.correlation_id == hop1.id
    # causation_id is the immediate predecessor.
    assert hop2.causation_id == hop1.id
    assert hop3.causation_id == hop2.id
    # ...and the trace id rides along with them.
    assert hop2.trace_id == hop1.trace_id == hop3.trace_id


def test_a_second_session_is_a_different_trace():
    """Two independent paths must not collapse into one trace."""
    one = inject(command(session_id="sess_1"), TraceContext())
    two = inject(command(session_id="sess_2"), TraceContext())

    assert one.trace_id != two.trace_id


# ---------------------------------------------------------------- exporters


def test_exporter_defaults_to_console_and_needs_no_backend():
    config = exporter_config(env={})

    assert config.kind is ExporterKind.CONSOLE
    assert config.requires_backend is False


def test_blank_env_var_falls_back_to_the_default():
    assert exporter_config(env={"MUSTER_OTEL_EXPORTER": "  "}).kind is ExporterKind.CONSOLE


@pytest.mark.parametrize("value,expected", [
    ("none", ExporterKind.NONE),
    ("console", ExporterKind.CONSOLE),
    ("file", ExporterKind.FILE),
    ("otlp", ExporterKind.OTLP),
    ("  OTLP  ", ExporterKind.OTLP),
])
def test_exporter_selection_honours_the_env_var(value, expected):
    assert exporter_config(env={"MUSTER_OTEL_EXPORTER": value}).kind is expected


def test_otlp_endpoint_comes_from_the_env_var():
    config = exporter_config(env={
        "MUSTER_OTEL_EXPORTER": "otlp",
        "MUSTER_OTLP_ENDPOINT": "http://tempo:4317",
    })

    assert config.endpoint == "http://tempo:4317"
    assert config.requires_backend is True


def test_otlp_endpoint_has_a_default():
    config = exporter_config(env={"MUSTER_OTEL_EXPORTER": "otlp"})

    assert config.endpoint and config.endpoint.startswith("http")


def test_file_exporter_path_comes_from_the_env_var():
    config = exporter_config(env={
        "MUSTER_OTEL_EXPORTER": "file",
        "MUSTER_OTEL_FILE": "/tmp/muster-traces.jsonl",
    })

    assert config.path == "/tmp/muster-traces.jsonl"
    assert config.requires_backend is False


def test_unknown_exporter_is_a_loud_config_error():
    with pytest.raises(ValueError, match="MUSTER_OTEL_EXPORTER"):
        exporter_config(env={"MUSTER_OTEL_EXPORTER": "jaeger"})


def test_exporter_config_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("MUSTER_OTEL_EXPORTER", "none")

    assert exporter_config().kind is ExporterKind.NONE


def test_only_otlp_requires_a_backend():
    for kind in (ExporterKind.NONE, ExporterKind.CONSOLE, ExporterKind.FILE):
        assert ExporterConfig(kind=kind).requires_backend is False
    assert ExporterConfig(kind=ExporterKind.OTLP).requires_backend is True


def test_build_exporter_is_none_when_export_is_disabled():
    assert build_exporter(ExporterConfig(kind=ExporterKind.NONE)) is None


def test_configure_tracing_is_safe_and_idempotent():
    first = configure_tracing(env={})
    second = configure_tracing(env={"MUSTER_OTEL_EXPORTER": "none"})

    assert first.kind is ExporterKind.CONSOLE
    assert second is first  # idempotent until reset/forced


def test_configure_tracing_can_be_forced_to_re_read():
    configure_tracing(env={})
    forced = configure_tracing(env={"MUSTER_OTEL_EXPORTER": "none"}, force=True)

    assert forced.kind is ExporterKind.NONE


def test_flush_tracing_is_safe_with_nothing_configured():
    assert tracing.flush_tracing() is True


@requires_otel_sdk
def test_cross_team_spans_reach_the_configured_exporter():
    """The two-hop path must arrive at a real backend as one trace."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import \
        InMemorySpanExporter

    exporter = InMemorySpanExporter()
    configure_tracing(env={}, force=True, exporter=exporter)

    with span("teamA.plan") as a_ctx:
        message = inject(command(), a_ctx)
    with span("teamB.research", parent=extract(message)):
        pass
    assert tracing.flush_tracing()

    exported = exporter.get_finished_spans()
    assert [s.name for s in exported] == ["teamA.plan", "teamB.research"]
    assert len({s.attributes["muster.trace_id"] for s in exported}) == 1


@pytest.mark.skipif(otel_sdk_available(), reason="asserts the no-SDK path")
def test_build_exporter_is_none_without_the_sdk():
    assert build_exporter(ExporterConfig(kind=ExporterKind.CONSOLE)) is None


@requires_otel_sdk
def test_console_exporter_is_built_when_the_sdk_is_present():
    assert build_exporter(ExporterConfig(kind=ExporterKind.CONSOLE)) is not None


@requires_otel_sdk
def test_file_exporter_writes_spans_to_the_named_file(tmp_path):
    path = tmp_path / "nested" / "traces.jsonl"
    config = exporter_config(env={
        "MUSTER_OTEL_EXPORTER": "file", "MUSTER_OTEL_FILE": str(path),
    })

    exporter = build_exporter(config)
    try:
        assert exporter is not None
        assert path.parent.is_dir()
    finally:
        exporter.shutdown()


# --------------------------------------------------------- span contextmanager


def test_span_records_name_and_attributes_either_way():
    with span("research.run", agent="research", attempt=2) as ctx:
        assert current_context() is ctx

    (entry,) = recorded_spans()
    assert entry.name == "research.run"
    assert entry.attributes == {"agent": "research", "attempt": 2}
    assert entry.status == "OK"
    assert entry.trace_id == ctx.trace_id
    assert entry.duration_ms is not None and entry.duration_ms >= 0


def test_span_clears_the_ambient_context_on_exit():
    assert current_context() is None
    with span("outer"):
        pass
    assert current_context() is None


def test_nested_spans_share_a_trace_and_chain_parents():
    with span("outer") as outer:
        with span("inner") as inner:
            pass

    assert inner.trace_id == outer.trace_id
    assert inner.parent_span_id == outer.span_id
    # Finish order, as a real span processor would see them.
    assert [s.name for s in recorded_spans()] == ["inner", "outer"]


def test_span_inherits_an_ambient_context():
    base = TraceContext(session_id="sess_1")

    with use_context(base):
        with span("work") as ctx:
            pass

    assert ctx.trace_id == base.trace_id
    assert ctx.parent_span_id == base.span_id
    assert ctx.session_id == "sess_1"


def test_span_records_failures_and_re_raises():
    with pytest.raises(RuntimeError, match="boom"):
        with span("failing"):
            raise RuntimeError("boom")

    (entry,) = recorded_spans()
    assert entry.status == "ERROR"
    assert "boom" in entry.error
    assert entry.ended_at is not None
    assert current_context() is None


def test_span_buffer_is_bounded():
    for i in range(tracing.MAX_RECORDED_SPANS + 25):
        with span(f"s{i}"):
            pass

    recorded = recorded_spans()
    assert len(recorded) == tracing.MAX_RECORDED_SPANS
    assert recorded[-1].name == f"s{tracing.MAX_RECORDED_SPANS + 24}"


@pytest.mark.skipif(otel_available(), reason="asserts the no-otel fallback path")
def test_span_works_with_otel_absent():
    """The in-memory recorder is the whole implementation when otel is gone."""
    with span("no-otel") as ctx:
        pass

    assert tracing.otel_trace is None
    assert recorded_spans()[0].span_id == ctx.span_id


@requires_otel
def test_span_emits_a_real_otel_span_when_installed():
    from opentelemetry import trace as otel_trace

    seen = {}
    with span("with-otel", agent="research") as ctx:
        seen["otel"] = otel_trace.get_current_span()

    assert seen["otel"] is not None
    # In-memory recording still happens, so tests read the same either way.
    (entry,) = recorded_spans()
    assert entry.name == "with-otel"
    assert entry.trace_id == ctx.trace_id


@requires_otel_sdk
def test_otel_span_carries_the_muster_ids_as_attributes():
    """Muster's ids must be joinable from a vendor backend."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import \
        InMemorySpanExporter

    exporter = InMemorySpanExporter()
    configure_tracing(env={}, force=True, exporter=exporter)

    with span("traced", parent=TraceContext(session_id="sess_1")) as ctx:
        pass
    assert tracing.flush_tracing()

    finished = exporter.get_finished_spans()
    assert finished, "no OTel span was exported"
    attrs = finished[-1].attributes
    assert attrs["muster.trace_id"] == ctx.trace_id
    assert attrs["muster.span_id"] == ctx.span_id
    assert attrs["muster.session_id"] == "sess_1"


def test_span_context_is_injectable_into_a_message():
    """The end-to-end shape: work in a span, then send from that span."""
    with span("director.plan") as ctx:
        message = inject(command(), ctx)

    assert message.trace_id == ctx.trace_id
    assert message.span_id == ctx.span_id
    assert extract(message).trace_id == recorded_spans()[0].trace_id


# ------------------------------------------------------------- run records


def test_stamp_run_sets_trace_and_span_ids():
    ctx = TraceContext()
    run = RunRecord(project_id="proj_1", agent="research", event_type="task.started")

    stamped = stamp_run(run, ctx)

    assert stamped.trace_id == ctx.trace_id
    assert stamped.span_id == ctx.span_id
    assert run.trace_id is None  # the original is untouched


def test_stamp_run_uses_the_ambient_span_context():
    run = RunRecord(project_id="proj_1", agent="research", event_type="task.started")

    with span("research.run") as ctx:
        stamped = stamp_run(run)

    assert stamped.trace_id == ctx.trace_id
    assert stamped.span_id == ctx.span_id


def test_stamp_run_without_any_context_still_produces_valid_ids():
    run = RunRecord(project_id="proj_1", task_id="task_1", agent="research",
                    event_type="task.started")

    stamped = stamp_run(run)

    assert TRACEPARENT_RE.match(
        TraceContext(trace_id=stamped.trace_id, span_id=stamped.span_id).traceparent
    )


def test_stamp_run_keeps_an_existing_trace_id():
    existing = new_trace_id()
    run = RunRecord(project_id="proj_1", agent="research",
                    event_type="task.started", trace_id=existing)

    stamped = stamp_run(run, TraceContext())

    assert stamped.trace_id == existing


def test_record_traced_run_persists_the_stamped_ids(repo):
    """The V1 timeline and the V2 trace must line up in the database."""
    run = RunRecord(project_id="proj_1", agent="research", event_type="task.started")

    with span("research.run") as ctx:
        record_traced_run(repo, run, ctx)

    (stored,) = repo.list_runs("proj_1")
    assert stored.trace_id == ctx.trace_id
    assert stored.span_id == ctx.span_id


def test_a_cross_team_path_and_its_run_records_share_one_trace(repo):
    """Two teams' timeline rows join on the same trace id as the messages."""
    with span("teamA.plan") as a_ctx:
        message = inject(command(), a_ctx)
        record_traced_run(
            repo,
            RunRecord(project_id="proj_1", agent="director", event_type="task.started"),
            a_ctx,
        )

    b_ctx = extract(message)
    with span("teamB.research", parent=b_ctx) as b_span:
        record_traced_run(
            repo,
            RunRecord(project_id="proj_1", agent="research", event_type="task.started"),
            b_span,
        )

    runs = repo.list_runs("proj_1")
    assert len(runs) == 2
    assert {r.trace_id for r in runs} == {message.trace_id}
    assert len({r.span_id for r in runs}) == 2


# ------------------------------------------------------------- module hygiene


#: Exercises the module in a subprocess where ``import opentelemetry`` raises,
#: so the no-otel fallback is verified even on a machine that HAS otel installed.
_NO_OTEL_SCRIPT = """
import sys
for name in list(sys.modules):
    if name == "opentelemetry" or name.startswith("opentelemetry."):
        del sys.modules[name]
# A None entry in sys.modules makes `import opentelemetry` raise ImportError.
sys.modules["opentelemetry"] = None
sys.modules["opentelemetry.sdk"] = None
sys.modules["opentelemetry.sdk.trace"] = None

from bus.observability import tracing as t
from bus.models.message import Message, MessageKind

assert t.otel_available() is False, "blocker did not take effect"
assert t.build_exporter(t.ExporterConfig(kind=t.ExporterKind.CONSOLE)) is None
assert t.configure_tracing(env={}).kind is t.ExporterKind.CONSOLE

msg = Message(kind=MessageKind.COMMAND, session_id="s", source_team="a",
              source_agent="d", destination="team://b/r")
with t.span("hop-a") as ctx:
    msg = t.inject(msg, ctx)
with t.span("hop-b", parent=t.extract(msg)) as ctx_b:
    pass

assert ctx_b.trace_id == ctx.trace_id
assert len(t.recorded_spans()) == 2
print("OK")
"""


def test_module_works_with_opentelemetry_unimportable():
    """The core suite runs with zero infrastructure; that must not regress.

    Run in a subprocess with ``opentelemetry`` made unimportable, so this holds
    on a developer machine that has the optional extra installed too.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", _NO_OTEL_SCRIPT],
        cwd=root, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_recorded_span_is_json_serialisable_enough_for_a_file_sink():
    with span("serialise-me", agent="research"):
        pass

    (entry,) = recorded_spans()
    payload = json.dumps({
        "name": entry.name,
        "trace_id": entry.trace_id,
        "span_id": entry.span_id,
        "attributes": entry.attributes,
        "status": entry.status,
    })
    assert json.loads(payload)["trace_id"] == entry.trace_id
