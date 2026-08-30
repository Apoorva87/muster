# PRD — Agent Teams V3: Create Any Custom Agent Team

## Goal
Turn the V1 `agent-team` runtime and optional V2 `agent-bus` into a repeatable template so a developer can create a new specialist team for an arbitrary use case without understanding or modifying Restate internals.

A new team should mostly be **configuration + agent prompts/logic + tools + subscriptions**, not infrastructure work.

## Inputs
This PRD assumes these already exist:

### Repository A — `agent-team`
Reusable local team runtime from V1: Agent/Task/Event/Subscription/Artifact, Restate adapter, Postgres semantic state, filesystem artifact store, local timeline UI, timers and human pause/resume.

### Repository B — `agent-bus`
Optional V2 multi-team control/routing layer: team registration, logical addressing, cross-team commands/topics, adapters and shared observability.

A custom team must work standalone with Repository A. Repository B is optional and should be enabled only when cross-team communication is wanted.

## Developer experience target
Creating a team should eventually look approximately like:

```bash
agent-team new security-analysis
cd security-analysis
# edit team.yaml + agents/*.py + prompts/* + tools/*
agent-team dev
```

A CLI generator is optional initially; copying a documented template is acceptable. Do not make a CLI framework a prerequisite.

## Files a new team owner should normally edit

```text
my-team/
├── team.yaml                 # team identity, agents, subscriptions/capabilities
├── .env                      # local/model/tool configuration
├── agents/
│   ├── director.py           # team coordinator if required
│   ├── specialist_a.py
│   └── specialist_b.py
├── prompts/
│   ├── director.md
│   ├── specialist_a.md
│   └── specialist_b.md
├── tools/
│   └── ...                   # team-specific native/MCP tool adapters
├── workflows/
│   └── ...                   # only explicit domain workflows that are useful
└── tests/
    └── test_team_scenario.py
```

The developer should normally NOT edit Restate/runtime, retry, scheduling, routing, artifact, database or bus internals.

## `team.yaml`
Define the team's public contract declaratively. Suggested shape:

```yaml
team:
  id: investment
  version: 1
  description: Investment research and adversarial analysis

agents:
  director:
    entrypoint: agents.director
    capabilities: [coordinate, synthesize]

  finance:
    entrypoint: agents.finance
    capabilities: [valuation, financial_analysis]

  critic:
    entrypoint: agents.critic
    capabilities: [challenge, adversarial_review]

subscriptions:
  - topic: proposal.ready
    agent: critic
  - topic: market.changed
    agent: finance

public:
  commands:
    - evaluate_company
  topics:
    - investment.analysis.completed
```

Exact schema may evolve, but keep it typed, small and human-readable.

## Creating an agent
Each agent implementation should define only domain behavior. Conceptually:

```python
@agent("critic")
async def critic(ctx: AgentContext, task: Task):
    proposal = await ctx.artifacts.get(task.input_refs["proposal"])

    result = await ctx.llm.run(
        instructions=ctx.prompt("critic"),
        input=proposal,
        tools=ctx.tools,
    )

    artifact = await ctx.artifacts.put(result)

    await ctx.publish(
        "critique.complete",
        {"artifact_id": artifact.id},
    )
```

The framework supplies task IDs, run metadata, durable invocation, error/retry behavior, artifact plumbing and event routing.

## Agent design rules
Every custom team must follow these rules:

1. Agents receive bounded tasks, not global transcripts.
2. Large outputs become artifacts and are passed by reference.
3. Commands target a specific logical agent/team.
4. Events describe facts that occurred and may fan out.
5. LLMs do reasoning; deterministic software does routing/scheduling/retries.
6. No LLM polling loops.
7. Timed monitoring uses `wake_later`/schedule primitives.
8. External callbacks/human responses wake sleeping workflows.
9. Never expose another agent's hidden scratchpad as coordination state.
10. Keep canonical project knowledge structured and external to context.

## Choosing team topology
Do not create agents merely to simulate job titles. Add an agent when one of these is true:
- it needs a meaningfully different system role/perspective;
- it requires different tools/permissions;
- independent context improves reasoning;
- work can run in parallel;
- an adversarial/independent evaluation is valuable;
- it has a different wakeup/subscription lifecycle.

Otherwise use ordinary functions/tools inside an existing agent.

### Example minimal team

```text
Director
  |- Specialist
  `- Critic
```

Often sufficient.

### Example parallel team

```text
             Director
        /       |       \
 Research   DomainExpert  Critic
        \       |       /
             synthesis
```

Do not default to large swarms.

## Adversarial agent pattern
For an independent critic:

```text
facts/objective ------> Strategist
       |                    |
       |                 proposal
       |                    |
       `--------------> Critic
                            |
                         objections
                            |
                            v
                         Director
```

Critic receives facts + proposal/evidence, not Strategist scratchpad. This preserves useful independence and reduces context bloat.

## Context builder
Every team can customize a `ContextBuilder`, but defaults should be sufficient. It should assemble:
- agent instructions;
- task objective;
- relevant structured project fields;
- explicit artifact inputs;
- narrowly relevant prior results.

It must enforce configurable size limits and log which references were loaded. Avoid automatically vector-searching all historical messages.

## Tools
Tools may be native Python or MCP-backed.

Use MCP for interoperable external tools/resources when appropriate. MCP is not agent-to-agent communication.

Tool permissions should be assigned per agent. Example:

```text
ResearchAgent -> web/search/read-only tools
FinanceAgent  -> financial-data/read-only tools
Executor      -> side-effectful tool, approval required
```

Do not give every agent every tool.

## Timed/autonomous work
Any agent may request a durable future wakeup:

```python
await ctx.wake_later(
    delay=timedelta(hours=6),
    reason="recheck market conditions",
    payload={...},
)
```

The LLM/process exits. Restate owns the timer. When the timer fires, a new bounded invocation is constructed.

For recurring checks, the agent/checker may schedule its next invocation. Prefer a deterministic cheap checker before waking an LLM when polling an external source is unavoidable.

## Standalone mode
Default new-team mode:

```env
BUS_ADAPTER=local
```

All `send/publish/subscribe` operations stay inside the team using the V1 adapter. No V2 bus service is required.

## Multi-team mode
To join the V2 bus:

```env
BUS_ADAPTER=restate_bus
BUS_URL=http://localhost:...
BUS_SESSION=workstation-01
TEAM_ID=investment
```

At startup the team registers its `team.yaml` descriptor. Its existing agent code remains unchanged.

Then a local command:

```python
await ctx.send("finance", ...)
```

still resolves locally, while:

```python
await ctx.send("team://research/web-researcher", ...)
```

routes through the bus.

Likewise, explicitly public/namespaced events may fan out across teams.

## A2A interoperability
If an external agent is A2A-only, configure an A2A adapter in V2. Do not modify the custom team's internal architecture. Treat the remote agent as another logical destination/capability.

## Human observability
Standalone teams use the V1 local timeline/approval page.

When Buzz is configured through V2, selected semantic events may be projected to Buzz. Team authors can mark events as human-visible, but should not use Buzz messages as machine memory.

## Artifacts
Default local backend is filesystem. Team code always accesses artifacts through `ctx.artifacts`; never hard-code filesystem paths into inter-agent messages.

This permits a later S3-compatible backend without changing agents.

## Adding domain state
If a team needs domain-specific structured state, add ordinary PostgreSQL models/tables owned by that team. Do not force every domain fact into generic framework tables.

Generic runtime tables remain generic; domain schemas remain domain-specific.

## Effects and approvals
For side-effectful operations (trade, deploy, send message, modify remote state):
1. create an Effect with stable operation/idempotency ID;
2. require approval where policy demands it;
3. execute through a restricted tool/agent;
4. persist result/unknown status;
5. reconcile ambiguous outcomes before retrying.

Read-only research teams may ignore this machinery.

## Testing a new team
Every new team must include one end-to-end scenario exercising its intended topology.

Minimum generic tests:
- team config validates;
- every configured agent loads;
- subscriptions reference valid agents;
- direct command works;
- expected event fan-out works;
- artifact references resolve;
- context builder does not include unrelated transcript/history;
- timed wakeup works if used;
- human pause/resume works if used;
- process restart during a representative workflow recovers correctly.

## Template repository structure
A reusable starter should look like:

```text
agent-team-template/
├── README.md
├── team.yaml
├── .env.example
├── pyproject.toml
├── agents/
│   ├── director.py
│   └── specialist.py
├── prompts/
│   ├── director.md
│   └── specialist.md
├── tools/
├── workflows/
├── domain/
├── tests/
│   └── test_smoke.py
└── docker-compose.override.yml   # only if team needs extra local services
```

The common runtime should be consumed as a package/dependency rather than copied and modified in every team once V1 stabilizes.

## New-team recipe
1. Copy/generate template.
2. Choose a short stable `team.id`.
3. Write the team's objective and external/public capabilities.
4. Define the smallest useful agent topology.
5. Write each agent's role/prompt.
6. Assign only required tools/permissions.
7. Define direct commands and topic subscriptions in `team.yaml`.
8. Define any domain-specific PostgreSQL state.
9. Implement agent handlers using `ctx.send`, `ctx.publish`, `ctx.artifacts`, and `ctx.wake_later`.
10. Add one realistic end-to-end scenario.
11. Run standalone locally and inspect timeline/context/artifacts.
12. Run crash/recovery test.
13. If cross-team functionality is needed, switch BusAdapter configuration and register with V2 bus.
14. Optionally expose selected capabilities through A2A and selected semantic events through Buzz.

## What must never need editing for a normal new team
- Restate internals
- retry implementation
- durable timer implementation
- bus routing implementation
- generic artifact backend
- generic task/event schemas
- human-resume plumbing
- tracing/correlation plumbing

If new teams routinely need to modify these, the V1/V2 abstraction is wrong and should be fixed centrally rather than copied.

## Acceptance criteria
V3 is successful when a developer can create a materially different team—e.g. security analysis, investment research, software architecture review, website monitoring—by editing team configuration, prompts, agents and tools, while reusing the same runtime and optionally the same multi-team bus. A new team should run standalone on a laptop and be connectable to the shared bus without rewriting its domain agents.
