---
name: muster-new
description: Bootstrap a new Muster agent team — interview the user, critique the proposed topology against V3's rules, then generate and verify teams/<id>/. Use when the user says "new muster team", "create an agent team", "bootstrap a team", "add a team to muster", "scaffold agents", or asks which agents a job needs.
---

# Bootstrap a new Muster team

A new team is **configuration + prompts + agent logic**. The runtime already exists.
You are running an interview and a generator, not designing a runtime.

Read before you start — these are the output contract:

- `docs/prd/v3-custom-teams.md` — the rules you are enforcing.
- `template/README.md` and `template/team.yaml` — what gets copied.
- `teams/research/team.yaml` and `teams/research/agents/web_researcher.py` — a real
  one-agent team. Note what its agent imports: `app.agents.base` and
  `app.kernel.models`, nothing else.
- `app/kernel/team_spec.py` — the schema you must emit and the errors it raises.

Supporting detail: `reference/topology.md` (decision rules + worked examples),
`reference/checklist.md` (required tests + the never-edit list).

---

## Step 1 — Interview, in one batch

Ask these seven. Use `AskUserQuestion` if the harness offers it: two calls of
roughly four questions, never one question per turn. If the user already answered
something in their opening request, do not ask it again — restate your reading of it
and move on.

1. **What job does this team do?** One sentence. Becomes `team.description`.
   Also: a short stable slug for `team.id` (lowercase, alphanumeric plus `-`/`_`).
2. **What does "done" look like?** What artifact, decision, or published fact ends a
   run? This becomes the terminal `publish()` and the team's public topic.
3. **What must a human approve?** Name the specific decisions, or say "nothing".
   Each one is a `ctx.request_approval` park point, not a whole agent.
4. **What is read-only and what is side-effectful?** Anything that writes to the
   outside world — trade, deploy, send, delete, file a ticket — is side-effectful and
   gets an Effect with an idempotency key plus a restricted executor.
5. **What external data or tools are needed, and which agent needs each?** Tool
   permissions are per agent. Do not give every agent every tool.
6. **What runs on a timer, or is woken by an outside event?** Interval, and what the
   cheap deterministic check is. An LLM must never poll — see `app/agents/monitor.py`.
7. **Who else calls this team or listens to it?** Fills `public.commands` and
   `public.topics`. Empty is a fine answer; standalone is the default.

## Step 2 — Critique the topology, before writing anything

This step is not optional and does not get skipped because the user sounded certain.

Propose the **smallest** topology that does the job, then argue it against V3's rule,
quoted to the user verbatim:

> Do not create agents merely to simulate job titles. Add an agent when one of these
> is true:
> - it needs a meaningfully different system role/perspective;
> - it requires different tools/permissions;
> - independent context improves reasoning;
> - work can run in parallel;
> - an adversarial/independent evaluation is valuable;
> - it has a different wakeup/subscription lifecycle.
>
> Otherwise use ordinary functions/tools inside an existing agent.

For **every** agent you propose, say which clause justifies it. An agent that
satisfies no clause is a function.

Push back explicitly on these three, by name:

- **Job-title agents.** "We need a PM agent / a QA agent / a writer agent" describes
  an org chart, not a context boundary. Ask what different *tools*, *permissions* or
  *independent context* it has. If the answer is none, it is a prompt section.
- **Swarms.** V3: "Do not default to large swarms." Five agents means five context
  reconstructions, five model calls and five failure modes per run. Start at one or
  two and let a real limitation add the third.
- **Reformatter agents.** An agent whose only job is to take another agent's output
  and restate it — summarise, tidy, render as markdown — buys nothing. It has the same
  tools, the same lifecycle and strictly less context. Make it a function.

Then say plainly: **"I am not creating X, because Y"** for each rejected agent, and
name the function or prompt section that absorbs it instead.

Present the final shape as a tree plus a one-line justification per agent, and the
subscription list. **Get explicit agreement before writing any file.** If the user
insists on an agent you argued against, build it — but record the reason in the team's
`team.yaml` comment so the next reader knows it was a deliberate choice.

## Step 3 — Generate

```bash
cp -R template teams/<id>
```

Then, in `teams/<id>/`:

- **`team.yaml`** — rewrite. `team.id` is the slug; entrypoints are
  `teams.<id>.agents.<module>`; every agent named in `subscriptions` must be declared
  in `agents`; namespace every entry in `public.topics` with the team id.
- **`teams/<id>/agents/*.py`** — one module per agent, `@agent("<name>", team="<id>")`. Delete
  `teams/<id>/agents/director.py` if the team has no coordination to do; a one-agent team is a
  real answer (`teams/research/` is one). **Every agent module may import only from
  `app.agents.base` and `app.kernel.models`** (plus stdlib and `__future__`). Importing
  `restate` or `bus` from team code is a bug the suite catches.
- **`teams/<id>/agents/__init__.py`** — empty. `load_team_spec(...).load_entrypoints()` imports
  each module by its entrypoint path; a re-export here just duplicates registration.
  The template ships one that imports `myteam.agents` — blank it out.
- **`teams/<id>/prompts/<agent>.md`** — filename must match the string passed to `ctx.prompt()`,
  which is the agent name. Say what the agent sees and what it must produce
  standalone; never tell it about another agent's reasoning.
- **`teams/<id>/tests/test_smoke.py`** — set `TEAM_DIR = "teams/<id>"` (the template ships `"."`,
  which does not resolve from the repo root), keep the four generic checks, and add
  one end-to-end scenario for the topology you just agreed. Work through
  `reference/checklist.md` for what else the team owes given its answers to Q3–Q6.
- Add `teams/<id>/__init__.py` if `cp -R` did not bring one.

Leave `teams/<id>/tools/`, `teams/<id>/workflows/` and `teams/<id>/domain/` empty until something needs them.

Worked `team.yaml` — a security-triage team, two agents, one human gate:

```yaml
team:
  id: triage
  version: 1
  description: Triage inbound security findings and propose a remediation

agents:
  analyst:
    entrypoint: teams.triage.agents.analyst
    capabilities: [assess_finding, reproduce]
  reviewer:
    # Adversarial second opinion on severity. Different perspective and
    # independent context — not a job title.
    entrypoint: teams.triage.agents.reviewer
    capabilities: [challenge, adversarial_review]
    provider: anthropic
    model: claude-opus-5

subscriptions:
  - {topic: finding.received, agent: analyst}
  - {topic: assessment.ready, agent: reviewer}

public:
  commands: [assess_finding]
  topics: [triage.assessment.reviewed]
```

Per-agent `provider:`/`model:` is optional; unset inherits the deployment default.
Available providers: `stub`, `anthropic`, `openai`, `ollama`, `claude_code`, `codex`
— see `app/runtime/llm.py`.

The agent shape to copy, from `teams/research/agents/web_researcher.py`:

```python
from __future__ import annotations

from app.agents.base import AgentContext, agent
from app.kernel.models import Task

TEAM = "triage"


@agent("analyst", team=TEAM)
async def analyst(ctx: AgentContext, task: Task) -> str:
    result = await ctx.llm.run(
        instructions=ctx.prompt("analyst"),
        input=task.objective,
        agent="analyst",
    )
    ref = await ctx.put_artifact(task=task, created_by="analyst",
                                 type="assessment", content=result)
    await ctx.publish("assessment.ready", {"artifact_id": ref.id})
    return ref.id
```

## Step 4 — Verify, and show the output

Run both. Do not claim success without pasting what they printed.

```bash
uv run python -c "from app.kernel.team_spec import load_team_spec; load_team_spec('teams/<id>').load_entrypoints()"
uv run pytest teams/<id>/tests -q
```

`load_team_spec` raises `SpecError` on: a subscription naming an undeclared agent, a
team with no agents, a duplicate `(topic, agent)` pair, a `team.id` that is not a
slug, and an entrypoint that will not import. Read the message — it names the fix.

Then confirm nothing else broke:

```bash
uv run pytest -q
```

## Step 5 — Hand off

```bash
uv run python -m app.main run --team <id> "<a real objective>"
uv run python -m app.main providers      # what models this machine can actually use
```

Note: `--team` is the V3 target shape and is not wired into `app/main.py` yet — today
`app.main run` drives the built-in demo team. Until it is, drive a new team with
`uv run pytest teams/<id>/tests -q`, or with `LocalRunner` + `drive` from
`app/local_runner.py`, which is what the CLI itself uses.

Tell them:

- which agent to send the opening command to, and the task name;
- which topics fan out to whom;
- where an approval will park (the timeline UI, `uv run python -m app.main web`);
- that in-process mode is **not durable** — `make dev` is the durable path, with the
  same agents and the same `team.yaml`;
- to set the model per agent in `team.yaml` rather than globally, e.g. a cheap local
  model for routine work and a stronger one for the critic.
