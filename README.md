# Muster

A local-first durable runtime for **ensembles of agents** — small teams of
independent, named agents that pass work to each other through commands, topic
events and durable timers, survive a crash mid-flight, park on a human when a
decision is needed, and learn from what that human decided.

The design rule the whole project is built on:

> **Our code describes agent semantics. Restate handles distributed-systems
> semantics.**

No scheduler, retry engine, actor runtime, broker, lease manager or shared-chat
memory is written here. Agents are separate minds that exchange *references* —
never a shared transcript.

```
787 tests. They run with no Docker, no Restate, no Postgres and no model —
8 skip unless you install an optional extra, the rest pass.
```

---

## Quick start

Needs [uv](https://docs.astral.sh/uv/). Nothing else.

```bash
git clone https://github.com/Apoorva87/muster.git && cd muster
./setup.sh
uv run python -m app.main run "Is Acme Corp attractive at 31x earnings?"
```

`setup.sh` installs the toolchain, dependencies and `.env`, then runs the suite
to prove it worked. The last command runs a whole team of agents and prints its
timeline — no Docker, no database, no model endpoint, no API key.

Three more things worth running before reading further:

```bash
make demo            # two independent teams coordinating over a bus
make buzz-demo       # a team driven entirely from a chat room
uv run python -m app.main providers    # which models this machine can use
```

---

## What is actually happening

A project runs like this:

```
you ──▶ director ──┬──▶ research  ──▶ research.complete ──┐
                   └──▶ finance   ──▶ finance.complete  ──┤
                                                          ▼
                                             director assembles a proposal
                                                          │
                                              proposal.ready
                                                          ▼
                                                       critic ──▶ critique.complete
                                                          │
                                              director synthesises
                                                          ▼
                                           ⏸ WAITING_FOR_HUMAN
                                                          │
                                              you approve / reject
                                                          ▼
                                        a memory note the next project recalls
```

Every arrow is a durable invocation. Kill the process anywhere along it and the
work resumes.

---

## The five concepts

| Concept | What it is |
|---|---|
| **Agent** | A named capability. Receives a bounded task, never a transcript |
| **Task** | One unit of work: stable id, objective, explicit input references |
| **Event** | "This happened." Metadata and references — never LLM output |
| **Subscription** | Maps a topic to one or more agents. Fan-out is many of these |
| **Artifact** | A large output stored *outside* context and passed by reference |

And the three APIs agents actually call:

```python
await ctx.send("finance", "analyze", objective=...)      # a targeted command
await ctx.publish("proposal.ready", {"artifact_id": ...}) # fan out to subscribers
await ctx.wake_later(timedelta(hours=6), reason="...")    # a durable future call
decision = await ctx.request_approval(task, "Approve?")   # park on a human
```

Plus, when a team has memory:

```python
notes = await ctx.recall("valuation multiples")           # explicit, never injected
await ctx.remember(kind=..., subject=..., summary=...)    # what we learned
```

---

## Architecture

```
agent code
    │  (never imports Restate, never imports the bus)
    ▼
kernel  send / publish / wake_later / request_approval
    │
    ▼
KernelContext ──────────┬──────────────────────┐
                        ▼                      ▼
             RestateKernelContext        FakeKernelContext
             durable, real SDK           tests, no server
```

That single seam is why the whole suite runs with no infrastructure, and it is
the same seam the multi-team bus plugs into.

Agents are Restate **Virtual Objects**, one object type per agent, each keyed by
`project_id`. Objects serialize per key and run in parallel across keys — so a
`publish()` to N subscribers wakes N distinct objects concurrently, while two
events for the *same* agent queue in order.

### Multiple teams

```
teams/investment/team.yaml ─┐
                            ├─▶ TeamRegistry ─▶ RestateBusAdapter ─▶ KernelContext
teams/research/team.yaml   ─┘
```

`ctx.send("finance", ...)` stays local. `ctx.send("team://research/web-researcher", ...)`
routes over the bus. **The agent code is identical either way.** A topic crosses
a team boundary only when the team declares it public, so team-local chatter
stays local. Cross-team artifacts travel by **reference** — the bytes stay with
the team that produced them.

---

## Running it

Two modes. They share the agents, the `team.yaml` and the kernel; only the
`KernelContext` differs.

```
in-process                            durable
uv run python -m app.main run "..."   make up
                                      uv run python -m app.main migrate
                                      make dev
no Docker, no Restate, no Postgres    Restate + Postgres
NOT durable — kill it, work is gone   survives crashes and restarts
for developing and demoing            for anything you care about
```

`migrate` is not optional on the durable path: it creates the schema **and**
seeds the subscription table. Skip it and `publish()` resolves to nobody — the
team appears to run while quietly doing nothing.

Useful flags: `--cross-team` (two teams over the bus), `--reject` (the rejection
path), `--provider` / `--model`, `--memory`.

### Container engine

One `docker-compose.yml`, two engines. `make engine` reports which is in use.

| Where | Engine |
|---|---|
| macOS laptop | **Apple Container** — native Linux containers, no Docker Desktop |
| Server (Coolify) | **Docker** — `docker compose`, same file |

```bash
brew install container container-compose
container system start && container system kernel set --recommended
make up
```

`scripts/compose.sh` auto-detects; force one with `MUSTER_ENGINE=docker|apple`.
Ports are parameterised, so a machine already running Postgres needs only
`POSTGRES_PORT=5433 make up` — the script checks the port first and says so
rather than letting the container fail with a bare "address already in use".

---

## Choosing a model

```bash
uv run python -m app.main providers
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
its own tools and file access and return its final answer. Same `LLMRunner`
protocol either way, so no agent code changes.

Set it per agent in `team.yaml` — a cheap local model for routine work and a
stronger one for the critic:

```yaml
agents:
  research:
    entrypoint: app.agents.research
  critic:
    entrypoint: app.agents.critic
    provider: anthropic
    model: claude-opus-5
    memory: read-write
```

---

## Team memory

Each team keeps a memory of markdown files in its own directory, so it improves
across projects instead of starting cold.

```bash
uv run python -m app.main memory investment
```

```text
teams/investment/memory/
├── lessons/     what worked, what did not, and why
├── domain/      durable facts about the subject matter
├── decisions/   approvals and rejections, with the reasoning
└── entities/    recurring subjects the team keeps meeting
```

**Markdown is canonical; any index is derived and disposable.** Delete the
index, rebuild it, lose nothing. A wrong memory is a bug — findable with `grep`,
fixable in an editor, revertible with `git`. That is the whole reason for
choosing files over embeddings-as-truth.

**Memory is retrieved explicitly, never injected.** `ctx.recall(...)` returns
*references*; the body comes from an explicit `ctx.load_memory(ref)`, exactly as
artifacts work. An uninvited memory would be a shared transcript by another
name, which is what this architecture exists to prevent.

The learning signal is one the team already produces: **every approve/reject
decision, with the proposal and critique that led to it.**

| `MEMORY_BACKEND` | Needs |
|---|---|
| `filesystem` | nothing. Markdown + lexical search. The default |
| `gbrain` | the `gbrain` CLI; degrades to lexical search if absent |
| `none` | nothing. The team behaves exactly as it did before memory existed |

GBrain install: `bun install -g github:garrytan/gbrain` then `gbrain init
--pglite`. It is **not** on npm. One brain per user at `~/.gbrain`, partitioned
by **source** (`muster-<team>`), created non-federated so a partition is only
searched when explicitly named.

---

## Driving it from chat

```bash
make buzz-demo
```

[Buzz](https://github.com/block/buzz) is Block's open-source Nostr workspace
where humans and agents share channels. A Muster team posts progress into a room
and takes commands back:

```
you       run Evaluate whether Acme Corp is attractive at 31x earnings
director  ▶ director started
research  ▶ research started
director  📋 proposal ready — art_dd3af1a7
critic    ⚔ critique ready — art_65b65bec
director  ⏸ needs your decision → reply approve or reject
you       approve
director  🏁 decision recorded: approve — project complete
```

Each agent posts under **its own keypair**, so the room shows who actually
spoke. Only *semantic* events reach the room — internal events, tool calls,
retries and token counts never do, and artifacts cross as references, never
bodies.

**Buzz is a control plane, not the transport.** Durable coordination stays on
Restate; if the relay goes down the team keeps working and the room goes quiet.

The demo runs a real NIP-01/29/42 relay in-process, so `--relay ws://your-buzz`
runs the identical code path against a real deployment.

---

## Creating your own team

Two skills, which chain:

```bash
/muster-new      # interviews you, critiques the topology, generates teams/<id>/
/muster-buzz     # puts that team in a chat room
```

**Installing them.** Inside this repo there is nothing to install — Claude Code
reads `.claude/skills/` from the project it was started in, so `/muster-new`
works as soon as you `cd` here. To use them from any other directory:

```bash
make install-skills      # symlinks .claude/skills/* into ~/.claude/skills/
```

Symlinks, not copies, so `git pull` updates the skills and nothing drifts. The
target refuses to overwrite a real directory of the same name, and
`make uninstall-skills` removes only links pointing back into this checkout.
Type `/muster-` to check they resolved; if they don't appear, restart Claude
Code, since skills are scanned at startup. For a harness with no native skill loading
(Codex, for instance), there is nothing to install either — point it at
`.claude/skills/muster-new/SKILL.md` and tell it to follow the file.

`muster-new` asks eleven questions in three batches — the shape of the work, how
it runs (which model per agent, whether it should learn), and where the human
sits — then **argues against your topology before writing anything**. It refuses
job-title agents and swarms: add an agent only when it needs a different role,
different tools, independent context, parallelism, adversarial review, or a
different wakeup lifecycle.

Or copy the template by hand:

```bash
cp -r template teams/myteam    # edit team.yaml, agents/, prompts/
uv run pytest teams/myteam/tests
```

You should never need to edit Restate internals, retry logic, durable timers,
bus routing, the artifact backend, task/event schemas, human-resume plumbing or
tracing. If a new team routinely does, the abstraction is wrong and gets fixed
centrally — see `template/README.md`.

---

## Where to watch, and when to step in

| Surface | Shows | Start it |
|---|---|---|
| **Timeline** | Every run in order, with timing, refs and errors. Approve/Reject live here | `uv run python -m app.main web` → :8000 |
| **Memory** | What the team has learned | `uv run python -m app.main memory <team>` |
| **Bus session** | Registered teams, health, running/waiting counts, cross-team routing | `bus/web/app.py` |
| **Buzz room** | Semantic progress, and you can type `run …` / `approve` back | `make buzz-demo` |
| **Restate UI** | Invocations, journals, retries — when durability itself misbehaves | :9070 |

You must intervene at four moments. Three are by design:

1. **An approval parked the workflow.** It burns no tokens and can wait days
   across restarts. Nothing proceeds until you answer — that is the point.
2. **An `UNKNOWN` effect needs reconciling.** A side effect whose outcome is
   unknown will not retry blindly: replay protection is not the same as
   exactly-once in the world.
3. **A memory note is wrong.** Fix the markdown and commit.

The fourth is a failure: `system.agent.failed` / `system.team.failed`.

---

## Testing

The whole unit suite runs with **no Docker, no Restate, no Postgres and no
model** — deliberately, because every kernel primitive takes a `KernelContext`
protocol and tests inject a fake that records durable sends and models journal
replay.

```bash
uv run pytest                     # 787 tests
uv run pytest -m integration      # needs `make up`, or a real gbrain/Buzz
make demo                         # two teams over a bus
```

| Suite | Proves |
|---|---|
| `test_send` / `test_publish` / `test_timer` | the three kernel APIs, replay-safe |
| `test_context_isolation` | no agent ever sees another's scratchpad |
| `test_human_resume` | a workflow parks on a human and resumes |
| `test_crash_recovery` | durable intent survives losing every in-memory object |
| `test_demo_flow` | the whole single-team choreography, end to end |
| `test_two_team_demo` | two teams, one bus session, cross-team command and event |
| `test_effects` | reconcile-before-retry; replay protection ≠ exactly-once |
| `test_memory_learning` | a rejection becomes a note; a later project recalls it |
| `test_buzz_demo` | a team driven entirely from a chat room |

---

## Repository layout

```text
app/                the team runtime
├── kernel/         send/publish/wake_later, tasks, events, artifacts, memory,
│                   context builder, lineage, team.yaml — the small core
├── agents/         director, research, finance, critic, monitor
├── memory/         filesystem + gbrain backends, decision distillation
├── runtime/        LLM providers, Restate wiring
├── db/             SQLAlchemy models and repository
├── web/            timeline UI and approvals
├── launcher.py     the one way work begins
└── local_runner.py in-process execution

bus/                multi-team coordination
├── models/         addresses, message envelope, team descriptors, effects
├── routing/        registry, command and topic routers, effect execution
├── adapters/       Restate bus, Buzz control plane, A2A (interface only)
├── nostr/          NIP-01 events, relay client, local dev relay
└── web/            bus session view

teams/              real teams: investment, research
template/           what a new team copies
demo/               the Buzz session demo
docs/prd/           the four PRDs this was built from
.claude/skills/     muster-new, muster-buzz
CLAUDE.md           the map for a coding agent: rules, quirks, what is unfinished
```

If you are pointing a coding agent at this repo, `CLAUDE.md` is what it should
read first — it carries the durable-execution rules and the environment quirks
that cost real time to rediscover. `tests/test_claude_md.py` keeps it honest.

---

## Status

| Stage | Scope | Status |
|---|---|---|
| V1 | Local durable agent runtime — one team | built |
| V2 | Multi-team bus, addressing, effects, tracing | built |
| V3 | `team.yaml` + template for any custom team | built |
| V4 | Per-team memory as markdown; teams learn across projects | built |

The [PRDs](docs/prd/) are the authored source of record. A2A and Buzz's optional
projection ship as **interfaces only**, which both PRDs explicitly permit.

**Not yet done:** a start button in the web UI (the launcher exists and is
tested; the button is not wired), and the live kill/restart crash test has not
been executed against a running server.

---

## Why "Muster"

*To muster* is to summon and assemble — which is exactly `send()`, `publish()`
and `wake_later()`. A *muster roll* is the register of who belongs to a unit —
which is the team registry. And the word implies a small disciplined unit with
named roles, not a swarm, which is the topology rule the whole project defends.

## Licensing

Muster must run self-hosted with no paid infrastructure service.

| Dependency | License |
|---|---|
| Restate | Source-available (BSL); free for self-hosted use |
| PostgreSQL | PostgreSQL License |
| Pydantic AI / FastAPI / SQLAlchemy | MIT / MIT / MIT |
| GBrain (optional) | MIT |
| Buzz (optional) | Apache-2.0 |

Model API charges are separate and avoidable entirely with a local model.
**A license for Muster itself has not been chosen yet.**
