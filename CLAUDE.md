# CLAUDE.md — Muster

Muster is a local-first durable runtime for **ensembles of agents** that work and
communicate. Read `docs/prd/v1-local-durable-runtime.md` before touching kernel code;
V2/V3 PRDs sit beside it and define the seams V1 must leave open.

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
await send(agent=..., task=..., payload=...)      # targeted command
await publish(topic=..., payload=...)             # logical fan-out
await wake_later(agent=..., delay=..., payload=...)  # durable future call
ctx.artifacts.put / ctx.artifacts.get             # artifacts by reference
BusAdapter                                        # V2 seam — no Restate types leak through
team.yaml                                         # V3 declarative team contract
```

Agent code calls the kernel, never Restate directly. Restate SDK types must not appear
in any public signature — that is what lets V2 route across teams without rewriting agents.

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

## Durable-execution rules (learned the hard way, against a live server)

Restate replays a handler after a retry. Anything that makes the replay take a
different path aborts the invocation with a code-path mismatch.

- **Every id that travels in a send must be minted via `Kernel.mint()`** — task,
  event, run *and artifact*. Artifact ids were missed and only live Restate
  caught it.
- **Every read of external mutable state that steers control flow must go
  through `Kernel.step()`.** The director querying the artifact table to decide
  whether both specialists had finished was exactly this bug: the first attempt
  returned early, the world changed, the retry took a different branch.
- **A journalled step's return value must be JSON-serializable.** Return
  `model_dump(mode="json")` rows and rebuild models outside the step.
- **One journal per invocation, not per process.** `FakeKernelContext.invocation()`
  models this; sharing a journal lets a later invocation replay an earlier one's
  reads and silently freeze the world.

## Memory rules (V4)

- **Markdown is canonical.** Deleting the derived index must lose nothing.
- **Retrieved explicitly, never injected.** An uninvited memory is a shared
  transcript by another name. `ctx.recall()` returns references; the body comes
  from an explicit `ctx.load_memory(ref)`, exactly as artifacts work.
- **Provenance or it is a rumour.** Every note names the runs and artifacts it
  came from.
- **`MEMORY_BACKEND=none` is a supported mode**, not a stub. Memory must never
  be load-bearing.
- **Use `ctx.root_objective(task)`, never `task.objective`**, when you need what
  work is *about*. A reaction task's objective is "React to <topic>"; querying
  memory with that finds nothing and the team silently never learns.

## Testing

- **Every externally exposed feature needs unit tests.** No exceptions.
- Each kernel primitive gets a test named for it (`test_send`, `test_publish`, `test_timer`).
- Context isolation is a tested property, not a convention: assert that an agent's built
  context contains no unrelated transcript or another agent's scratchpad.
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

## Explicitly out of scope (V1/V2)

Kubernetes, Kafka, NATS, Redpanda, Redis-for-coordination, Temporal, DBOS, vector DB as
canonical state, Prometheus/Grafana/Tempo/Loki. Adding a broker requires a measured
justification against the V2 PRD's decision gate.
