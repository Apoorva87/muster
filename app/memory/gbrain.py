"""GBrain team memory: markdown on disk, GBrain as a derived index over it.

The **opt-in** V4 backend. GBrain (github.com/garrytan/gbrain, MIT) is a
TypeScript program that runs on Bun, so a Python project reaches it across a
process boundary — another runtime to install, a CLI contract that can drift,
and failures that arrive as exit codes rather than exceptions. The V4 PRD
states that cost rather than hiding it, which is why ``filesystem`` is the
default and this is not.

The one design point everything else follows from:

    **Writes land as markdown first; GBrain is asked to index afterwards.**
    A note is written to the same corpus the filesystem backend owns, and only
    then is ``gbrain import`` run over it. If GBrain is missing, slow, broken
    or mid-upgrade, the note is already on disk, already readable by a human,
    already in Git, and still recallable through the filesystem path. GBrain
    is never asked to hold anything it cannot lose.

Retrieval mirrors that. ``recall`` shells out to ``gbrain query`` for *ranking
only*: GBrain returns page slugs, and every returned note is then read back
from its markdown file. Nothing a search index says is trusted as content. A
result that does not resolve to a file in this team's corpus is dropped, so a
shared brain cannot leak another team's pages into a recall (V4 PRD rule 4).

When GBrain is unavailable this store **degrades to the filesystem backend's
lexical search instead of raising** — its absence must never block a team —
and says so in the log exactly once, not once per call. The one exception is
``require=True``, for a caller who explicitly demanded this backend: then a
missing binary is an error naming how to install it.

GBrain is invoked, never imported. No new Python dependency, no container.

Verified CLI surface (garrytan/gbrain ``master``, ``src/cli.ts``,
``src/core/ops/search.ts``, ``src/commands/import.ts``)::

    gbrain query <question> --limit N --json   ->  JSON array of search rows,
                                                   each {slug, title, type,
                                                   chunk_text, score, stale, ...}
    gbrain import <dir> --json                 ->  {"status": "success",
                                                    "imported": N, "skipped": N,
                                                    "errors": N, "chunks": N,
                                                    "total_files": N}

GBrain validates flags strictly and exits 1 on an unknown one, so nothing is
invented here. Anything beyond the two verified invocations is *configuration*
— see ``query_args`` and ``import_args`` — not a guess baked into the code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from app.kernel.memory import Confidence, MemoryKind, MemoryNote, MemoryRef
from app.memory.filesystem import FilesystemMemoryStore

logger = logging.getLogger(__name__)

#: Install the way the project's own docs say to. GBrain is **not** on npm —
#: the npm package of that name is unrelated — so `npm install -g gbrain` and
#: `bun add -g gbrain` both install the wrong thing. `gbrain doctor` detects a
#: shadowing npm install if one is already there.
INSTALL_HINT = ("install Bun, then `bun install -g github:garrytan/gbrain` "
                "and `gbrain init --pglite` (GBrain is NOT on npm: "
                "`npm install -g gbrain` installs an unrelated package)")


class GBrainError(RuntimeError):
    """GBrain could not run — with instructions for fixing it.

    Raised only when the caller asked for GBrain and no fallback is allowed.
    On the default path this is caught and turned into a degraded recall.
    """


@dataclass(frozen=True)
class CommandResult:
    """What a GBrain invocation produced. The whole subprocess seam."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


#: ``(argv, *, timeout, cwd) -> CommandResult``. Substitutable in tests so the
#: suite never needs Bun, GBrain, or a monkeypatched ``asyncio``.
CommandRunner = Callable[..., Awaitable[CommandResult]]


async def run_subprocess(argv: Sequence[str], *, timeout: float,
                         cwd: str | None = None) -> CommandResult:
    """Default runner: the same shape as ``CliAgentRunner`` drives its CLI.

    Kills the process on timeout and lets ``asyncio.TimeoutError`` escape —
    the store turns it into a :class:`GBrainError` naming the timeout, so a
    fake runner can signal a timeout the same way.
    """
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(),
                                                timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise
    return CommandResult(returncode=process.returncode or 0,
                         stdout=stdout.decode(errors="replace"),
                         stderr=stderr.decode(errors="replace"))


class GBrainMemoryStore:
    """V4 opt-in backend: markdown corpus, GBrain as the derived index.

    Satisfies ``MemoryStore`` structurally. Composes
    :class:`~app.memory.filesystem.FilesystemMemoryStore` rather than
    reimplementing it — the corpus layout, the note-per-``(kind, subject)``
    merge, ``get``/``forget`` and the lexical fallback are all its behaviour.
    This class only adds the index call and the retrieval it enables.

    Args:
        root: the team's memory directory; also what ``gbrain import`` is run over.
        team_id: memory is per-team, exactly like artifacts.
        files: a pre-built filesystem store to compose with. Defaults to one
            over ``root``.
        binary: name on ``PATH`` or an explicit path. Never hardcoded absolute.
        timeout: seconds per invocation.
        require: demand GBrain. Every failure raises instead of degrading.
        index_on_write: run ``gbrain import`` after each write. Turn off to
            batch indexing into :meth:`rebuild_index`.
        query_args: extra args appended to ``gbrain query`` — the escape hatch
            for flags this adapter deliberately does not assume, e.g.
            ``["--source", "<id>"]`` to scope a shared brain, ``["--no-expand"]``.
        import_args: extra args appended to ``gbrain import``, e.g.
            ``["--no-embed"]`` to index without paying for embeddings.
        cwd: directory to invoke GBrain from — it resolves which brain to use
            from its own config, so a per-team brain is selected this way.
        runner: the subprocess seam. Injecting one implies the binary exists
            unless ``locate`` says otherwise.
        locate: binary lookup, defaulting to :func:`shutil.which`.
    """

    def __init__(self, root: str | Path, team_id: str, *,
                 files: FilesystemMemoryStore | None = None,
                 binary: str = "gbrain", timeout: float = 60.0,
                 require: bool = False, index_on_write: bool = True,
                 query_args: Iterable[str] = (),
                 import_args: Iterable[str] = (),
                 cwd: str | None = None,
                 runner: CommandRunner | None = None,
                 locate: Callable[[str], str | None] | None = None) -> None:
        self._files = files if files is not None else FilesystemMemoryStore(root, team_id)
        self._root = Path(self._files.root)
        self._binary = binary
        self._timeout = timeout
        self._require = require
        self._index_on_write = index_on_write
        self._query_args = list(query_args)
        self._import_args = list(import_args)
        self._cwd = cwd
        self._runner: CommandRunner = runner or run_subprocess
        if locate is not None:
            self._locate = locate
        elif runner is not None:
            # An injected runner *is* the binary; there is nothing on PATH to find.
            self._locate = lambda name: name
        else:
            self._locate = shutil.which
        #: Degradation is announced once. A team that has chosen to run without
        #: GBrain does not need the same warning on every recall.
        self._degraded = False
        #: Why the last invocation failed, kept for the message it produces.
        self._reason = ""

    @property
    def team_id(self) -> str:
        return self._files.team_id

    @property
    def root(self) -> Path:
        return self._root

    @property
    def files(self) -> FilesystemMemoryStore:
        """The canonical store underneath. Markdown lives here."""
        return self._files

    @property
    def degraded(self) -> bool:
        """True once a GBrain call has failed and the lexical path took over."""
        return self._degraded

    # ------------------------------------------------------------- commands

    def query_command(self, query: str, limit: int) -> list[str]:
        """``gbrain query <question> --limit N --json`` plus configured extras."""
        return [self._binary, "query", query,
                "--limit", str(max(limit, 1)), "--json", *self._query_args]

    def import_command(self) -> list[str]:
        """``gbrain import <corpus> --json`` plus configured extras.

        The corpus path travels here. ``gbrain query`` takes no corpus
        argument — scoping a shared brain is ``--source <id>``, an id and not a
        path — so that is left to ``query_args`` rather than guessed at.
        """
        return [self._binary, "import", str(self._root), "--json",
                *self._import_args]

    # -------------------------------------------------------------- writing

    async def remember(self, *, kind: MemoryKind, subject: str, summary: str,
                       sources: list[str] | None = None,
                       confidence: Confidence = Confidence.MEDIUM,
                       evidence: str | None = None) -> MemoryRef:
        """Write the markdown, then ask GBrain to index it.

        In that order, always. The file is durable before the subprocess is
        ever spawned, so an indexing failure costs retrieval quality until the
        next :meth:`rebuild_index` and nothing else — the note is on disk, in
        Git, and still found by the lexical fallback.

        Mints a note id, so a caller inside a durable handler wraps this in
        ``Kernel.step()``; a replay must not write a second note.
        """
        ref = await self._files.remember(kind=kind, subject=subject,
                                         summary=summary, sources=sources,
                                         confidence=confidence,
                                         evidence=evidence)
        if self._index_on_write:
            # Failure here has already been survived: it degrades or, under
            # `require`, raises with the note safely written.
            self._check(await self._try(self.import_command()))
        return ref

    # -------------------------------------------------------------- reading

    async def recall(self, query: str, *, limit: int = 3,
                     kinds: list[MemoryKind] | None = None) -> list[MemoryRef]:
        """Hybrid recall through GBrain, resolved back to the markdown files.

        GBrain supplies ranking; the corpus supplies content. Each returned row
        names a page slug, which is matched against the corpus by filename stem
        (and by note id, for a brain that was told to use one). A row that
        matches nothing under this team's root is dropped — a brain shared with
        another team must not be able to answer this team's recall.

        Degrades to :meth:`FilesystemMemoryStore.recall` when GBrain is absent
        or fails, unless ``require=True``. Explicit, bounded, references only:
        the same contract as every other backend.
        """
        rows = await self._try(self.query_command(query, limit))
        if rows is None:
            self._check(rows)
            return await self._files.recall(query, limit=limit, kinds=kinds)

        allowed = set(kinds) if kinds else None
        refs: list[MemoryRef] = []
        seen: set[str] = set()
        corpus = self._corpus_by_stem()
        for slug in _slugs(rows.get("payload")):
            note = await self._resolve(slug, corpus)
            if note is None or note.id in seen:
                continue
            if allowed is not None and note.kind not in allowed:
                continue
            seen.add(note.id)
            refs.append(MemoryRef.of(note))
            if len(refs) >= max(limit, 0):
                break
        return refs

    async def get(self, note_id: str) -> MemoryNote:
        """Always from the markdown file, never from the index."""
        return await self._files.get(note_id)

    # ------------------------------------------------------------- deleting

    async def forget(self, note_id: str) -> bool:
        """Delete the file, then re-index so the deletion reaches the index.

        The file going is what makes it stay deleted; the import is bookkeeping.
        """
        removed = await self._files.forget(note_id)
        if removed and self._index_on_write:
            self._check(await self._try(self.import_command()))
        return removed

    # ---------------------------------------------------------------- index

    async def rebuild_index(self) -> int:
        """Re-run ``gbrain import`` over the corpus. Returns the note count.

        The derived index can be deleted at will: this rebuilds it from the
        files, and the count it returns is the corpus's, not the index's,
        because the corpus is what is true.
        """
        count = await self._files.rebuild_index()
        payload = await self._try(self.import_command())
        self._check(payload)
        if payload is not None:
            body = payload.get("payload")
            if isinstance(body, dict):
                logger.info("gbrain indexed %s: imported=%s skipped=%s errors=%s",
                            self._root, body.get("imported"), body.get("skipped"),
                            body.get("errors"))
        return count

    # ------------------------------------------------------------ internals

    async def _try(self, argv: list[str]) -> dict[str, Any] | None:
        """Run one GBrain command. ``None`` means "it did not work".

        Every way GBrain can fail — not installed, not executable, timed out,
        non-zero exit, an error envelope on stdout — converges here, because
        the caller's decision is the same in all of them: degrade, or raise if
        the caller demanded GBrain. The reason is kept for the message.
        """
        located = self._locate(self._binary)
        if located is None:
            self._reason = (f"{self._binary!r} is not on PATH; {INSTALL_HINT}")
            return None

        try:
            result = await self._runner(argv, timeout=self._timeout, cwd=self._cwd)
        except asyncio.TimeoutError:
            self._reason = (f"{self._binary} {argv[1]} timed out after "
                            f"{self._timeout}s")
            return None
        except OSError as exc:
            self._reason = f"could not run {self._binary}: {exc}"
            return None

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:400]
            self._reason = (f"{self._binary} {argv[1]} exited "
                            f"{result.returncode}: {detail}")
            return None

        payload, error = _decode(result.stdout)
        if error is not None:
            self._reason = f"{self._binary} {argv[1]} reported: {error}"
            return None
        return {"payload": payload}

    def _check(self, result: dict[str, Any] | None) -> None:
        """Raise for a caller who demanded GBrain; otherwise warn once."""
        if result is not None:
            return
        reason = self._reason or "gbrain is unavailable"
        if self._require:
            raise GBrainError(
                f"memory backend 'gbrain' was required but {reason}. "
                f"Either fix it, or set MEMORY_BACKEND=filesystem — the "
                f"filesystem backend needs no service and loses nothing.")
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "gbrain unavailable (%s); team %r falls back to lexical search "
                "over %s. Markdown is canonical, so nothing is lost — to enable "
                "gbrain, %s.", reason, self.team_id, self._root, INSTALL_HINT)

    def _corpus_by_stem(self) -> dict[str, Path]:
        """Filename stem -> file, for resolving a GBrain slug to a real note."""
        return {path.stem.lower(): path
                for path in sorted(self._root.rglob("*.md"))}

    async def _resolve(self, slug: str, corpus: dict[str, Path]) -> MemoryNote | None:
        """A GBrain slug back to the note it names, or ``None`` if it is not ours."""
        key = slug.rsplit("/", 1)[-1].removesuffix(".md").lower()
        path = corpus.get(key)
        if path is not None:
            try:
                return MemoryNote.from_markdown(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("gbrain named an unreadable note %s: %s", path, exc)
                return None
        if key.startswith("mem_"):
            try:
                return await self._files.get(key)
            except KeyError:
                return None
        return None


def _decode(stdout: str) -> tuple[Any, str | None]:
    """Parse ``--json`` output, tolerating a banner line before the payload.

    Returns ``(payload, error)``. GBrain reports refusals as a JSON envelope on
    *stdout* with a zero-ish shape (``{"status": "error", ...}``), so a clean
    exit code is not by itself a success.
    """
    text = stdout.strip()
    if not text:
        return None, "empty output"
    payload = _loads(text)
    if payload is None:
        # A wrapper or a future version may print a line before the JSON.
        for start in ("[", "{"):
            index = text.find(start)
            if index > 0:
                payload = _loads(text[index:])
                if payload is not None:
                    break
    if payload is None:
        return None, f"unparseable output: {text[:200]}"
    if isinstance(payload, dict) and payload.get("status") == "error":
        return None, str(payload.get("message") or payload.get("reason"))
    return payload, None


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _slugs(payload: Any) -> list[str]:
    """Slugs from a ``gbrain query --json`` payload, best first.

    The verified shape is a bare array of search rows. A dict wrapper around
    the rows is accepted too, so a future envelope does not silently return
    nothing — the adapter reads what it recognises and ignores the rest.
    """
    rows = payload
    if isinstance(payload, dict):
        for key in ("results", "data", "pages", "rows"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            return []
    if not isinstance(rows, list):
        return []
    slugs: list[str] = []
    for row in rows:
        slug = row.get("slug") if isinstance(row, dict) else row
        if isinstance(slug, str) and slug:
            slugs.append(slug)
    return slugs
