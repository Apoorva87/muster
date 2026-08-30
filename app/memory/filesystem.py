"""Filesystem team memory: markdown on disk, lexical search over it.

The default backend, and the one the V4 PRD requires to stay sufficient for a
small team: no new service, no network, no model. The files *are* the memory.
This class only reads and writes them, plus keeps a disposable index of where
they live — delete the index and nothing is lost, edit a file with an editor
and the next recall sees the edit, because every read goes back to disk.

Layout, one directory per kind (V4 PRD, "Memory bank")::

    <root>/lessons/    <root>/domain/    <root>/decisions/    <root>/entities/

Each file is ``<note.slug>.md``. The root is the team's memory boundary, the
same way a project directory is for artifacts: a store reads only below it.

Search is lexical term overlap — deliberately, not embeddings. An index that
cannot be predicted cannot be debugged, and a wrong memory is a bug someone has
to find with ``grep``. The exact ranking is documented on :meth:`recall`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

import yaml

from app.kernel.artifacts import _safe_segment
from app.kernel.memory import (Confidence, MemoryKind, MemoryNote, MemoryRef)

logger = logging.getLogger(__name__)

#: One directory per kind, named as the PRD names them.
_DIRECTORIES = {
    MemoryKind.LESSON: "lessons",
    MemoryKind.DOMAIN: "domain",
    MemoryKind.DECISION: "decisions",
    MemoryKind.ENTITY: "entities",
}

_TOKEN = re.compile(r"[a-z0-9]+")

#: How much a subject match is worth relative to a body match. Any value > 0
#: puts every subject match above every body-only match of equal overlap.
_SUBJECT_WEIGHT = 0.5

#: A corrupt file must never take the corpus down with it, so a bad parse is
#: skipped and logged. These are the ways a hand-edited note goes wrong:
#: unreadable file, no/invalid frontmatter, missing or unknown field.
_UNREADABLE = (OSError, ValueError, KeyError, yaml.YAMLError)


class FilesystemMemoryStore:
    """V4 default backend: ``<root>/<kind>/<slug>.md``.

    Satisfies ``MemoryStore`` structurally. Directories are created lazily, so
    a team that never writes a memory leaves no directories behind.
    """

    def __init__(self, root: str | Path, team_id: str) -> None:
        self._root = Path(root)
        self._team_id = _safe_segment(team_id, "team_id")
        #: Derived and disposable: note id -> file. Never a source of truth.
        self._index: dict[str, Path] = {}

    @property
    def team_id(self) -> str:
        return self._team_id

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------ writing

    async def remember(self, *, kind: MemoryKind, subject: str, summary: str,
                       sources: list[str] | None = None,
                       confidence: Confidence = Confidence.MEDIUM,
                       evidence: str | None = None) -> MemoryRef:
        """Write a note, or extend the existing note on this ``(kind, subject)``.

        Extending is the point. A team that learns the same thing twice should
        end up with one note carrying two dated pieces of evidence, not two
        near-duplicate notes competing in recall — that is how a corpus stays
        small enough to stay useful (V4 PRD, "Unbounded growth").

        On an extend, ``evidence`` (falling back to ``summary``) is appended to
        the timeline and the note's summary, confidence and sources are updated
        to what *this* write states: the summary is compiled truth, so the
        latest write owns it, while the timeline keeps the history.

        The note id is minted here, so a caller inside a durable handler must
        wrap this in ``Kernel.step()`` — a replay must not write a second note.
        """
        _safe_segment(subject, "subject")
        sources = list(sources or [])

        found = self._find_by_subject(kind, subject)
        if found is None:
            note = MemoryNote(team=self._team_id, kind=kind, subject=subject,
                              summary=summary, confidence=confidence,
                              sources=sources)
            if evidence:
                note = note.with_evidence(evidence,
                                          source=sources[0] if sources else None)
            path = self._directory_for(kind) / f"{note.slug}.md"
        else:
            path, existing = found
            note = existing.with_evidence(evidence or summary,
                                          source=sources[0] if sources else None,
                                          summary=summary)
            merged = list(dict.fromkeys([*note.sources, *sources]))
            note = note.model_copy(update={"sources": merged,
                                           "confidence": confidence})

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note.to_markdown(), encoding="utf-8")
        self._index[note.id] = path
        return MemoryRef.of(note)

    # ------------------------------------------------------------ reading

    async def recall(self, query: str, *, limit: int = 3,
                     kinds: list[MemoryKind] | None = None) -> list[MemoryRef]:
        """Lexical recall. Explicit, bounded, and predictable by hand.

        Ranking, in full — a reader must be able to say what comes back:

        1. **Term overlap.** Query and note are lowercased and split into
           ``[a-z0-9]+`` tokens. ``overlap`` is the fraction of distinct query
           terms found in ``note.searchable_text()`` (subject, summary and every
           timeline entry). Matching is whole-token: "multiple" does not match
           "multiples", and no stemming or stopword list is applied.
        2. **Subject boost.** ``subject`` is the fraction of distinct query
           terms found in the note's subject. The score is
           ``overlap + 0.5 * subject``, so a note whose *subject* matches always
           outranks one of equal overlap that only mentions the terms in its
           body.
        3. **Recency.** Equal scores are broken by ``updated``, newest first —
           the PRD makes ``updated`` part of ranking precisely because a team's
           domain moves. Remaining ties break on subject, so the order is
           total and stable.

        A note that scores zero — not one query term anywhere — is never
        returned; padding a result set with noise is worse than returning less.
        An empty query scores every note zero and therefore returns simply the
        most recently updated notes, which is what a caller with no question is
        asking for.

        Returns at most ``limit`` references, best first. References, never
        bodies: the caller loads a note with :meth:`get` if it wants one.
        """
        terms = _terms(query)
        scored: list[tuple[float, int, str, MemoryNote]] = []
        for path, note in self._iter_notes(kinds):
            self._index[note.id] = path
            score = _score(terms, note)
            if terms and score <= 0:
                continue
            scored.append((score, note.updated.toordinal(), note.subject, note))

        scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
        return [MemoryRef.of(note) for *_, note in scored[:max(limit, 0)]]

    async def get(self, note_id: str) -> MemoryNote:
        """Load one note, always from disk, so a hand-edit is picked up.

        Works with a cold index after a restart: the corpus is globbed the way
        ``FilesystemArtifactStore._find`` globs artifacts.
        """
        path = self._index.get(note_id)
        if path is None or not path.is_file():
            path = self._find(note_id)
        note = self._read(path) if path is not None else None
        if note is None:
            raise KeyError(f"unknown memory note: {note_id}")
        self._index[note.id] = path
        return note

    # ----------------------------------------------------------- deleting

    async def forget(self, note_id: str) -> bool:
        """Delete a note's file. It stays deleted — a rebuild cannot resurrect it.

        Poisoning is a real failure mode, so removal has to be real removal:
        the file goes, and the index only ever describes files that exist.
        """
        path = self._index.get(note_id)
        if path is None or not path.is_file():
            path = self._find(note_id)
        self._index.pop(note_id, None)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    # ------------------------------------------------------------- index

    async def rebuild_index(self) -> int:
        """Re-read the corpus and return how many notes are in it.

        Markdown is canonical, so this is a cache warm, not a recovery: the
        store answers every question from disk whether or not it has ever run.
        Unreadable files are skipped and not counted.
        """
        self._index = {note.id: path for path, note in self._iter_notes()}
        return len(self._index)

    def path_for(self, note_id: str) -> Path | None:
        path = self._index.get(note_id)
        if path is not None and path.is_file():
            return path
        return self._find(note_id)

    # ------------------------------------------------------------ internals

    def _directory_for(self, kind: MemoryKind) -> Path:
        return self._root / _DIRECTORIES[kind]

    def _iter_notes(self, kinds: list[MemoryKind] | None = None,
                    ) -> Iterator[tuple[Path, MemoryNote]]:
        """Every readable note under the root, malformed files skipped."""
        for kind in (list(MemoryKind) if kinds is None else kinds):
            directory = self._directory_for(kind)
            for path in sorted(directory.glob("*.md")):
                note = self._read(path)
                if note is not None:
                    yield path, note

    def _read(self, path: Path) -> MemoryNote | None:
        """Parse one file, or log and return ``None``.

        One corrupt file must not break recall for the whole team; a memory
        nobody can retrieve is worse than a memory somebody has to fix.
        """
        try:
            return MemoryNote.from_markdown(path.read_text(encoding="utf-8"))
        except _UNREADABLE as exc:
            logger.warning("skipping malformed memory note %s: %s", path, exc)
            return None

    def _find(self, note_id: str) -> Path | None:
        """Recover a path with a cold index, refreshing what it walks past."""
        for path, note in self._iter_notes():
            self._index[note.id] = path
            if note.id == note_id:
                return path
        return None

    def _find_by_subject(self, kind: MemoryKind, subject: str,
                         ) -> tuple[Path, MemoryNote] | None:
        """The note this ``(kind, subject)`` write should extend, if any.

        Should hand-editing ever leave two notes on one subject, the most
        recently updated one wins, so the choice stays deterministic.
        """
        matches = [(path, note) for path, note in self._iter_notes([kind])
                   if note.subject == subject]
        if not matches:
            return None
        return max(matches, key=lambda row: (row[1].updated, row[0].name))


def _terms(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _score(terms: set[str], note: MemoryNote) -> float:
    if not terms:
        return 0.0
    body = _terms(note.searchable_text())
    subject = _terms(note.subject)
    overlap = len(terms & body) / len(terms)
    boost = len(terms & subject) / len(terms)
    return overlap + _SUBJECT_WEIGHT * boost
