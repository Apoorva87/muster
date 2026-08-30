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

**V1 implemented.** The kernel, agents, artifacts, persistence, timeline UI and
the full Day-1 demo choreography are built and tested. See
[`docs/prd/v1-local-durable-runtime.md`](docs/prd/v1-local-durable-runtime.md).

| Stage | Scope | Doc |
|---|---|---|
| V1 | Local durable agent runtime — one team, one laptop | [PRD](docs/prd/v1-local-durable-runtime.md) · [plan](docs/superpowers/plans/2026-08-30-v1-local-durable-runtime.md) · [decisions](docs/superpowers/specs/v1-runtime-decisions.md) |
| V2 | Multi-team bus, addressing, effects, tracing | [PRD](docs/prd/v2-multi-team-bus.md) |
| V3 | Template + recipe for building any custom team | [PRD](docs/prd/v3-custom-teams.md) |

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Docker (Desktop, or Engine under WSL2).

```bash
git clone https://github.com/Apoorva87/muster.git && cd muster
./setup.sh
```

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
