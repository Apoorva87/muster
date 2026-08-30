# PRD — Agent Teams V4: Team Memory

## Goal
Give each team a durable memory it can read before working and write to after
learning something, so a team improves across projects instead of starting cold
every time.

Memory is **markdown files on disk**, owned by the team, readable and editable
by a human, and versioned in Git. Any index over those files is derived and
disposable.

The key design rule is: **memory is retrieved explicitly, never injected
ambiently.** A memory that arrives uninvited is a shared transcript by another
name, and the entire architecture exists to prevent that.

## Non-goals
Do NOT add automatic context injection, cross-team memory sharing by default,
model fine-tuning, a conversation-history store, an LLM call on every write, a
second canonical datastore, or a hosted memory service. Do NOT make memory a
required dependency — a team with memory disabled must behave exactly as it
does in V3.

## Why markdown and not embeddings-as-truth
V1 states it plainly: *do not use a vector DB as canonical state.* That rule
decides this design.

- A markdown corpus can be read, diffed, reviewed, corrected and reverted. A
  float array cannot.
- A wrong memory is a bug. Bugs must be findable by a human with `grep`, and
  fixable with an editor and a commit.
- An index can be deleted and rebuilt. Canonical state cannot.

Embeddings remain useful — as a **derived index over the files**, rebuilt from
them, never the source of truth.

## Technology
- Markdown files under a per-team directory, committed to the team's repository
- A `MemoryStore` interface with two implementations:
  - **`filesystem`** — the default. Lexical search over the corpus. No new
    dependency, no service, works offline. This must remain sufficient for a
    small team.
  - **`gbrain`** — an adapter over [GBrain](https://github.com/garrytan/gbrain)
    (MIT, TypeScript/Bun, CLI + MCP). Better retrieval, hybrid lexical+semantic
    search, and a typed relationship graph.
- No new container in the compose stack. GBrain stores its index in PGLite
  in-process and is invoked as a subprocess or over MCP, exactly as the
  `claude_code` and `codex` model providers are.

### The GBrain cost, stated plainly
GBrain is a TypeScript/Bun program. A Python project reaches it across a
process boundary, which means: another runtime to install, a CLI contract that
can drift, and failures that surface as non-zero exit codes rather than
exceptions. That is why `filesystem` is the default and `gbrain` is opt-in. A
team must never be unable to run because a memory backend is missing.

## Core concepts

### Memory bank
A per-team directory of markdown files. Scoped exactly like artifacts and the
team's repository — a team's memory is its own, and crossing that boundary is a
deliberate act, not a default.

```text
teams/<team-id>/memory/
├── lessons/        what worked, what did not, and why
├── domain/         durable facts about the team's subject matter
├── decisions/      approvals and rejections, with the reasoning that led there
└── entities/       recurring subjects the team keeps encountering
```

### Memory note
One markdown file, following GBrain's **compiled truth + timeline** shape:

```markdown
---
id: mem_7f3a
team: investment
kind: lesson
subject: valuation-multiples
confidence: medium
sources: [art_a1b2, run_c3d4]
created: 2026-08-30
updated: 2026-09-14
---

# Summary

Our critic reliably objects when a proposal cites a P/E multiple without a
peer-group comparison. Include one up front.

## Timeline

- 2026-08-30 — rejected: proposal cited 31x with no comparables (run_c3d4)
- 2026-09-14 — approved after adding a peer table (run_e5f6)
```

The summary is current understanding; the timeline is the evidence that
produced it. A reader can see both what the team believes and why. Frontmatter
carries provenance so any claim can be traced back to the run that created it.

### Retrieval
An explicit call, made by an agent that decided it needed help:

```python
notes = await ctx.memory.recall("valuation multiples", limit=3)
```

Results arrive as **references**, loaded into context the same way artifacts
are — through the existing `ContextBuilder`, subject to the same size limits
and the same deny-by-default rule. Nothing enters an agent's context because a
retriever thought it might be relevant.

### Writing
Also explicit, and deliberately rarer than reading:

```python
await ctx.memory.remember(kind="lesson", subject="valuation-multiples",
                          summary="...", sources=[run.id])
```

Writes are journalled through `Kernel.step()` like any other external effect, so
a replay does not duplicate a note.

## The learning signal
The team already produces a labelled dataset and currently discards it: **every
approve/reject decision, together with the proposal and critique that led to
it.** That is a human telling the team what good looks like.

V4 turns that into memory:

```text
decision.completed ──▶ distil ──▶ memory note (kind: decision)
                                        │
                                  later ▼
                        director recalls before proposing again
```

Distillation is a small, bounded step: given the proposal, the critique and the
decision, write one note. It runs once per decision, not once per run.

## Rules every team must follow

1. **Memory is queried, never injected.** No middleware adds memories to a
   prompt. An agent asks, or it does not get any.
2. **Markdown is canonical.** Deleting the index must lose nothing.
3. **Provenance or it did not happen.** Every note names the runs and artifacts
   it came from. A note with no sources is a rumour.
4. **A team's memory is its own.** Cross-team recall requires an explicit,
   configured grant, and travels as references over the bus like everything else.
5. **Never store another agent's reasoning.** The same rule as artifacts: a
   critic's independence does not survive reading the strategist's scratchpad,
   whether it arrives fresh or from memory.
6. **Bounded by construction.** Recall returns a small number of notes within
   the existing context budget. A memory bank that grows without limit is a
   context leak with extra steps.
7. **A wrong memory is a bug.** It must be findable with `grep`, fixable with an
   editor, and revertible with `git`.

## Failure modes to design against

### Poisoning
An agent writes a confident, wrong lesson; later runs retrieve it and compound
the error. Mitigations: provenance on every note, a `confidence` field, human
review of `lessons/` in the normal code-review flow (they are files in a repo),
and the ability to delete a note and have it stay deleted.

### Memory as a back door
Retrieval that is too eager reconstructs the shared transcript the architecture
rejects. Mitigation: recall is an explicit call with a hard result limit, and
the context builder treats a memory note exactly like a foreign artifact —
denied unless referenced.

### Staleness
A team's domain moves; old notes keep asserting the old world. Mitigation: the
timeline shape makes age visible in the file itself, and `updated` is part of
retrieval ranking.

### Unbounded growth
Mitigation: notes are consolidated, not appended forever. Consolidation is a
background step a human can inspect, and it is the only place a note may be
rewritten rather than added to.

## Configuration

```env
MEMORY_BACKEND=filesystem     # filesystem | gbrain | none
MEMORY_ROOT=teams/<id>/memory
MEMORY_RECALL_LIMIT=3
MEMORY_WRITE_POLICY=decisions # decisions | explicit | off
```

`none` must be a fully supported mode: the team runs, `ctx.memory.recall`
returns nothing, and no code path changes. This is what keeps memory optional
rather than load-bearing.

Per-agent overrides live in `team.yaml` beside the existing model selection, so
a critic can be given memory while a research agent is not:

```yaml
agents:
  critic:
    entrypoint: agents.critic
    memory: read-write
  research:
    entrypoint: agents.research
    memory: off
```

## Repository layout

```text
app/kernel/memory.py          MemoryStore protocol, MemoryNote, MemoryRef
app/memory/filesystem.py      default backend: markdown + lexical search
app/memory/gbrain.py          adapter over the GBrain CLI / MCP
app/memory/distil.py          decision -> note
teams/<id>/memory/            the corpus, committed to the repo
tests/test_memory.py
tests/test_memory_isolation.py
tests/test_gbrain_adapter.py  integration-marked; needs bun + gbrain
```

## Day-4 demonstration
1. Run a project. The director proposes; the human rejects with a reason.
2. A note appears under `teams/investment/memory/decisions/` — show the file.
3. Run a second, similar project. The director recalls that note before
   proposing, and the proposal reflects it.
4. Show that the note was the *only* thing retrieved, and that it entered
   context as an explicit reference.
5. Delete the derived index. Rebuild it. Nothing is lost.
6. Edit the note by hand to correct it. The next run uses the corrected version.

## Acceptance criteria
V4 is done when:
- a team accumulates memory as markdown files in its own repository;
- an agent recalls a note only by asking, and receives references;
- nothing enters an agent's context that it did not explicitly reference;
- an approve/reject decision produces exactly one durable note with provenance;
- a note can be read, corrected and reverted by a human with an editor and Git;
- deleting the derived index loses nothing;
- `MEMORY_BACKEND=none` runs identically to V3;
- the `filesystem` backend needs no new service, no network and no model;
- the `gbrain` backend is opt-in, and its absence never blocks a team;
- memory is per-team, and cross-team recall requires an explicit grant;
- a wrong memory is findable with `grep` and fixable with a commit.

## What V4 explicitly does not decide
Whether GBrain's typed relationship graph is worth its runtime cost for agent
teams. Its predicates (`works_at`, `founded`, `invested_in`, `advises`) are
tuned for people and companies, not for tasks and decisions. Start with store
and search; adopt the graph only if retrieval quality turns out to be what
limits the team, and measure before adding it.
