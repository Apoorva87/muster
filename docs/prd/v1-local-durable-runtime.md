# PRD — Agent Teams V1: Day 1 Local Durable Agent Runtime

## Goal
Build a small, reusable multi-agent runtime that runs comfortably on a local laptop under Docker/WSL and proves the core architecture in one day. It must support independent agents, targeted commands, topic subscriptions/fan-out, durable asynchronous wakeups, delayed/timer wakeups, isolated context, filesystem artifacts, human pause/resume, crash recovery, and simple human observability.

The key design rule is: **our code describes agent semantics; Restate handles distributed-systems semantics.** Do not build a scheduler, retry engine, actor runtime, broker, lease manager, or shared-chat memory system.

## Day-1 non-goals
Do NOT add Kubernetes, Kafka, NATS, Redpanda, Redis for coordination, Temporal, DBOS, a vector database, A2A, complex authentication, dynamic agent discovery, arbitrary DAG editors, sophisticated memory, Prometheus/Grafana/Tempo/Loki, or Buzz as a runtime dependency. Leave clean extension points where appropriate.

## Technology
- Python 3.12+
- Restate: durable execution, addressed invocations, retries, timers/delayed sends, wakeups, workflow state and idempotency
- PostgreSQL: canonical semantic/project data
- PydanticAI: initial LLM/agent harness; keep an adapter boundary so another harness can be used later
- Local filesystem: artifact storage
- FastAPI or similarly tiny Python web layer: local status/event viewer and human approval endpoint
- Docker Compose: local installation
- Optional local model or configured model API. Model choice must not be coupled to the coordination kernel.

## Core concepts
Implement only these first-class domain concepts in V1:

### Agent
Named capability/handler. Initial agents: `director`, `research`, `finance`, `critic`, plus a small `monitor` agent for timer testing.

### Task
A bounded unit of work. Minimum fields: `id`, `project_id`, `type`, `objective`, `assigned_agent`, `status`, `created_at`, `parent_task_id`, `input_refs`.

### Event
A small structured notification. Events contain metadata and references, not large LLM outputs or conversation transcripts.

### Subscription
Maps a logical topic to one or more agents. V1 implementation may use a PostgreSQL table plus a thin router. Public API must hide the implementation.

### Artifact
Large/meaningful agent output stored outside agent context. Minimum metadata: `id`, `project_id`, `task_id`, `type`, `path`, `created_by`, `created_at`, optional JSON metadata.

## Three primary kernel APIs
The first implementation should make these operations obvious and boring:

```python
await send(agent="finance", task="analyze", payload={...})
await publish(topic="proposal.ready", payload={...})
await wake_later(agent="monitor", delay=..., payload={...})
```

`send()` is targeted work. `publish()` is logical pub/sub fan-out. `wake_later()` is a durable future invocation. They should use Restate underneath rather than implement reliability themselves.

Also expose a small human-resume primitive for workflows waiting on approval/input.

## Commands vs events
Keep the distinction explicit:
- **Command:** a particular agent is expected to perform work.
- **Event:** something happened and zero or more subscribers may care.

Do not turn every operation into pub/sub.

## Topic/subscription implementation
For V1, do not install a message broker. Maintain logical subscriptions, e.g.:

```text
proposal.ready      -> critic
proposal.ready      -> finance
research.complete   -> director
finance.complete    -> director
critique.complete   -> director
market.changed      -> finance
market.changed      -> director
```

`publish(topic, event)` resolves the subscribers and issues durable Restate sends to them. Preserve the topic abstraction so a future bus adapter can replace this implementation without changing agent code.

## Agent execution and context
Agents must NOT inherit a shared/global transcript.

Every invocation reconstructs a bounded context from:
1. agent system instructions/role;
2. current Task;
3. selected small project state;
4. explicit Artifact references;
5. optionally the latest directly relevant event/result.

Large outputs are written as artifacts. Other agents receive artifact IDs/references and load them only when needed.

Never pass another agent's hidden scratchpad/reasoning. The critic should receive facts + proposal/artifacts, not the director/strategist's complete reasoning history.

## Durable wakeups
Demonstrate all of these:

### Direct asynchronous wakeup
Director sends work to Finance; Director need not block unless the workflow explicitly needs the result synchronously.

### Event wakeup
Publishing `proposal.ready` durably wakes all current subscribers.

### Timer wakeup
`monitor` performs a cheap deterministic check, schedules its next wakeup, and exits. There must be no continuously polling LLM.

### Human/external wakeup
A workflow can enter `WAITING_FOR_HUMAN`, consume no model tokens while waiting, and resume when the local UI sends Approve/Reject.

If an external API lacks push/webhooks and polling is unavoidable, use a Restate timer to wake a cheap deterministic checker. Only wake an LLM if something meaningful changed.

## Idempotency and side effects
All tasks/events/invocations must have stable IDs. Restate owns durable invocation/retry semantics.

Do not claim arbitrary external side effects are exactly-once. For later effectful tools, retain an `operation_id`/idempotency key and reconcile unknown outcomes before retrying. V1 only needs enough structure that this can be added cleanly in V2.

## State ownership
Keep two types of state separate:

### Restate state
Execution state: what is running/waiting, durable timers, invocation progress, workflow state.

### PostgreSQL state
Semantic state: projects, tasks, events, subscriptions, artifact metadata and human-visible run records.

Do not use a vector DB as canonical state.

## Artifact storage
V1 artifacts live under a local directory, e.g.:

```text
./data/artifacts/<project-id>/<artifact-id>.md
./data/artifacts/<project-id>/<artifact-id>.json
```

Provide a tiny `ArtifactStore` interface (`put`, `get`, metadata/reference). Initial implementation is filesystem-only. Do not add S3/MinIO on Day 1.

## Observability
Do not deploy a full observability stack on Day 1. Record a compact event/run timeline in PostgreSQL with at least:
- `run_id`
- `parent_run_id`
- `project_id`
- `task_id`
- `agent`
- `event_type`
- `started_at`
- `finished_at`
- `status`
- `duration`
- optional model/token/cost metadata
- input/output/artifact references
- error summary

Create a tiny local web page showing a chronological project timeline. Clicking a row should show structured input/output references, artifacts, timing and errors. Keep trace IDs in the schema so OpenTelemetry can be attached later without redesign.

## Human approval V1
The local page should show waiting actions with `[Approve] [Reject]`. The button sends a callback/signal that resumes the corresponding durable workflow. No polling and no LLM should remain active while waiting.

## Repository layout

```text
agent-team/
├── README.md
├── .env.example
├── docker-compose.yml
├── pyproject.toml
├── app/
│   ├── main.py
│   ├── agents/
│   │   ├── base.py
│   │   ├── director.py
│   │   ├── research.py
│   │   ├── finance.py
│   │   ├── critic.py
│   │   └── monitor.py
│   ├── kernel/
│   │   ├── runtime.py
│   │   ├── tasks.py
│   │   ├── events.py
│   │   ├── subscriptions.py
│   │   └── artifacts.py
│   ├── runtime/
│   │   ├── restate.py
│   │   └── llm.py
│   ├── db/
│   │   ├── models.py
│   │   ├── repository.py
│   │   └── migrations/
│   └── web/
│       ├── app.py
│       └── templates/
├── data/
│   └── artifacts/
└── tests/
    ├── test_send.py
    ├── test_publish.py
    ├── test_timer.py
    ├── test_context_isolation.py
    ├── test_human_resume.py
    └── test_crash_recovery.py
```

Keep `kernel/` small. If it starts becoming a large framework before the demo works, stop and simplify.

## Installation / local startup
Target both Docker Desktop and Docker under WSL2. A developer should be able to:

1. Clone repository.
2. Copy `.env.example` to `.env`.
3. Configure either a local/model-provider endpoint and credentials as applicable.
4. Run `docker compose up -d` for Restate + PostgreSQL.
5. Install Python environment (`uv sync` preferred, or documented equivalent).
6. Run database migration/init.
7. Start the Python agent service.
8. Register/discover the service with local Restate as required by the Restate SDK.
9. Start the local web UI.
10. Open one localhost URL and launch the demo.

README must contain exact copy/paste commands and a `make dev` or similarly simple convenience command if useful. Avoid scripts that hide important failures.

## Day-1 demonstration
Use a concrete scenario such as: **“Evaluate whether Company X is attractive at its current valuation.”**

Expected flow:

```text
User
  -> Director
      -> Research
      -> Finance
      -> proposal.ready
            -> Critic
      <- research.complete
      <- finance.complete
      <- critique.complete
  -> Director synthesis
  -> WAITING_FOR_HUMAN
  -> Approve/Reject
  -> complete
```

Separately:

```text
Monitor wakes
 -> performs cheap check
 -> if material change: publish market.changed
 -> schedule next wakeup
 -> exit
```

`market.changed` should wake at least two subscribers to prove fan-out.

## Crash/recovery test
This is a release criterion, not an optional demo:
1. Start a workflow.
2. Kill the Python agent process during execution/waiting.
3. Restart it.
4. Verify durable work/timer/wait state is recovered correctly and already committed durable steps are not blindly repeated.

Also restart Restate/Postgres once and document expected recovery behavior.

## Acceptance criteria
Day 1 is done when:
- one command durably wakes another agent;
- one topic wakes multiple subscribed agents;
- a timer wakes an otherwise dormant agent;
- an agent can schedule its own future wakeup;
- agents have isolated reconstructed context rather than shared transcript;
- artifacts are passed by reference;
- a workflow can sleep waiting for a human and resume from a button;
- project activity is visible in a simple timeline;
- killing/restarting the agent process does not lose durable workflow intent;
- everything runs locally without Kafka/NATS/Kubernetes/hosted infrastructure;
- README can reproduce the demo from a clean local checkout.

## Architectural extension point for V2
Define a `BusAdapter` interface now, but do **not** implement a network bus in V1. The existing Restate/local subscription implementation is the default adapter. Agent code must call the kernel (`send/publish/subscribe`) rather than Restate directly wherever practical, so V2 can connect multiple teams without rewriting agents.

## Licensing/local-first requirement
The runtime must work self-hosted locally without a paid infrastructure service. Restate is free for self-hosted use under its current source-available BSL terms; PydanticAI/Postgres and the relevant protocol libraries are locally usable. Model API charges are separate and avoidable if a compatible local model is used. Record dependency licenses in the README.
