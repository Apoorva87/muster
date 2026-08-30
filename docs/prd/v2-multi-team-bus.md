# PRD — Agent Teams V2: Day 2 Multi-Team Bus and Production Semantics

## Goal
Extend the working V1 repository without replacing its core. V2 adds a **separate bus adapter/repository/component** that can coordinate multiple independent agent teams through one bus session while preserving the lean local-first architecture.

V1 remains a complete standalone team runtime. V2 is optional infrastructure layered around it.

## Architectural rule
Agent-team code must not know whether communication is local, Restate-backed, or routed across a multi-team bus.

```text
Agent code
   |
   v
Kernel send/publish/subscribe
   |
   v
BusAdapter
   |-------------------|
   v                   v
Local/Restate       MultiTeamBus
Adapter             Adapter
```

Do not introduce Kafka/NATS/Redpanda merely because V2 is called a bus. The initial multi-team bus should remain Restate-backed unless measurements or required semantics demonstrate a real broker is necessary.

## V2 repository model
Prefer two repositories/components with clean ownership:

### Repository 1: `agent-team`
The V1 reusable team runtime. It contains agents, team-local topics, tasks, artifacts, local observability and Restate integration.

### Repository 2: `agent-bus`
A small independent service/library responsible for team registration, addressing, cross-team commands/events, routing and shared control-plane visibility.

Do not move V1's agent execution logic into `agent-bus`.

## BusAdapter contract
V1 should already contain the interface. V2 implements it fully. Suggested conceptual contract:

```python
class BusAdapter(Protocol):
    async def register_team(self, descriptor: TeamDescriptor): ...
    async def unregister_team(self, team_id: str): ...
    async def send(self, destination: Address, command: Command): ...
    async def publish(self, topic: str, event: Event): ...
    async def subscribe(self, subscription: Subscription): ...
    async def unsubscribe(self, subscription_id: str): ...
```

Do not expose Restate SDK types in this public contract.

## Addressing
Support explicit hierarchical addresses from the beginning:

```text
team://investment/director
team://investment/finance
team://security/triage
team://security/visualizer
```

An address identifies a logical destination, not a process/container/network location.

A team descriptor should minimally contain:
- `team_id`
- `version`
- `agents`
- each agent's capabilities
- subscribed/public topics
- commands accepted
- health/status metadata

## Bus session
A bus session is a logical scope through which multiple teams can be controlled/observed. It should not imply one giant shared conversation or context.

Example:

```text
BusSession: workstation-01
  |- investment-team
  |- security-team
  |- research-team
  `- visualization-team
```

Each team retains its own project/task/context/artifact boundaries. Cross-team messages carry references and explicitly allowed payloads.

## Cross-team commands
Example:

```text
investment/director
   -> send team://research/web-researcher
      research_company(X)
```

The bus resolves the logical address and durably invokes the target team through its adapter/Restate endpoint. The sender need not know where the target is hosted.

## Cross-team topics
Allow namespaced topics, for example:

```text
investment.proposal.ready
research.report.ready
security.alert.high
system.agent.failed
system.team.registered
```

Subscriptions may target team-local or bus-wide topics. Avoid wildcard/topic-pattern complexity in the first V2 implementation unless it is trivial. Exact-topic subscriptions are sufficient initially.

## Delivery model
Preserve the V1 distinction:
- targeted **Command** -> one logical destination;
- **Event** -> zero or more subscribers.

Restate remains the durability authority. The bus is routing logic, not a second independent delivery/retry system.

Every message should have stable IDs and useful correlation fields:
- `message_id`
- `session_id`
- `source_team`
- `source_agent`
- `destination` or `topic`
- `project_id` when applicable
- `task_id`
- `correlation_id`
- `causation_id`
- `created_at`
- payload/artifact references

Use a CloudEvents-compatible envelope if convenient, but do not spend Day 2 building a standards-compliance project. Preserve enough fields that CloudEvents can be emitted/consumed cleanly.

## Multiple teams and isolation
Do NOT create one global LLM memory. The bus transports commands/events/references only.

Each receiving team reconstructs its own bounded context from explicitly supplied inputs and accessible artifacts/state. A bus event should usually be hundreds of bytes or a few KB, not an entire report/transcript.

## Team registration/discovery
Keep discovery simple. On startup, a team registers a descriptor with the bus. The bus keeps a small durable registry. Teams can be listed by ID/capability.

No distributed consensus/service-discovery framework is needed for local use.

For a single laptop, all teams may be separate Python services/processes behind one local Restate server.

## Human/control-plane integration
V2 is the point to add the **Buzz adapter** if desired.

Buzz's role:
- human/agent rooms;
- meaningful progress messages;
- approvals and intervention;
- agent identities;
- searchable collaborative history;
- semantic audit trail.

Buzz is NOT the durable transaction/execution engine and Buzz/Nostr events are NOT the internal bus protocol.

Only project meaningful semantic events into Buzz, e.g.:
- task started/completed;
- proposal ready;
- critique ready;
- decision waiting for approval;
- agent/team failed;
- decision completed.

Do not mirror every tool call, retry, token or DB operation into Buzz.

If Buzz adds too much local deployment weight for Day 2, keep the adapter implemented/optional and retain the V1 local UI as the default control plane.

## Human approval across teams
Support:

```text
team workflow
 -> ApprovalRequested
 -> bus/control plane
 -> local UI or Buzz
 -> human response
 -> durable signal
 -> sleeping workflow resumes
```

No LLM polling while waiting.

## Effects/idempotency
V2 should add a first-class `Effect` abstraction for externally visible side effects.

Minimum model:

```text
Effect
  id
  task_id
  operation
  idempotency_key
  status
  request_ref
  result_ref
```

Statuses should cover at least:
`PENDING`, `SENT`, `CONFIRMED`, `UNKNOWN`, `FAILED`, optionally `COMPENSATED`.

Rule: if an external API supports idempotency keys, use the Effect ID. If the outcome is unknown and the external API does not provide idempotency, reconcile before retrying. Never equate workflow replay protection with universal exactly-once side effects.

## Observability V2
Add OpenTelemetry instrumentation without requiring a heavyweight local backend.

Propagate:
- `trace_id`
- `span_id`
- `session_id`
- `team_id`
- `agent_id`
- `project_id`
- `task_id`
- `message_id`

Allow console/file export by default. Make OTLP export configurable for users who later add Jaeger/Tempo/Grafana/etc.

The local control page should gain a team/session view:

```text
Bus Session
  investment-team  healthy  2 running  1 waiting
  research-team    healthy  0 running
  security-team    healthy  1 running
```

Clicking a team shows its V1 project/event timeline.

## Optional A2A adapter
Add A2A only as an interoperability adapter, not as the internal bus. It may expose a team/agent to external A2A clients or wrap a remote A2A agent as a bus destination.

```text
our bus <-> A2A adapter <-> external agent
```

If this threatens the Day-2 schedule, define/test the interface and defer the complete implementation.

## MCP
MCP remains agent -> tools/resources. Tool configuration belongs to individual teams/agents. The bus must not become a tool protocol.

## V2 directory structure — `agent-bus`

```text
agent-bus/
├── README.md
├── .env.example
├── pyproject.toml
├── app/
│   ├── main.py
│   ├── models/
│   │   ├── address.py
│   │   ├── message.py
│   │   ├── team.py
│   │   └── effect.py
│   ├── routing/
│   │   ├── registry.py
│   │   ├── commands.py
│   │   ├── topics.py
│   │   └── subscriptions.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── restate.py
│   │   ├── buzz.py
│   │   └── a2a.py
│   ├── observability/
│   │   └── tracing.py
│   └── web/
│       └── app.py
└── tests/
    ├── test_registration.py
    ├── test_cross_team_send.py
    ├── test_cross_team_publish.py
    ├── test_duplicate_message.py
    ├── test_team_restart.py
    └── test_effect_recovery.py
```

Keep this repository small. The bus should mostly be schemas + routing + adapters.

## Changes required in V1 repository
V2 integration should require only:
1. Implement/use `BusAdapter` abstraction.
2. Add `team.yaml` (or equivalent typed config) describing team ID, agents, capabilities and subscriptions.
3. Configure `BUS_ADAPTER=local` or `BUS_ADAPTER=remote/restate`.
4. Register the team at startup when a multi-team bus is configured.
5. Propagate bus correlation metadata through existing Task/Event/run records.

Agent business logic should not otherwise change.

## Local deployment
Target one laptop:

```text
Restate (one instance)
Postgres (one instance initially)
agent-bus service
investment-team service
second demo team service
optional Buzz stack
```

All should be runnable with Docker Compose plus local Python processes, or a single development compose file. Avoid Kubernetes.

## Day-2 demonstration
Run two distinct teams:

### Investment team
Director / Research / Finance / Critic.

### Visualization or Research helper team
A small independent team exposing one useful capability.

Demonstrate:
1. both register with one bus session;
2. bus UI lists both;
3. Investment Director sends a command to the second team;
4. second team completes asynchronously and publishes an event;
5. Investment team wakes from that event and continues;
6. a bus-wide topic wakes subscribers in more than one team;
7. kill/restart one team during work and verify durable continuation;
8. send a duplicate message ID and verify no duplicate logical work/effect;
9. human approval can pause/resume a workflow;
10. trace/correlation IDs connect the cross-team path.

## Broker decision gate
Do NOT add NATS in V2 unless a measured/required feature exists such as:
- sustained high-volume event streams;
- wildcard subject routing;
- independent long-retention event replay;
- many external non-agent consumers;
- broker-level consumer groups;
- event throughput Restate routing cannot reasonably handle.

If that threshold is reached later, implement a `NatsBusAdapter`; do not change agent APIs.

## Acceptance criteria
V2 is done when multiple independently defined V1 teams can share one bus session, address each other, publish/subscribe across team boundaries, survive process restarts, retain isolated contexts, expose useful human-visible state, and require no Kafka/NATS/Kubernetes or hosted control plane.
