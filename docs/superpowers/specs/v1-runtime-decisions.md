# V1 Runtime Decisions

Status: **accepted** — 2026-08-30
Scope: decisions that shape V1 before any code is written. Supplements
`docs/prd/v1-local-durable-runtime.md`; does not replace it.

API details verified against Restate Python SDK docs, Aug 2026. Re-check on upgrade.

---

## D1 — Collapse the runtime adapters

**Accepted.** The V1 PRD allocates `app/runtime/restate.py` and `app/runtime/llm.py`.
The Restate Pydantic AI integration already provides durable execution, LLM-call
persistence and durable tool steps, so both files are thin configuration rather than
subsystems.

```text
app/runtime/restate.py  ─┐
                         ├─→  app/runtime/durable.py
app/runtime/llm.py      ─┘
```

Rationale: the PRD's own rule — if `kernel/` is becoming a framework before the demo
works, stop and simplify. Applied before writing it rather than after.

---

## D2 — Agents are Virtual Objects, one object type per agent, keyed by `project_id`

**Accepted.** Pub/sub fan-out is preserved; this was the open question.

### Why Virtual Objects over plain Services

Virtual Objects give single-writer concurrency per key, isolated K/V state per key,
and parallel execution across keys. `WAITING_FOR_HUMAN` state, per-project isolation
and idempotency are all keyed-state problems, so the Object model supplies them
instead of us building them.

### Fan-out is unaffected

Serialization is **per key**, not global. `publish()` resolving to N subscribers
targets N distinct objects, which execute concurrently.

```text
publish("proposal.ready")
   ├─→ Critic  @ project-123   ─┐
   └─→ Finance @ project-123   ─┴─ distinct objects → run in parallel
```

Two events to the *same* agent for the *same* project queue in order. That is the
desired semantics: no interleaved state mutation on one project.

### Keying rule

One Object type **per agent**, each keyed by `project_id`.

Keying every agent by `project_id` alone would make all agents in a project a single
object, so a slow `research` invocation would head-of-line block a queued `finance`
event. Distinct object types keep different agents parallel while same-agent events
still serialize.

### Kernel → SDK mapping

| Kernel API | Restate call |
|---|---|
| `send(agent, task, payload)` | `ctx.object_send(handler, key=project_id, arg=...)` |
| `publish(topic, payload)` | per subscriber: `ctx.generic_send(agent, "handle", key=project_id, arg=...)` |
| `wake_later(agent, delay, payload)` | `ctx.object_send(..., send_delay=timedelta(...))` |
| human pause/resume | `restate_context().awakeable(type_hint=...)` |
| durable side-effect step | `restate_context().run_typed(...)` |
| dedup on a call | `idempotency_key=...` |

`publish()` must use `ctx.generic_send` — subscriber names come from the
subscriptions table as runtime strings, so the typed form (which needs a
compile-time handler reference) cannot express fan-out. Using generic dispatch is
what keeps the topic abstraction replaceable by a V2 bus adapter.

---

## D3 — Persist the awakeable ID from the first migration

**Accepted.** The web UI's Approve/Reject button cannot resume a workflow unless the
awakeable's ID is stored alongside the run record. It is the only state bridging the
UI and the sleeping workflow.

Add to the run/task schema in the initial migration — not retrofitted:

```text
awakeable_id      text null    -- set when entering WAITING_FOR_HUMAN
awaiting_since    timestamptz null
```

---

## D4 — `Effect` wraps the status machine, not the durable step

**Accepted, V2 scope.** `run_typed()` already provides durable, replay-safe steps.
What it does not provide is reconciliation of `UNKNOWN` outcomes against external
APIs lacking idempotency keys — the genuinely hard part.

`Effect` therefore owns only: status machine
(`PENDING/SENT/CONFIRMED/UNKNOWN/FAILED/COMPENSATED`), the idempotency key, and
reconcile-before-retry. It sits *on top of* `run_typed`, never wrapping or
replacing it. Where the external API accepts an idempotency key, the Effect ID is
passed through as Restate's `idempotency_key`.

---

## D5 — License deferred

Public repo currently has no `LICENSE` (defaults to all-rights-reserved). Deferred by
decision, not oversight. Apache-2.0 is the likely pick given V3's reuse goal.

---

## Open

- Whether `monitor` needs its own key namespace, since it is project-independent.
- Artifact GC policy — the PRD does not state one and V1 does not need it.
