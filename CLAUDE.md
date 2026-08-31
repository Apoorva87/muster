# CLAUDE.md — Muster

Muster is a local-first durable runtime for **ensembles of agents** that work and
communicate. Agents call a small kernel (`send` / `publish` / `wake_later` / `recall`);
Restate makes those calls survive crashes.

**Read in this order:** this file → `README.md` (how a human uses it) →
`docs/prd/v1-local-durable-runtime.md` before touching kernel code. V2/V3/V4 PRDs sit
beside it and define the seams V1 must leave open.

## Orientation — what is here

| Path | Owns |
|---|---|
| `app/kernel/` | The whole public surface. `runtime.py` (Kernel), `context.py` (the seam, below), `models.py`, `artifacts.py`, `subscriptions.py`, `context_builder.py`, `memory.py`, `lineage.py`, `team_spec.py` (team.yaml), `ids.py` |
| `app/agents/` | The five reference agents: director, research, finance, critic, monitor. `base.py` is the agent contract |
| `app/prompts/` | Their markdown prompts — edit these, not the Python, for behaviour |
| `app/runtime/` | `llm.py` (provider registry + CLI-agent runner), `durable.py` (Restate Virtual Object factory) |
| `app/memory/` | V4 backends: `filesystem.py` (default), `gbrain.py` (opt-in), `distil.py` |
| `app/db/` | SQLAlchemy repository — projects, tasks, runs, artifacts, subscriptions |
| `app/web/` | Timeline UI + approve/reject buttons |
| `app/local_runner.py` | In-process runtime + `drive()`. No Restate, no Postgres. Most tests use it |
| `app/launcher.py` | Starts a project from outside (used by Buzz); mints per-launch project ids |
| `app/main.py` | CLI: `migrate`, `web`, `serve`, `run`, `providers`, `memory` |
| `bus/` | V2 multi-team bus: `models/`, `routing/` (registry, commands, topics, effects), `adapters/` (restate, buzz, buzz_live, a2a), `nostr/` (NIP-01/29/42 + in-process dev relay), `web/` |
| `teams/` | Real teams. `investment` (reuses `app/agents`), `research` (its own agents + prompts) |
| `template/` | What `/muster-new` copies to make a team |
| `demo/` | Runnable demos, including `buzz_session` |
| `.claude/skills/` | `muster-new` (interview + scaffold a team), `muster-buzz` (put it in a chat room) |
| `docs/prd/` | V1–V4 PRDs. V1–V3 are the user's, kept **verbatim** |
| `docs/superpowers/` | `specs/` (accepted decisions), `plans/` |
| `scripts/compose.sh` | Container-engine wrapper: Apple Container or Docker |

## Commands that work

| Command | What it does |
|---|---|
| `uv run pytest` | Full unit suite. No Docker, no Restate, no Postgres, ~8s |
| `uv run pytest -m integration` | Needs `make up` + `make deps` |
| `make run` / `uv run python -m app.main run "<objective>"` | One team in-process, prints the timeline |
| `make demo` | Two teams, one bus, cross-team command + event |
| `make buzz-demo` | Drive a team from a real Nostr relay running in-process |
| `make dev` | `up` + **`migrate`** + serve + register — the durable path |
| `make install-skills` | Symlink the two skills into `~/.claude/skills/` |
| `uv run python -m app.main providers` | Which model providers are installed and reachable |

`migrate` is **not optional** before the durable path: it seeds the subscriptions table,
so without it `publish()` fans out to nobody and the run silently stalls.

## Prime directive

> **Our code describes agent semantics. Restate handles distributed-systems semantics.**

We never build a scheduler, retry engine, actor runtime, broker, lease manager, or
shared-chat memory. If a problem is "distributed systems", it is Restate's job.

## Design principles

1. **Do not reinvent.** Before writing infrastructure, check whether a maintained
   library already does it. Prefer boring, widely-used dependencies over clever ones.
2. **Minimal surface.** Muster *exposes* ensemble functionality; internals stay small.
   If `kernel/` starts becoming a framework before the demo works, stop and simplify.
3. **Structured, obvious internals.** One module, one job. No cleverness that needs a
   paragraph to explain. A new reader should trace a request end-to-end in one sitting.
4. **Never break the public surface.** Anything documented as a feature is a contract.
   Changing it needs a deliberate decision, not a refactor side effect.
5. **Agents are separate minds.** Bounded reconstructed context, never a shared
   transcript. Large outputs become artifacts passed by reference.
6. **LLMs reason; deterministic code routes.** No LLM polling loops, ever.

## Already solved upstream — use, don't rebuild

Verified Aug 2026; re-check versions before relying on specifics.

| Need | Use |
|---|---|
| Durable agent execution | `restate.ext.pydantic.RestateAgent` wrapping a `pydantic_ai.Agent` |
| Human pause/resume (approve/reject) | `restate_context().awakeable(type_hint=...)` |
| Durable timer / `wake_later` | `restate_context().sleep(timedelta(...))` + `restate.select()` |
| Side-effect dedup, idempotent steps | `restate_context().run_typed(...)` |
| Per-session/keyed agent state | Restate **Virtual Objects** (journal dedups tool calls) |

Install: `uv add restate_sdk[serde] pydantic-ai`

Muster's own job is therefore only: kernel agent semantics (`send`/`publish`/
`wake_later`/`subscribe`), the subscription router, Postgres semantic state, the
artifact store, the context builder, the timeline UI, and the `BusAdapter` seam.

## Public surface (treat as contract)

```python
await send(agent=..., task=..., payload=...)         # targeted command
await publish(topic=..., payload=...)                # logical fan-out
await wake_later(agent=..., delay=..., payload=...)  # durable future call
ctx.artifacts.put / ctx.artifacts.get                # artifacts by reference
ctx.recall(...) / ctx.load_memory(ref)               # V4 memory, explicit only
BusAdapter                                           # V2 seam — no Restate types leak
team.yaml                                            # V3 declarative team contract
```

Agent code calls the kernel, never Restate directly. Restate SDK types must not appear
in any public signature — that is what lets V2 route across teams without rewriting agents.

## The one architectural fact

`app/kernel/context.py` defines `KernelContext`, a **Protocol** with two implementations:
`RestateKernelContext` (real SDK) and `FakeKernelContext` (in-process, records sends and
journal entries). Everything above the seam is written once and runs both ways — which is
why the entire unit suite needs zero infrastructure. **Do not let Restate types cross it.**

## Accepted design decisions

Recorded in `docs/superpowers/specs/v1-runtime-decisions.md`. Do not relitigate silently.

- Agents are Restate **Virtual Objects**, one object type **per agent**, keyed by `project_id`.
  Objects serialize per key and run in parallel across keys, so `publish()` fan-out is
  unaffected. Keying all agents by `project_id` alone would head-of-line block them — don't.
- `publish()` must use `ctx.generic_send` (runtime string dispatch), not the typed
  `ctx.object_send` — subscriber names come from the subscriptions table.
- `wake_later()` is `ctx.object_send(..., send_delay=...)`. No timer code of our own.
- `awakeable_id` is persisted on the run record from the **first** migration; the Approve
  button cannot resume a workflow without it.
- V2 `Effect` owns only the status machine and reconciliation, sitting on top of
  `run_typed()` — it never wraps or replaces the durable step.
- A topic crosses a team boundary **only if the team declares it in `public.topics`**.
  Team-local subscribers are woken directly, so nothing is ever woken twice.
- The agent registry is keyed by `(team, name)`. Unscoped registration (`team=""`)
  stays reachable from anywhere, which is what keeps V1 callers working.
- `app/` imports `bus/` **lazily, inside the function**, never at module top level.
  A standalone V1 team must work with the bus package absent.
- Cross-team artifacts cross **by reference**: the receiving team registers the ID
  with `path=""` and `meta.external=True`; the bytes stay with the producing team.
- A receiving team **mints its own task id** from `payload["id"]`. Reusing the sender's
  collided with the sender's record and the director silently ignored the work.

## Durable-execution rules (learned the hard way, against a live server)

Restate replays a handler after a retry. Anything that makes the replay take a
different path aborts the invocation with a code-path mismatch.

- **Every id that travels in a send must be minted via `Kernel.mint()`** — task,
  event, run *and artifact*. Artifact ids were missed and only live Restate caught it.
- **Every read of external mutable state that steers control flow must go
  through `Kernel.step()`.** The director querying the artifact table to decide
  whether both specialists had finished was exactly this bug: the first attempt
  returned early, the world changed, the retry took a different branch.
- **A journalled step's return value must be JSON-serializable.** Return
  `model_dump(mode="json")` rows and rebuild models outside the step.
- **One journal per invocation, not per process.** `FakeKernelContext.invocation()`
  models this; sharing a journal lets a later invocation replay an earlier one's
  reads and silently freeze the world.
- **Choreography needs a termination condition.** V1 looped forever until specialists
  announced completion only for `task.type == "analyze"` and the director proposed
  once per project. Check for an existing artifact before producing one.

## Memory rules (V4)

- **Markdown is canonical.** Deleting the derived index must lose nothing.
- **Retrieved explicitly, never injected.** An uninvited memory is a shared
  transcript by another name. `ctx.recall()` returns references; the body comes
  from an explicit `ctx.load_memory(ref)`, exactly as artifacts work.
- **Provenance or it is a rumour.** Every note names the runs and artifacts it came from.
- **`MEMORY_BACKEND=none` is a supported mode**, not a stub. Memory must never
  be load-bearing.
- **Use `ctx.root_objective(task)`, never `task.objective`**, when you need what
  work is *about*. A reaction task's objective is "React to <topic>"; querying
  memory with that finds nothing and the team silently never learns.
- One GBrain, **one source per team** (`gbrain sources add`), not one brain per team.
  A source id is permanently bound to a path; a mismatch recalls empty and silent.

## Environment quirks (each cost real time once)

- **Containers**: `scripts/compose.sh` auto-detects Apple Container (macOS) or Docker;
  force with `MUSTER_ENGINE=docker|apple`. Apple's `container-compose` has no `--wait`.
  Postgres on a mounted volume needs `PGDATA=/var/lib/postgresql/data/pgdata` or
  `initdb` trips over `lost+found`.
- **`sqlite://` in-memory** needs `poolclass=StaticPool`, else each thread gets its own
  empty database. Already fixed in `app/db/repository.py` — don't undo it.
- **YAML 1.1 reads bare `off` as `False`**, so `memory: off` in team.yaml needs the
  `field_validator(mode="before")` in `team_spec.py`.
- **`uv export` includes the dev group by default** — `setup.sh` passes `--no-dev`, or
  pytest ships in the runtime requirements.
- **GBrain is not on npm.** `bun install -g github:garrytan/gbrain` is the real install;
  `npm install -g gbrain` installs an unrelated package.
- **`teams/*/memory/` is gitignored runtime data.** A real `make run` writes notes there;
  never commit them.

## Testing

- **Every externally exposed feature needs unit tests.** No exceptions.
- Each kernel primitive gets a test named for it (`test_send`, `test_publish`, `test_timer`).
- Context isolation is a tested property, not a convention: assert that an agent's built
  context contains no unrelated transcript or another agent's scratchpad.
- **Docs are tested too.** `tests/test_readme.py` and `tests/test_claude_md.py` derive
  assertions from code, so a stale path or command in either file fails the suite.
- Never assert on shared state outside `tmp_path`. Snapshot before/after instead —
  an assertion that a repo directory is empty measures the developer, not the code.
- Crash recovery is a **release criterion**, not a demo: kill the process mid-workflow,
  restart, assert durable intent survived and committed steps did not re-run.
- Run tests before claiming anything works. Evidence before assertions.

## Working agreements

- **Launch subagents whenever useful and possible.** Fan out genuinely independent
  work — separate modules, separate test files, research spikes — to parallel agents
  rather than doing it serially. Keep tightly-coupled foundational work inline, since
  a cold agent re-deriving a shared interface costs more than it saves. Give each
  agent the exact interface contract it must code against.
- **Do fresh web lookups.** This stack moves fast; knowledge goes stale within months.
  Verify current library capabilities before designing around remembered APIs.
- **Suggest better ideas.** If an approach in a PRD or in my own plan looks wrong, say so
  with the reason before building it. Silent compliance on a bad design is not helpful.
- PRDs in `docs/prd/` are the authored source of record — keep them verbatim. They use
  working names (`agent-team`, `agent-bus`); the README carries the map to real repo names.
- Plans go in `docs/superpowers/plans/`, specs in `docs/superpowers/specs/`.

## State of the repo

Working and verified live, not just in tests: the durable path against real Restate +
Postgres; the Buzz demo against a real Nostr relay; every model provider including
Ollama and `claude -p`; GBrain with per-team sources.

Known unfinished — pick these up rather than rediscovering them:

- **No `POST /start` in `app/web/app.py`.** `Launcher` exists and is tested; the button
  and form were never wired. Smallest useful next task.
- **The live kill/restart crash test has never been executed.** `tests/test_crash_recovery.py`
  is written and now runnable via Apple Container. Highest-value unverified claim.
- **No `LICENSE` file.** Deliberately deferred.
- Memory notes are per-decision by default, so twenty similar rejections make twenty
  notes rather than one accumulating lesson. Passing a topical `subject` already works;
  nothing calls it.

## Explicitly out of scope (V1/V2)

Kubernetes, Kafka, NATS, Redpanda, Redis-for-coordination, Temporal, DBOS, vector DB as
canonical state, Prometheus/Grafana/Tempo/Loki. Adding a broker requires a measured
justification against the V2 PRD's decision gate.
