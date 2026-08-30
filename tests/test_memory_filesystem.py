"""The default memory backend: markdown files, lexical search, no service.

These tests are the V4 acceptance criteria written down: the corpus is the
truth, a human can edit it, deleting the index loses nothing, and a wrong or
broken note never takes recall down with it.
"""
from datetime import date

import pytest

from app.kernel.memory import (Confidence, MemoryKind, MemoryNote, MemoryRef,
                               MemoryStore)
from app.memory.filesystem import _DIRECTORIES, FilesystemMemoryStore


@pytest.fixture
def store(tmp_path):
    return FilesystemMemoryStore(root=tmp_path, team_id="investment")


def write_note(root, note: MemoryNote):
    """Put a note on disk the way a human with an editor and Git would."""
    directory = root / _DIRECTORIES[note.kind]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{note.slug}.md"
    path.write_text(note.to_markdown(), encoding="utf-8")
    return path


# ------------------------------------------------------------------ contract

def test_satisfies_memory_store_protocol(store):
    assert isinstance(store, MemoryStore)


def test_team_id_is_exposed(store):
    assert store.team_id == "investment"


def test_directories_are_created_lazily(tmp_path):
    FilesystemMemoryStore(root=tmp_path / "memory", team_id="investment")
    assert not (tmp_path / "memory").exists()


# ------------------------------------------------------------------- writing

async def test_remember_writes_a_file_in_the_kind_directory(store, tmp_path):
    ref = await store.remember(kind=MemoryKind.LESSON,
                               subject="valuation-multiples",
                               summary="Cite a peer group with every multiple.",
                               sources=["run_c3d4"])
    files = list((tmp_path / "lessons").glob("*.md"))
    assert len(files) == 1
    assert isinstance(ref, MemoryRef)
    assert ref.subject == "valuation-multiples"
    assert files[0].stem.startswith("valuation-multiples-")


@pytest.mark.parametrize("kind,directory", list(_DIRECTORIES.items()))
async def test_every_kind_has_its_own_directory(store, tmp_path, kind, directory):
    await store.remember(kind=kind, subject="s", summary="x", sources=["run_1"])
    assert len(list((tmp_path / directory).glob("*.md"))) == 1


async def test_file_round_trips_through_from_markdown(store, tmp_path):
    ref = await store.remember(kind=MemoryKind.DECISION, subject="pe-31x",
                               summary="Rejected: no comparables.",
                               sources=["run_c3d4"], confidence=Confidence.HIGH,
                               evidence="rejected: cited 31x with no peers")
    path = next((tmp_path / "decisions").glob("*.md"))
    note = MemoryNote.from_markdown(path.read_text(encoding="utf-8"))

    assert note.id == ref.id
    assert note.team == "investment"
    assert note.kind is MemoryKind.DECISION
    assert note.subject == "pe-31x"
    assert note.summary == "Rejected: no comparables."
    assert note.confidence is Confidence.HIGH
    assert note.sources == ["run_c3d4"]
    assert [e.note for e in note.timeline] == ["rejected: cited 31x with no peers"]
    assert note.is_grounded


async def test_remembering_the_same_subject_extends_one_note(store, tmp_path):
    first = await store.remember(kind=MemoryKind.LESSON,
                                 subject="valuation-multiples",
                                 summary="Cite a peer group.", sources=["run_c3d4"],
                                 evidence="rejected: 31x with no comparables")
    second = await store.remember(kind=MemoryKind.LESSON,
                                  subject="valuation-multiples",
                                  summary="Cite a peer group up front.",
                                  sources=["run_e5f6"],
                                  evidence="approved after adding a peer table")

    assert second.id == first.id
    assert len(list((tmp_path / "lessons").glob("*.md"))) == 1

    note = await store.get(first.id)
    assert note.summary == "Cite a peer group up front."
    assert [e.note for e in note.timeline] == [
        "rejected: 31x with no comparables",
        "approved after adding a peer table",
    ]
    assert note.sources == ["run_c3d4", "run_e5f6"]


async def test_same_subject_in_a_different_kind_is_a_different_note(store):
    lesson = await store.remember(kind=MemoryKind.LESSON, subject="multiples",
                                  summary="a", sources=["run_1"])
    domain = await store.remember(kind=MemoryKind.DOMAIN, subject="multiples",
                                  summary="b", sources=["run_2"])
    assert lesson.id != domain.id


async def test_remember_without_evidence_leaves_the_timeline_empty(store):
    ref = await store.remember(kind=MemoryKind.DOMAIN, subject="ebitda",
                               summary="Cash proxy, not cash.", sources=["run_1"])
    assert (await store.get(ref.id)).timeline == []


# ------------------------------------------------------------------- recall

async def seed(store):
    await store.remember(kind=MemoryKind.LESSON, subject="valuation-multiples",
                         summary="Always cite a peer group.", sources=["run_1"])
    await store.remember(kind=MemoryKind.DOMAIN, subject="ebitda",
                         summary="A cash proxy, not cash itself.", sources=["run_2"])
    await store.remember(kind=MemoryKind.DECISION, subject="series-b-pass",
                         summary="Passed on the round; the burn multiple was rich.",
                         sources=["run_3"])


async def test_recall_finds_by_term_overlap(store):
    await seed(store)
    hits = await store.recall("peer group")
    assert [h.subject for h in hits] == ["valuation-multiples"]


async def test_recall_matches_the_body_not_only_the_subject(store):
    await seed(store)
    hits = await store.recall("burn")
    assert [h.subject for h in hits] == ["series-b-pass"]


async def test_recall_returns_nothing_when_no_term_matches(store):
    await seed(store)
    assert await store.recall("kubernetes") == []


async def test_recall_respects_limit(store):
    await seed(store)
    for n in range(5):
        await store.remember(kind=MemoryKind.LESSON, subject=f"cash-lesson-{n}",
                             summary="cash discipline", sources=[f"run_{n}"])
    assert len(await store.recall("cash", limit=2)) == 2
    assert len(await store.recall("cash", limit=3)) == 3


async def test_recall_respects_kinds(store):
    await seed(store)
    await store.remember(kind=MemoryKind.LESSON, subject="cash-burn",
                         summary="Watch the burn multiple.", sources=["run_4"])

    hits = await store.recall("burn", kinds=[MemoryKind.DECISION])
    assert [h.subject for h in hits] == ["series-b-pass"]
    assert all(h.kind is MemoryKind.DECISION for h in hits)


async def test_recall_ranks_a_subject_match_above_a_body_match(store):
    await store.remember(kind=MemoryKind.LESSON, subject="unrelated-heading",
                         summary="The team should watch dilution closely.",
                         sources=["run_1"])
    await store.remember(kind=MemoryKind.LESSON, subject="dilution",
                         summary="Something else entirely.", sources=["run_2"])

    hits = await store.recall("dilution", limit=2)
    assert [h.subject for h in hits] == ["dilution", "unrelated-heading"]


async def test_recency_breaks_a_scoring_tie(store, tmp_path):
    # Identical wording, so only `updated` can separate them (PRD: staleness).
    write_note(tmp_path, MemoryNote(team="investment", kind=MemoryKind.LESSON,
                                    subject="alpha-signal", summary="a signal",
                                    sources=["run_1"], created=date(2026, 1, 1),
                                    updated=date(2026, 1, 1)))
    write_note(tmp_path, MemoryNote(team="investment", kind=MemoryKind.LESSON,
                                    subject="beta-signal", summary="a signal",
                                    sources=["run_2"], created=date(2026, 1, 1),
                                    updated=date(2026, 9, 14)))

    hits = await store.recall("signal", limit=2)
    assert [h.subject for h in hits] == ["beta-signal", "alpha-signal"]


async def test_empty_query_returns_the_most_recently_updated(store, tmp_path):
    write_note(tmp_path, MemoryNote(team="investment", kind=MemoryKind.LESSON,
                                    subject="old", summary="stale",
                                    sources=["run_1"], updated=date(2026, 1, 1)))
    write_note(tmp_path, MemoryNote(team="investment", kind=MemoryKind.DOMAIN,
                                    subject="new", summary="fresh",
                                    sources=["run_2"], updated=date(2026, 8, 30)))

    assert [h.subject for h in await store.recall("", limit=2)] == ["new", "old"]


async def test_recall_returns_references_not_bodies(store):
    await store.remember(kind=MemoryKind.LESSON, subject="valuation-multiples",
                         summary="x" * 5000, sources=["run_1"])
    hit = (await store.recall("valuation"))[0]
    assert isinstance(hit, MemoryRef)
    assert len(hit.model_dump_json()) < 512


# ----------------------------------------------------------------------- get

async def test_get_returns_the_note(store):
    ref = await store.remember(kind=MemoryKind.LESSON, subject="dilution",
                               summary="Watch it.", sources=["run_1"])
    note = await store.get(ref.id)
    assert note.id == ref.id and note.summary == "Watch it."


async def test_get_unknown_id_raises(store):
    with pytest.raises(KeyError):
        await store.get("mem_doesnotexist")


async def test_get_works_on_a_cold_store(store, tmp_path):
    ref = await store.remember(kind=MemoryKind.DECISION, subject="series-b-pass",
                               summary="Passed.", sources=["run_1"])

    # A restart: a fresh store over the same directory, index empty.
    cold = FilesystemMemoryStore(root=tmp_path, team_id="investment")
    assert cold._index == {}
    assert (await cold.get(ref.id)).summary == "Passed."
    assert [h.subject for h in await cold.recall("passed")] == ["series-b-pass"]


async def test_a_hand_edited_file_is_picked_up(store, tmp_path):
    """PRD: a wrong memory is fixable with an editor and a commit."""
    ref = await store.remember(kind=MemoryKind.LESSON, subject="dilution",
                               summary="Options never dilute founders.",
                               sources=["run_1"])
    path = store.path_for(ref.id)
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Options never dilute founders.",
        "Option pools dilute founders before the round closes."),
        encoding="utf-8")

    assert (await store.get(ref.id)).summary.startswith("Option pools dilute")
    assert [h.subject for h in await store.recall("pools")] == ["dilution"]
    assert await store.recall("never") == []


# -------------------------------------------------------------------- forget

async def test_forget_deletes_the_file_and_stays_deleted(store, tmp_path):
    ref = await store.remember(kind=MemoryKind.LESSON, subject="wrong-lesson",
                               summary="A confident, wrong lesson.",
                               sources=["run_1"])
    path = store.path_for(ref.id)

    assert await store.forget(ref.id) is True
    assert not path.exists()
    with pytest.raises(KeyError):
        await store.get(ref.id)

    # It must not come back when the derived index is rebuilt.
    assert await store.rebuild_index() == 0
    assert await store.recall("confident") == []
    cold = FilesystemMemoryStore(root=tmp_path, team_id="investment")
    with pytest.raises(KeyError):
        await cold.get(ref.id)


async def test_forget_unknown_id_returns_false(store):
    assert await store.forget("mem_doesnotexist") is False


async def test_forget_leaves_other_notes_alone(store):
    keep = await store.remember(kind=MemoryKind.LESSON, subject="keep",
                                summary="keep this", sources=["run_1"])
    drop = await store.remember(kind=MemoryKind.LESSON, subject="drop",
                                summary="drop this", sources=["run_2"])
    assert await store.forget(drop.id) is True
    assert (await store.get(keep.id)).summary == "keep this"


# -------------------------------------------------------------------- index

async def test_rebuild_index_counts_the_corpus_and_loses_nothing(store, tmp_path):
    """PRD acceptance: deleting the derived index loses nothing."""
    await seed(store)
    lesson = await store.remember(kind=MemoryKind.LESSON, subject="dilution",
                                  summary="Watch the option pool.", sources=["run_9"])

    store._index = {}  # the index is disposable; the markdown is not
    assert await store.rebuild_index() == 4

    assert (await store.get(lesson.id)).summary == "Watch the option pool."
    assert [h.subject for h in await store.recall("option pool")] == ["dilution"]
    assert len(await store.recall("", limit=10)) == 4


async def test_rebuild_index_sees_notes_written_by_another_process(store, tmp_path):
    write_note(tmp_path, MemoryNote(team="investment", kind=MemoryKind.DOMAIN,
                                    subject="checked-in-by-hand",
                                    summary="A note committed straight to the repo.",
                                    sources=["run_1"]))
    assert await store.rebuild_index() == 1
    assert [h.subject for h in await store.recall("committed")] == ["checked-in-by-hand"]


# ------------------------------------------------------------------ failures

async def test_a_malformed_file_is_skipped_and_recall_still_works(store, tmp_path):
    await store.remember(kind=MemoryKind.LESSON, subject="valuation-multiples",
                         summary="Always cite a peer group.", sources=["run_1"])
    (tmp_path / "lessons" / "broken.md").write_text("no frontmatter here at all\n",
                                                    encoding="utf-8")
    (tmp_path / "lessons" / "half-broken.md").write_text(
        "---\nid: mem_x\nteam: investment\n---\n\n# Summary\n\nno kind field\n",
        encoding="utf-8")

    assert [h.subject for h in await store.recall("peer")] == ["valuation-multiples"]
    assert await store.rebuild_index() == 1


async def test_a_malformed_file_is_logged_not_swallowed(store, tmp_path, caplog):
    (tmp_path / "lessons").mkdir(parents=True)
    (tmp_path / "lessons" / "broken.md").write_text("garbage", encoding="utf-8")
    with caplog.at_level("WARNING"):
        await store.rebuild_index()
    assert "broken.md" in caplog.text


# ------------------------------------------------------------- path safety

def test_unsafe_team_id_is_rejected(tmp_path):
    for team in ["../evil", "a/b", "a\\b", ".hidden", ""]:
        with pytest.raises(ValueError):
            FilesystemMemoryStore(root=tmp_path, team_id=team)


async def test_unsafe_subject_is_rejected(store, tmp_path):
    for subject in ["../../etc/passwd", "a/b", "a\\b", ".hidden", ""]:
        with pytest.raises(ValueError):
            await store.remember(kind=MemoryKind.LESSON, subject=subject,
                                 summary="x", sources=["run_1"])
    assert not any(tmp_path.rglob("*.md"))
