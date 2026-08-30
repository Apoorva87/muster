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

**Pre-implementation.** This repository currently contains the three product
requirement documents that define the roadmap. No runtime code exists yet.

| Stage | Scope | Doc |
|---|---|---|
| V1 | Local durable agent runtime — one team, one laptop | [`docs/prd/v1-local-durable-runtime.md`](docs/prd/v1-local-durable-runtime.md) |
| V2 | Multi-team bus, addressing, effects, tracing | [`docs/prd/v2-multi-team-bus.md`](docs/prd/v2-multi-team-bus.md) |
| V3 | Template + recipe for building any custom team | [`docs/prd/v3-custom-teams.md`](docs/prd/v3-custom-teams.md) |

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
