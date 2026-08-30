# Muster team template

Copy this directory, rename it to your team id, and edit four things:

1. `team.yaml` — identity, agents, capabilities, subscriptions, public contract
2. `agents/*.py` — domain behaviour only
3. `prompts/*.md` — each agent's role
4. `tools/` — team-specific native or MCP tools

Then run `tests/test_smoke.py`. That is the whole job.

## What you must never need to edit

Restate internals, retry implementation, durable timer implementation, bus
routing, the artifact backend, generic task/event schemas, human-resume
plumbing, tracing. If a new team routinely needs to change any of these, the
runtime abstraction is wrong and should be fixed centrally — not copied around.

## Standalone vs multi-team

```env
BUS_ADAPTER=local          # default: everything resolves inside this team
```

```env
BUS_ADAPTER=restate_bus    # join a bus session
BUS_URL=http://localhost:9070
BUS_SESSION=workstation-01
TEAM_ID=myteam
```

Agent code is identical either way. `ctx.send("specialist", ...)` stays local;
`ctx.send("team://research/web-researcher", ...)` routes over the bus.

## Choosing your topology

Add an agent only when it needs a meaningfully different role, different tools
or permissions, independent context, parallelism, an adversarial second
opinion, or a different wakeup lifecycle. Otherwise use a plain function inside
an existing agent. Do not default to a swarm.
