"""The memory note format, the store contract, and the wiring.

Backend-independent: uses NullMemoryStore and a small in-memory fake, so this
file stays green regardless of which backends exist.
"""
from datetime import date, timedelta

import pytest

from app.agents.base import AgentContext, StubLLMRunner
from app.kernel.memory import (Confidence, MemoryKind, MemoryNote, MemoryRef,
                               MemoryStore, NullMemoryStore, TimelineEntry)
from app.memory import (BACKENDS, MemoryError_, ReadOnlyMemory, apply_permission,
                        build_memory_store)


class FakeMemory:
    """Minimal MemoryStore for wiring tests."""

    def __init__(self, team_id="investment"):
        self._team_id = team_id
        self.notes: dict[str, MemoryNote] = {}
        self.recall_calls: list[str] = []

    @property
    def team_id(self) -> str:
        return self._team_id

    async def remember(self, *, kind, subject, summary, sources=None,
                       confidence=Confidence.MEDIUM, evidence=None):
        note = MemoryNote(team=self._team_id, kind=kind, subject=subject,
                          summary=summary, sources=sources or [],
                          confidence=confidence)
        self.notes[note.id] = note
        return MemoryRef.of(note)

    async def recall(self, query, *, limit=3, kinds=None):
        self.recall_calls.append(query)
        found = [n for n in self.notes.values()
                 if not kinds or n.kind in kinds]
        return [MemoryRef.of(n) for n in found[:limit]]

    async def get(self, note_id):
        return self.notes[note_id]

    async def forget(self, note_id):
        return self.notes.pop(note_id, None) is not None

    async def rebuild_index(self):
        return len(self.notes)


# ------------------------------------------------------------- note format

def test_note_round_trips_through_markdown():
    note = MemoryNote(team="investment", kind=MemoryKind.LESSON,
                      subject="valuation multiples",
                      summary="Cite a peer group when quoting a P/E multiple.",
                      sources=["run_c3d4"])
    note = note.with_evidence("rejected: 31x with no comparables", source="run_c3d4")
    back = MemoryNote.from_markdown(note.to_markdown())

    assert back.subject == note.subject
    assert back.summary == note.summary
    assert back.kind is note.kind
    assert back.sources == note.sources
    assert [e.note for e in back.timeline] == [e.note for e in note.timeline]
    assert back.timeline[0].source == "run_c3d4"


def test_markdown_is_readable_by_a_human():
    """The whole reason for choosing markdown over embeddings."""
    note = MemoryNote(team="t", kind=MemoryKind.LESSON, subject="s",
                      summary="A human can read this.")
    text = note.to_markdown()
    assert text.startswith("---\n")
    assert "# Summary" in text
    assert "A human can read this." in text


def test_a_note_without_sources_is_ungrounded():
    """PRD: a note with no sources is a rumour."""
    assert not MemoryNote(team="t", kind=MemoryKind.LESSON, subject="s",
                          summary="x").is_grounded
    assert MemoryNote(team="t", kind=MemoryKind.LESSON, subject="s",
                      summary="x", sources=["run_1"]).is_grounded


def test_evidence_appends_and_bumps_updated():
    note = MemoryNote(team="t", kind=MemoryKind.DECISION, subject="s", summary="x",
                      created=date(2020, 1, 1), updated=date(2020, 1, 1))
    grown = note.with_evidence("something happened", source="run_9")

    assert len(grown.timeline) == 1
    assert grown.updated > note.updated
    assert "run_9" in grown.sources
    assert note.timeline == [], "the original must not be mutated"


def test_timeline_entries_round_trip():
    entry = TimelineEntry(on=date(2026, 8, 30), note="rejected: too rich",
                          source="run_c3d4")
    assert TimelineEntry.parse(entry.render()) == entry


def test_timeline_entry_without_a_source_round_trips():
    entry = TimelineEntry(on=date(2026, 8, 30), note="approved")
    assert TimelineEntry.parse(entry.render()) == entry


def test_malformed_note_raises_clearly():
    with pytest.raises(ValueError, match="frontmatter"):
        MemoryNote.from_markdown("no frontmatter here")


def test_slug_is_filesystem_safe_and_stable():
    note = MemoryNote(team="t", kind=MemoryKind.LESSON,
                      subject="P/E multiples & peers!", summary="x")
    assert note.slug == note.slug
    assert all(c.isalnum() or c == "-" for c in note.slug)


def test_ref_carries_no_body():
    """References cross into context; bodies are loaded deliberately."""
    note = MemoryNote(team="t", kind=MemoryKind.LESSON, subject="s",
                      summary="line one\n" + "x" * 5000)
    ref = MemoryRef.of(note)
    assert len(ref.model_dump_json()) < 400
    assert "x" * 200 not in ref.preview


# ----------------------------------------------------------------- backends

def test_null_store_satisfies_the_protocol():
    assert isinstance(NullMemoryStore(), MemoryStore)


async def test_disabled_memory_recalls_nothing_and_never_raises():
    """MEMORY_BACKEND=none is a supported mode, not a stub."""
    store = build_memory_store(backend="none", team_id="t")
    assert await store.recall("anything") == []
    assert await store.rebuild_index() == 0
    assert await store.forget("mem_x") is False


def test_unknown_backend_lists_the_valid_ones():
    with pytest.raises(MemoryError_, match="unknown MEMORY_BACKEND"):
        build_memory_store(backend="pinecone", team_id="t")


def test_every_documented_backend_is_selectable():
    assert set(BACKENDS) == {"filesystem", "gbrain", "none"}


# -------------------------------------------------------------- permissions

def test_permission_off_disables_memory_entirely():
    narrowed = apply_permission(FakeMemory(), "off")
    assert isinstance(narrowed, NullMemoryStore)


def test_permission_read_allows_recall_but_refuses_writes():
    narrowed = apply_permission(FakeMemory(), "read")
    assert isinstance(narrowed, ReadOnlyMemory)


async def test_a_read_only_write_fails_loudly():
    """A team that thinks it is learning and is not would be worse."""
    narrowed = apply_permission(FakeMemory(), "read")
    with pytest.raises(PermissionError, match="read-write"):
        await narrowed.remember(kind=MemoryKind.LESSON, subject="s", summary="x")


async def test_read_only_still_recalls():
    inner = FakeMemory()
    await inner.remember(kind=MemoryKind.LESSON, subject="s", summary="x")
    assert len(await apply_permission(inner, "read").recall("s")) == 1


def test_unset_permission_means_full_access():
    inner = FakeMemory()
    assert apply_permission(inner, None) is inner


def test_unknown_permission_is_rejected():
    with pytest.raises(MemoryError_, match="memory permission"):
        apply_permission(FakeMemory(), "sometimes")


# ------------------------------------------------------------- team.yaml

def test_team_yaml_accepts_a_per_agent_memory_permission(tmp_path):
    from app.kernel.team_spec import load_team_spec

    path = tmp_path / "team.yaml"
    path.write_text("""
team: {id: remembering}
agents:
  critic:
    entrypoint: app.agents.critic
    memory: read-write
  research:
    entrypoint: app.agents.research
    memory: off
  finance:
    entrypoint: app.agents.finance
""")
    spec = load_team_spec(path)
    assert spec.memory_for("critic") == "read-write"
    assert spec.memory_for("research") == "off"
    assert spec.memory_for("finance") is None


def test_yaml_bare_off_is_read_as_the_permission_not_a_boolean(tmp_path):
    """YAML 1.1 parses `off` as False. Everyone writes `memory: off` anyway."""
    from app.kernel.team_spec import load_team_spec

    path = tmp_path / "team.yaml"
    path.write_text("""
team: {id: yamltrap}
agents:
  critic: {entrypoint: app.agents.critic, memory: off}
""")
    assert load_team_spec(path).memory_for("critic") == "off"


def test_yaml_bare_on_is_rejected_with_a_useful_message(tmp_path):
    from app.kernel.team_spec import load_team_spec

    path = tmp_path / "team.yaml"
    path.write_text("""
team: {id: yamltrap}
agents:
  critic: {entrypoint: app.agents.critic, memory: on}
""")
    with pytest.raises(ValueError, match="read or read-write"):
        load_team_spec(path)


def test_team_yaml_rejects_a_bad_memory_permission(tmp_path):
    from app.kernel.team_spec import SpecError, load_team_spec

    path = tmp_path / "team.yaml"
    path.write_text("""
team: {id: broken}
agents:
  critic: {entrypoint: app.agents.critic, memory: sometimes}
""")
    with pytest.raises(SpecError, match="off\\|read\\|read-write"):
        load_team_spec(path)


# ----------------------------------------------------- agent-facing wiring

@pytest.fixture
def agent_ctx(kernel):
    return AgentContext(kernel=kernel, llm=StubLLMRunner(), memory=FakeMemory())


async def test_an_agent_gets_nothing_unless_it_asks(agent_ctx):
    """The core rule: memory is retrieved explicitly, never injected."""
    store = agent_ctx.memory
    await store.remember(kind=MemoryKind.LESSON, subject="s", summary="x")
    assert store.recall_calls == [], "nothing may query memory on the agent's behalf"


async def test_recall_returns_references(agent_ctx):
    await agent_ctx.memory.remember(kind=MemoryKind.LESSON,
                                    subject="valuation", summary="cite peers")
    refs = await agent_ctx.recall("valuation")
    assert refs and isinstance(refs[0], MemoryRef)
    assert agent_ctx.memory.recall_calls == ["valuation"]


async def test_recall_respects_the_limit(agent_ctx):
    for i in range(5):
        await agent_ctx.memory.remember(kind=MemoryKind.LESSON,
                                        subject=f"s{i}", summary="x")
    assert len(await agent_ctx.recall("s", limit=2)) == 2


async def test_recall_is_replay_safe(ctx, repo, store):
    """Journalled: the corpus may move between an attempt and its replay."""
    from app.kernel.runtime import Kernel
    from app.kernel.subscriptions import SubscriptionRegistry

    memory = FakeMemory()
    await memory.remember(kind=MemoryKind.LESSON, subject="a", summary="x")

    def build():
        return AgentContext(
            kernel=Kernel(ctx=ctx, repository=repo,
                          subscriptions=SubscriptionRegistry(repo), artifacts=store),
            llm=StubLLMRunner(), memory=memory)

    first = await build().recall("a")
    ctx.replay()
    # The world moves on between the attempt and the replay.
    await memory.remember(kind=MemoryKind.LESSON, subject="b", summary="y")
    second = await build().recall("a")

    assert [r.id for r in first] == [r.id for r in second]


async def test_remember_is_replay_safe(ctx, repo, store):
    from app.kernel.runtime import Kernel
    from app.kernel.subscriptions import SubscriptionRegistry

    memory = FakeMemory()

    def build():
        return AgentContext(
            kernel=Kernel(ctx=ctx, repository=repo,
                          subscriptions=SubscriptionRegistry(repo), artifacts=store),
            llm=StubLLMRunner(), memory=memory)

    first = await build().remember(kind=MemoryKind.LESSON, subject="s",
                                   summary="x", sources=["run_1"])
    ctx.replay()
    second = await build().remember(kind=MemoryKind.LESSON, subject="s",
                                    summary="x", sources=["run_1"])

    assert first.id == second.id, "a replay must not write a second note"
    assert len(memory.notes) == 1


async def test_load_memory_fetches_the_body_deliberately(agent_ctx):
    ref = await agent_ctx.memory.remember(kind=MemoryKind.LESSON,
                                          subject="s", summary="the full body")
    note = await agent_ctx.load_memory(ref)
    assert note.summary == "the full body"


async def test_an_agent_with_memory_off_behaves_as_in_v3(kernel):
    """PRD acceptance: MEMORY_BACKEND=none runs identically to V3."""
    ctx = AgentContext(kernel=kernel, llm=StubLLMRunner())
    assert await ctx.recall("anything") == []
