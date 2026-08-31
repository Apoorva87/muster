# Muster

Muster is a local-first durable runtime for **teams of agents** — small groups of
independent, named agents that pass work to each other through commands, topic
events and durable timers, and that survive a process crash mid-flight.

The design rule the whole project is built around:

> **Our code describes agent semantics. Restate handles distributed-systems semantics.**

We do not build a scheduler, retry engine, actor runtime, broker, lease manager or
shared-chat memory system. Agents are separate minds that exchange *references*,
never a shared transcript.

## Status

**V1, V2 and V3 implemented.** Two independently defined teams coordinate over
one bus session, with the whole suite runnable on a laptop with no Docker.

| Stage | Scope | Status | Doc |
|---|---|---|---|
| V1 | Local durable agent runtime — one team | built | [PRD](docs/prd/v1-local-durable-runtime.md) · [plan](docs/superpowers/plans/2026-08-30-v1-local-durable-runtime.md) · [decisions](docs/superpowers/specs/v1-runtime-decisions.md) |
| V2 | Multi-team bus, addressing, effects, tracing | built | [PRD](docs/prd/v2-multi-team-bus.md) |
| V3 | `team.yaml` + template for any custom team | built | [PRD](docs/prd/v3-custom-teams.md) |
| V4 | Per-team memory as markdown; teams learn across projects | built | [PRD](docs/prd/v4-team-memory.md) |

A2A and Buzz ship as **interfaces only**, which both PRDs explicitly permit.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Docker (Desktop, or Engine under WSL2).

```bash
git clone https://github.com/Apoorva87/muster.git && cd muster
./setup.sh
uv run python -m app.main run "Evaluate whether Acme Corp is attractive at 31x."
```

That last command runs a whole team in-process and prints the timeline — no
Docker, no Postgres, no model endpoint. Add `--cross-team` to run two teams
over the bus, `--reject` to take the rejection path.

## Choosing a model

```bash
uv run python -m app.main providers        # what is usable on this machine
uv run python -m app.main run --provider ollama --model llama3.2:3b "..."
```

| `LLM_PROVIDER` | Kind | Needs |
|---|---|---|
| `stub` | — | nothing. Deterministic; the default |
| `anthropic` | API | `uv sync --extra anthropic` + `ANTHROPIC_API_KEY` (or `ant auth login`) |
| `openai` | API | `uv sync --extra openai` + `OPENAI_API_KEY` |
| `ollama` | API | `uv sync --extra openai` + a running Ollama; no key |
| `claude_code` | CLI agent | the `claude` binary on PATH |
| `codex` | CLI agent | the `codex` binary on PATH |

The CLI providers are not chat completions — they run a full coding agent with
its own tools and file access, and return its final answer. Same `LLMRunner`
protocol either way, so no agent code changes.

Override per agent in `team.yaml` — a cheap local model for routine work, a
stronger one for the critic:

```yaml
agents:
  research:
    entrypoint: app.agents.research
  critic:
    entrypoint: app.agents.critic
    provider: anthropic
    model: claude-opus-5
```

**It is not durable.** In-process mode dispatches sends immediately instead of
handing them to Restate, so killing it loses the work. It exists for watching
the choreography and developing agents. `make dev` is the durable path — same
agents, same `team.yaml`, only the `KernelContext` differs.

That installs the toolchain, dependencies and `.env`, then runs the 174-test
suite to prove it worked. It needs no Docker and no model endpoint — the LLM
defaults to a deterministic stub. Add `--install-uv` if you don't have uv yet,
`--help` for the rest.

To run the durable stack:

```bash
./setup.sh --durable          # adds restate-sdk, pydantic-ai, psycopg
make up                       # Restate + Postgres
make migrate                  # create schema, seed the subscription table
make dev                      # run the Muster service and register it
uv run python -m app.main web # timeline + approvals at http://localhost:8000
```

### Container engine

One `docker-compose.yml`, two engines. `make engine` reports which is in use.

| Where | Engine | Notes |
|---|---|---|
| macOS laptop | **Apple Container** | native Linux containers, no Docker Desktop |
| Server (Coolify) | **Docker** | `docker compose`, same file |

```bash
brew install container container-compose
container system start
container system kernel set --recommended
make up
```

`scripts/compose.sh` auto-detects; force one with `MUSTER_ENGINE=docker|apple`.
Ports are parameterised, so a machine already running Postgres just needs
`POSTGRES_PORT=5433 make up` — the script checks the port first and says so
rather than letting the container fail with a bare "address already in use".

Not using uv? `requirements.txt` (runtime) and `requirements-dev.txt` (with test
tooling) are generated from `uv.lock` and work with plain `pip install -r`.
Regenerate them with `./setup.sh --lock`.

`make help` lists every target. Nothing swallows errors — if a step fails you see why.

## Testing

The entire unit suite runs with **no Docker, no Restate, no Postgres**. That is
deliberate: every kernel primitive takes a `KernelContext` protocol, so tests
inject an in-memory fake that records durable sends and models journal replay.

```bash
uv run pytest                     # unit suite
uv run pytest -m integration      # needs `make up` first
```

| Suite | Proves |
|---|---|
| `test_send` | a command durably wakes one agent, replay-safe, idempotency key attached |
| `test_publish` | one topic wakes every subscriber, as distinct parallel objects |
| `test_timer` | a timer wakes a dormant agent; an agent schedules its own next wakeup |
| `test_context_isolation` | no agent ever sees another's scratchpad or reasoning |
| `test_human_resume` | a workflow parks on a human and resumes from a button |
| `test_crash_recovery` | durable intent survives losing every in-memory object |
| `test_demo_flow` | the PRD's whole Day-1 choreography, end to end |
| `test_two_team_demo` | two teams, one bus session, cross-team command and event |
| `test_team_spec` | `team.yaml` validates, loads, and projects to the bus |
| `test_effects` | reconcile-before-retry; replay protection ≠ exactly-once |
| `test_memory_learning` | a rejection becomes a note; a later project recalls it |

## Architecture

```text
agent code  ->  kernel (send / publish / wake_later)  ->  KernelContext
                                                              |
                                       +----------------------+---------------+
                                       v                                      v
                            RestateKernelContext                     FakeKernelContext
                            (durable, real SDK)                      (tests, no server)
```

Agents are Restate **Virtual Objects**, one object type per agent, each keyed by
`project_id`. Objects serialize per key and run in parallel across keys, so a
`publish()` to N subscribers wakes N distinct objects concurrently, while two
events for the *same* agent queue in order.

### Multiple teams

```text
teams/investment/team.yaml  ─┐
                             ├─→ TeamRegistry ─→ RestateBusAdapter ─→ KernelContext
teams/research/team.yaml    ─┘
```

A team is `team.yaml` + prompts + agents. `ctx.send("finance", ...)` stays
local; `ctx.send("team://research/web-researcher", ...)` routes over the bus —
the agent code is identical either way. A topic crosses a team boundary only
when the team declares it public, so team-local chatter stays local.

Cross-team artifacts travel **by reference**: the receiving team registers the
ID, and the bytes stay with the team that produced them.

## Team memory

Each team keeps a memory of markdown files in its own repository, so it improves
across projects instead of starting cold.

```bash
uv run python -m app.main memory investment    # what the team has learned
```

```text
teams/investment/memory/
├── lessons/     what worked, what did not, and why
├── domain/      durable facts about the subject matter
├── decisions/   approvals and rejections, with the reasoning
└── entities/    recurring subjects the team keeps meeting
```

**Markdown is canonical; any index is derived and disposable.** Delete the
index, rebuild it, lose nothing. A wrong memory is a bug — findable with
`grep`, fixable in an editor, revertible with `git`. That is the whole reason
for choosing files over embeddings-as-truth.

**Memory is retrieved explicitly, never injected.** An agent calls
`ctx.recall(...)` and gets *references*; the note body is loaded only by an
explicit `ctx.load_memory(ref)`, exactly as artifacts work. An uninvited memory
would be a shared transcript by another name, which is what this architecture
exists to prevent.

| `MEMORY_BACKEND` | Needs |
|---|---|
| `filesystem` | nothing. Markdown + lexical search. The default |
| `gbrain` | the `gbrain` CLI ([GBrain](https://github.com/garrytan/gbrain), TypeScript/Bun); degrades to lexical search if absent |

GBrain install (verified): `bun install -g github:garrytan/gbrain` then
`gbrain init --pglite`. It is **not** on npm — `npm install -g gbrain` fetches
an unrelated package. GBrain keeps one brain per user under `~/.gbrain`, shared
across teams, so per-team isolation is enforced by Muster's adapter: every
result is resolved back to a file under that team's root and dropped otherwise.
| `none` | nothing. The team behaves exactly as it did in V3 |

Per-agent permissions in `team.yaml` — a critic that remembers past objections
is useful; a research agent accumulating opinions usually is not:

```yaml
agents:
  critic:
    entrypoint: app.agents.critic
    memory: read-write
  research:
    entrypoint: app.agents.research
    memory: off
```

## Creating a team

```bash
cp -r template myteam        # edit team.yaml, agents/, prompts/
uv run pytest myteam/tests
```

You should never need to edit Restate internals, retry logic, durable timers,
bus routing, the artifact backend, task/event schemas, human-resume plumbing or
tracing. If a new team routinely does, the abstraction is wrong and gets fixed
centrally — see `template/README.md`.

## Why "Muster"

- *to muster* — to summon and assemble. That is exactly `send()`, `publish()` and `wake_later()`.
- *a muster roll* — the register of who belongs to a unit. That is V2's team registry and `team.yaml`.
- The word implies a **small disciplined unit with named roles**, not a swarm — which
  matches V3's explicit rule against defaulting to large agent swarms.

## Naming map

The PRDs were authored with generic working names. They are kept verbatim as the
source of record; this table is the translation to actual repository names.

| PRD name | Actual repository | Stage |
|---|---|---|
| `agent-team` | `muster` (this repo) | V1 |
| `agent-bus` | `muster-bus` (separate repo) | V2 |
| `agent-team-template` | `muster-template` | V3 |

V2's PRD calls for the bus to live in its own repository with clean ownership, so
`muster-bus` will be split out when V2 begins. Until then all three PRDs live here.

## Core concepts

| Concept | What it is |
|---|---|
| **Agent** | A named capability/handler. Receives bounded tasks, never a global transcript. |
| **Task** | A bounded unit of work with a stable ID, objective and input references. |
| **Event** | A small structured notification — metadata and references, not LLM output. |
| **Subscription** | Maps a logical topic to one or more agents. |
| **Artifact** | Large output stored *outside* agent context and passed by reference. |

## The three kernel APIs

```python
await send(agent="finance", task="analyze", payload={...})   # targeted command
await publish(topic="proposal.ready", payload={...})         # logical fan-out
await wake_later(agent="monitor", delay=..., payload={...})  # durable future call
```

Plus a human-resume primitive for workflows parked on approval. A workflow waiting
on a human consumes zero model tokens while it waits.

## Planned stack

- Python 3.12+
- [Restate](https://restate.dev) — durable execution, addressed invocations, retries, timers, wakeups
- PostgreSQL — canonical semantic state (projects, tasks, events, subscriptions, artifact metadata)
- PydanticAI — initial LLM harness, behind an adapter boundary
- Local filesystem — artifact storage
- FastAPI — local timeline viewer and approval endpoint
- Docker Compose — local install (Docker Desktop and Docker under WSL2)

Explicitly **not** on the roadmap for V1/V2: Kubernetes, Kafka, NATS, Redpanda,
Redis for coordination, Temporal, DBOS, or a vector database as canonical state.

## Repository structure

```text
.
├── docs/
│   ├── prd/                    The three roadmap PRDs (source of record)
│   └── superpowers/
│       ├── plans/              Implementation plans
│       └── specs/              Design specs
└── README.md                   This file
```

## Licensing

Muster must be runnable self-hosted with no paid infrastructure service.

| Dependency | License note |
|---|---|
| Restate | Source-available (BSL); free for self-hosted use under current terms |
| PostgreSQL | PostgreSQL License (permissive) |
| PydanticAI | MIT |
| FastAPI | MIT |

Model API charges are separate and avoidable entirely by pointing at a compatible
local model. A license for Muster itself has not been chosen yet.
