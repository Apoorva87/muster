"""GBrain memory backend: the opt-in one, and the one that must never block a team.

Every test here runs with **no Bun and no GBrain installed**. The subprocess is
a constructor seam (``runner=``/``locate=``), so nothing is monkeypatched onto
``asyncio`` and nothing spawns a process. The two tests that genuinely need a
real binary are ``@pytest.mark.integration`` — deselected by default, and they
skip cleanly when it is absent.

What is pinned here is the contract the V4 PRD cares about:

* markdown reaches disk even when GBrain does not;
* a missing GBrain degrades to lexical search and says so once;
* a caller who demanded GBrain gets an actionable error instead;
* the commands are the two verified invocations, with no invented flags.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

import pytest

from app.kernel.memory import Confidence, MemoryKind, MemoryStore
from app.memory.filesystem import FilesystemMemoryStore
from app.memory.gbrain import (CommandResult, GBrainError, GBrainMemoryStore,
                               run_subprocess)

TEAM = "investment"


class FakeGBrain:
    """A stand-in for the CLI. Records every argv; replies from a script.

    ``replies`` maps the GBrain verb (``query``, ``import``) to what that call
    should do: a :class:`CommandResult`, or an exception instance to raise.
    """

    def __init__(self, **replies) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.cwds: list[str | None] = []

    async def __call__(self, argv, *, timeout, cwd=None) -> CommandResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        self.cwds.append(cwd)
        reply = self.replies.get(argv[1], CommandResult(returncode=0, stdout="[]"))
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def commands(self, verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[1] == verb]


def ok(payload) -> CommandResult:
    return CommandResult(returncode=0, stdout=json.dumps(payload))


def rows(*slugs: str) -> CommandResult:
    """A ``gbrain query --json`` payload in the verified SearchResult shape."""
    return ok([{"slug": slug, "page_id": i, "title": slug, "type": "note",
                "chunk_text": f"chunk for {slug}", "chunk_source": "compiled_truth",
                "chunk_id": i, "chunk_index": 0, "score": 1.0 - i / 10, "stale": False}
               for i, slug in enumerate(slugs)])


def make_store(tmp_path, **kwargs) -> GBrainMemoryStore:
    """A store whose GBrain is a fake, unless a test says otherwise."""
    kwargs.setdefault("runner", FakeGBrain())
    return GBrainMemoryStore(tmp_path / "memory", TEAM, **kwargs)


async def seed(store, subject: str, summary: str, *,
               kind: MemoryKind = MemoryKind.LESSON):
    ref = await store.remember(kind=kind, subject=subject, summary=summary,
                               sources=["run_seed"])
    return ref


# ------------------------------------------------------------------ contract


def test_satisfies_the_memory_store_protocol(tmp_path):
    store = make_store(tmp_path)
    assert isinstance(store, MemoryStore)
    assert store.team_id == TEAM


def test_composes_the_filesystem_store_rather_than_reimplementing_it(tmp_path):
    store = make_store(tmp_path)
    assert isinstance(store.files, FilesystemMemoryStore)
    assert store.files.root == store.root


def test_accepts_a_prebuilt_filesystem_store(tmp_path):
    files = FilesystemMemoryStore(tmp_path / "corpus", TEAM)
    store = GBrainMemoryStore(tmp_path / "ignored", TEAM, files=files,
                              runner=FakeGBrain())
    assert store.root == files.root


# ------------------------------------------------------- markdown is canonical


async def test_write_lands_as_markdown_even_when_gbrain_fails(tmp_path):
    """The single most important property: the note survives a broken index."""
    fake = FakeGBrain(**{"import": CommandResult(returncode=1, stderr="boom")})
    store = make_store(tmp_path, runner=fake)

    ref = await store.remember(kind=MemoryKind.LESSON,
                               subject="valuation-multiples",
                               summary="Cite a peer group with any P/E.",
                               sources=["run_c3d4"])

    files = list((store.root / "lessons").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "valuation-multiples" in text
    assert "Cite a peer group" in text
    assert "run_c3d4" in text
    # And it is still retrievable, through the filesystem path.
    assert (await store.get(ref.id)).subject == "valuation-multiples"
    assert store.degraded is True


async def test_write_lands_as_markdown_when_gbrain_is_not_installed(tmp_path):
    store = make_store(tmp_path, runner=FakeGBrain(), locate=lambda name: None)
    ref = await store.remember(kind=MemoryKind.DECISION, subject="pe-31x",
                               summary="Rejected: no comparables.",
                               sources=["run_e5f6"])
    assert (store.root / "decisions").glob("*.md")
    assert (await store.get(ref.id)).summary == "Rejected: no comparables."


async def test_markdown_is_written_before_gbrain_is_asked_to_index(tmp_path):
    """Ordering, not just outcome: the file exists by the time import runs."""
    seen: list[int] = []

    async def runner(argv, *, timeout, cwd=None):
        seen.append(len(list((tmp_path / "memory").rglob("*.md"))))
        return ok({"status": "success", "imported": 1})

    store = make_store(tmp_path, runner=runner)
    await store.remember(kind=MemoryKind.LESSON, subject="ordering",
                         summary="disk first", sources=["run_1"])
    assert seen == [1]


async def test_get_reads_the_file_so_a_hand_edit_wins(tmp_path):
    store = make_store(tmp_path)
    ref = await seed(store, "staleness", "The old world.")
    path = next((store.root / "lessons").glob("*.md"))
    path.write_text(path.read_text().replace("The old world.", "The new world."))
    assert (await store.get(ref.id)).summary == "The new world."


async def test_forget_removes_the_file_and_reindexes(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake)
    ref = await seed(store, "poisoned", "A confident wrong lesson.")

    assert await store.forget(ref.id) is True
    assert list((store.root / "lessons").glob("*.md")) == []
    assert len(fake.commands("import")) == 2  # one for the write, one for the delete
    assert await store.forget(ref.id) is False


# --------------------------------------------------------------- command shape


async def test_query_command_carries_the_query_the_limit_and_json(tmp_path):
    fake = FakeGBrain(query=rows())
    store = make_store(tmp_path, runner=fake, index_on_write=False)

    await store.recall("valuation multiples", limit=3)

    assert fake.commands("query") == [
        # Over-fetched: ~/.gbrain is shared across teams, so we ask for more
        # than we need and filter to this team's files afterwards. Asking for
        # exactly `limit` lets another team's pages crowd this team out.
        ["gbrain", "query", "valuation multiples",
         "--limit", str(GBrainMemoryStore.OVERFETCH_MIN), "--json"]]


async def test_import_command_carries_the_corpus_path(tmp_path):
    """``gbrain query`` takes no corpus argument, so the path travels on import."""
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake)
    await seed(store, "corpus-path", "anything")

    assert fake.commands("import") == [
        ["gbrain", "import", str(store.root), "--json"]]


async def test_binary_extra_args_timeout_and_cwd_are_configurable(tmp_path):
    fake = FakeGBrain(query=rows())
    store = GBrainMemoryStore(
        tmp_path / "memory", TEAM, binary="/opt/brains/gbrain", timeout=12.5,
        query_args=["--source", "team-investment"], import_args=["--no-embed"],
        cwd=str(tmp_path), runner=fake)

    await store.recall("q", limit=2)
    await store.rebuild_index()

    assert fake.commands("query")[0] == [
        "/opt/brains/gbrain", "query", "q",
        "--limit", str(GBrainMemoryStore.OVERFETCH_MIN), "--json",
        "--source", "team-investment"]
    assert fake.commands("import")[0] == [
        "/opt/brains/gbrain", "import", str(store.root), "--json", "--no-embed"]
    assert fake.timeouts == [12.5, 12.5]
    assert fake.cwds == [str(tmp_path), str(tmp_path)]


async def test_index_on_write_can_be_turned_off(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    await seed(store, "batched", "index later")
    assert fake.calls == []


# --------------------------------------------------------------------- recall


async def test_recall_parses_gbrain_output_into_refs(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    first = await seed(store, "valuation-multiples", "Cite a peer group.")
    second = await seed(store, "discount-rates", "State the risk-free rate.")

    slugs = [path.stem for path in sorted(store.root.rglob("*.md"))]
    ordered = [s for s in slugs if s.startswith("discount")] + \
              [s for s in slugs if s.startswith("valuation")]
    fake.replies["query"] = rows(*ordered)

    refs = await store.recall("multiples", limit=5)

    # GBrain supplies the order; the corpus supplies the content.
    assert [ref.id for ref in refs] == [second.id, first.id]
    assert [ref.subject for ref in refs] == ["discount-rates", "valuation-multiples"]
    assert refs[0].kind is MemoryKind.LESSON
    assert refs[0].preview == "State the risk-free rate."
    assert store.degraded is False


async def test_recall_honours_the_limit_and_drops_duplicate_pages(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    await seed(store, "one", "First.")
    await seed(store, "two", "Second.")
    stems = [path.stem for path in sorted(store.root.rglob("*.md"))]
    # GBrain returns one row per chunk, so a page can appear more than once.
    fake.replies["query"] = rows(stems[0], stems[0], stems[1])

    refs = await store.recall("anything", limit=2)
    assert len({ref.id for ref in refs}) == 2

    assert len(await store.recall("anything", limit=1)) == 1


async def test_recall_drops_results_that_are_not_this_teams_notes(tmp_path):
    """A shared brain must not answer this team's recall with someone else's page."""
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    mine = await seed(store, "mine", "Ours.")
    stem = next(store.root.rglob("*.md")).stem
    fake.replies["query"] = rows("other-team/secret-strategy", stem,
                                 "mem_deadbeef")

    refs = await store.recall("q", limit=5)
    assert [ref.id for ref in refs] == [mine.id]


async def test_recall_filters_by_kind(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    await seed(store, "a-lesson", "L.", kind=MemoryKind.LESSON)
    decision = await seed(store, "a-decision", "D.", kind=MemoryKind.DECISION)
    stems = [path.stem for path in sorted(store.root.rglob("*.md"))]
    fake.replies["query"] = rows(*stems)

    refs = await store.recall("q", limit=5, kinds=[MemoryKind.DECISION])
    assert [ref.id for ref in refs] == [decision.id]


async def test_recall_tolerates_an_envelope_and_a_banner_line(tmp_path):
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    ref = await seed(store, "tolerant", "Parsed anyway.")
    stem = next(store.root.rglob("*.md")).stem
    payload = json.dumps({"results": [{"slug": stem, "score": 0.9}]})
    fake.replies["query"] = CommandResult(
        returncode=0, stdout=f"using brain: default\n{payload}\n")

    assert [r.id for r in await store.recall("q")] == [ref.id]


async def test_recall_returns_nothing_when_gbrain_found_nothing(tmp_path):
    """An empty result is an answer, not a failure — no fallback, no warning."""
    store = make_store(tmp_path, runner=FakeGBrain(query=rows()),
                       index_on_write=False)
    await seed(store, "present", "Lexical search would have found this.")
    assert await store.recall("present") == []
    assert store.degraded is False


# ------------------------------------------------------------- degradation


async def test_recall_degrades_to_lexical_search_when_the_binary_is_missing(tmp_path):
    store = make_store(tmp_path, locate=lambda name: None)
    await seed(store, "valuation-multiples", "Cite a peer group with any P/E.")

    refs = await store.recall("peer group", limit=3)  # does not raise

    assert [ref.subject for ref in refs] == ["valuation-multiples"]
    assert store.degraded is True


@pytest.mark.parametrize("reply", [
    CommandResult(returncode=2, stderr="No brain configured"),
    CommandResult(returncode=0, stdout=""),
    CommandResult(returncode=0, stdout="not json at all"),
    CommandResult(returncode=0,
                  stdout='{"status": "error", "reason": "invalid_flag", '
                         '"message": "unknown flag --nope"}'),
    asyncio.TimeoutError(),
    OSError("Exec format error"),
])
async def test_every_failure_mode_degrades_instead_of_raising(tmp_path, reply):
    store = make_store(tmp_path, runner=FakeGBrain(query=reply),
                       index_on_write=False)
    await seed(store, "resilience", "A team is never blocked by a memory backend.")

    refs = await store.recall("resilience", limit=3)

    assert [ref.subject for ref in refs] == ["resilience"]
    assert store.degraded is True


async def test_a_timeout_kills_the_call_and_names_the_timeout(tmp_path, caplog):
    store = make_store(tmp_path, runner=FakeGBrain(query=asyncio.TimeoutError()),
                       timeout=0.25, index_on_write=False)
    with caplog.at_level(logging.WARNING, logger="app.memory.gbrain"):
        await store.recall("q")
    assert "timed out after 0.25s" in caplog.text


async def test_a_non_zero_exit_names_the_code_and_the_stderr(tmp_path, caplog):
    store = make_store(
        tmp_path, index_on_write=False,
        runner=FakeGBrain(query=CommandResult(returncode=3, stderr="pglite locked")))
    with caplog.at_level(logging.WARNING, logger="app.memory.gbrain"):
        await store.recall("q")
    assert "exited 3" in caplog.text
    assert "pglite locked" in caplog.text


async def test_the_degradation_is_logged_once_not_per_call(tmp_path, caplog):
    store = make_store(tmp_path, locate=lambda name: None)
    with caplog.at_level(logging.WARNING, logger="app.memory.gbrain"):
        for _ in range(5):
            await store.recall("anything")
        await store.remember(kind=MemoryKind.LESSON, subject="s", summary="x",
                             sources=["run_1"])
        await store.rebuild_index()

    warnings = [r for r in caplog.records if "gbrain unavailable" in r.message]
    assert len(warnings) == 1
    assert "bun install -g github:garrytan/gbrain" in caplog.text


# ------------------------------------------------------------ require=True


async def test_require_raises_with_an_actionable_message_when_not_installed(tmp_path):
    store = make_store(tmp_path, require=True, locate=lambda name: None)
    with pytest.raises(GBrainError) as excinfo:
        await store.recall("anything")

    message = str(excinfo.value)
    assert "not on PATH" in message
    assert "bun install -g github:garrytan/gbrain" in message
    assert "gbrain init --pglite" in message
    assert "MEMORY_BACKEND=filesystem" in message
    assert "npm install -g gbrain" in message  # names the squatted package


async def test_require_raises_on_a_failed_call_but_the_note_is_still_written(tmp_path):
    fake = FakeGBrain(**{"import": CommandResult(returncode=1, stderr="nope")})
    store = make_store(tmp_path, runner=fake, require=True)

    with pytest.raises(GBrainError, match="exited 1"):
        await store.remember(kind=MemoryKind.LESSON, subject="durable",
                             summary="On disk before the subprocess ran.",
                             sources=["run_1"])

    assert len(list((store.root / "lessons").glob("*.md"))) == 1


async def test_require_does_not_raise_while_gbrain_works(tmp_path):
    store = make_store(tmp_path, runner=FakeGBrain(query=rows()), require=True)
    await seed(store, "healthy", "No error.")
    assert await store.recall("healthy") == []
    assert store.degraded is False


# ------------------------------------------------------------- rebuild_index


async def test_rebuild_index_reruns_import_over_the_corpus(tmp_path):
    fake = FakeGBrain(**{"import": ok({"status": "success", "imported": 2,
                                       "skipped": 0, "errors": 0, "chunks": 7,
                                       "total_files": 2})})
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    await seed(store, "one", "First.")
    await seed(store, "two", "Second.")

    assert await store.rebuild_index() == 2
    assert fake.commands("import") == [["gbrain", "import", str(store.root), "--json"]]


async def test_deleting_the_index_loses_nothing(tmp_path):
    """The V4 acceptance criterion, exercised through this backend."""
    fake = FakeGBrain()
    store = make_store(tmp_path, runner=fake, index_on_write=False)
    ref = await seed(store, "canonical", "Markdown is the truth.")

    fake.replies["query"] = asyncio.TimeoutError()  # the index is gone
    refs = await store.recall("markdown truth")

    assert [r.id for r in refs] == [ref.id]
    assert (await store.get(ref.id)).summary == "Markdown is the truth."
    assert await store.rebuild_index() == 1


# ------------------------------------------------------- the default runner


async def test_the_default_runner_actually_spawns_a_process():
    """The seam is a seam, not a replacement: the default runner runs a real
    binary. Uses ``echo``, so this needs neither Bun nor GBrain."""
    result = await run_subprocess(["echo", "hello"], timeout=10.0)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"


# --------------------------------------------------------------- integration


@pytest.mark.integration
async def test_real_gbrain_indexes_the_corpus(tmp_path):
    if shutil.which("gbrain") is None:
        pytest.skip("gbrain is not installed (bun install -g github:garrytan/gbrain)")
    store = GBrainMemoryStore(tmp_path / "memory", TEAM, require=True)
    await store.remember(kind=MemoryKind.LESSON, subject="valuation-multiples",
                         summary="Cite a peer group with any P/E.",
                         sources=["run_c3d4"], confidence=Confidence.HIGH)
    assert await store.rebuild_index() == 1


@pytest.mark.integration
async def test_real_gbrain_recalls_what_it_indexed(tmp_path):
    if shutil.which("gbrain") is None:
        pytest.skip("gbrain is not installed (bun install -g github:garrytan/gbrain)")
    store = GBrainMemoryStore(tmp_path / "memory", TEAM, require=True)
    ref = await store.remember(kind=MemoryKind.LESSON, subject="valuation-multiples",
                               summary="Cite a peer group with any P/E.",
                               sources=["run_c3d4"])
    refs = await store.recall("peer group comparison for a P/E multiple", limit=3)
    assert ref.id in {r.id for r in refs}



@pytest.mark.integration
async def test_a_shared_brain_never_leaks_another_teams_pages(tmp_path):
    """GBrain keeps ONE brain per user under ~/.gbrain, not one per directory.

    Verified by observation: a brain re-inited in a fresh directory still
    answered with pages from a previously imported corpus. So V4's rule that a
    team's memory is its own is enforced *here* — every result is resolved back
    to a file under this team's root — and this test is what keeps that filter
    from being deleted as redundant.
    """
    import shutil

    if shutil.which("gbrain") is None:
        pytest.skip("gbrain is not installed (bun install -g github:garrytan/gbrain)")

    from app.kernel.memory import MemoryKind
    from app.memory.gbrain import GBrainMemoryStore

    ours = GBrainMemoryStore(root=tmp_path / "ours", team_id="investment")
    theirs = GBrainMemoryStore(root=tmp_path / "theirs", team_id="research")

    await theirs.remember(kind=MemoryKind.LESSON, subject="quantum widgets",
                          summary="Quantum widgets are the research team's topic.",
                          sources=["run_theirs"])
    await theirs.rebuild_index()

    # The shared brain now knows about "quantum widgets". Our team must not.
    assert await ours.recall("quantum widgets") == [], \
        "a shared brain leaked another team's pages into this team's recall"
