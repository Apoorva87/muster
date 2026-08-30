# Muster V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the V1 local durable agent runtime — independent agents, targeted commands, topic fan-out, durable timers, human pause/resume, isolated context, filesystem artifacts, and a local timeline UI.

**Architecture:** All kernel primitives take a narrow `KernelContext` protocol rather than a Restate context directly. `RestateKernelContext` adapts the real SDK; `FakeKernelContext` records calls in memory. This single seam makes every externally exposed feature unit-testable with zero infrastructure, and is the same seam V2's `BusAdapter` plugs into. Agents are Restate Virtual Objects, one object type per agent, keyed by `project_id`.

**Tech Stack:** Python 3.12, uv, restate-sdk, pydantic-ai, SQLAlchemy 2.x, FastAPI, pytest.

**Spec:** `docs/prd/v1-local-durable-runtime.md` + `docs/superpowers/specs/v1-runtime-decisions.md`

## Global Constraints

- Python 3.12+ (`uv python pin 3.12`). System python is 3.9 — never invoke it.
- **Every externally exposed feature has a unit test.** Non-negotiable (CLAUDE.md).
- Unit tests must run with **no Docker, no Restate, no Postgres**. SQLite backs repository tests.
- Restate SDK types must not appear in any public kernel signature.
- `publish()` uses runtime string dispatch (`generic_send`), never a compile-time handler ref.
- Do not reinvent: `awakeable` for human waits, `send_delay` for timers, `run_typed` for side effects.
- Kernel stays small. If it becomes a framework before the demo works, stop and simplify.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/config.py` | Env-backed settings |
| `app/kernel/ids.py` | Stable ID generation |
| `app/kernel/models.py` | Domain models: Task, Event, Artifact, Subscription, RunRecord |
| `app/kernel/context.py` | `KernelContext` protocol + `FakeKernelContext` |
| `app/kernel/artifacts.py` | `ArtifactStore` protocol + filesystem impl |
| `app/kernel/subscriptions.py` | Topic → agents resolution |
| `app/kernel/runtime.py` | `send` / `publish` / `wake_later` / `request_approval` |
| `app/kernel/context_builder.py` | Bounded context reconstruction |
| `app/kernel/bus.py` | `BusAdapter` protocol + `LocalBusAdapter` (V2 seam) |
| `app/db/models.py` | SQLAlchemy tables incl. `awakeable_id` |
| `app/db/repository.py` | Persistence for tasks/events/runs/subscriptions/artifacts |
| `app/agents/base.py` | `@agent` registry + `AgentContext` |
| `app/agents/*.py` | director, research, finance, critic, monitor |
| `app/runtime/durable.py` | Restate Virtual Object wiring (D1: collapsed adapter) |
| `app/web/app.py` | Timeline + approve/reject endpoints |

---

## Task 1: Project scaffold and test harness

**Files:** Create `pyproject.toml`, `.python-version`, `app/__init__.py`, `tests/conftest.py`

**Produces:** a `uv run pytest` that executes green.

- [ ] Step 1: `uv init --no-workspace`, pin 3.12, add deps
- [ ] Step 2: Add pytest config with `-m "not integration"` default
- [ ] Step 3: Run `uv run pytest` — expect "no tests ran"
- [ ] Step 4: Commit

## Task 2: Domain models and IDs

**Files:** Create `app/kernel/ids.py`, `app/kernel/models.py`, `tests/test_models.py`

**Produces:** `new_id(prefix)`, `Task`, `Event`, `Artifact`, `Subscription`, `RunRecord`, `TaskStatus`.

- [ ] Step 1: Failing test — IDs are prefixed, unique, stable-length; Task round-trips
- [ ] Step 2: Run, expect ImportError
- [ ] Step 3: Implement pydantic models; `TaskStatus` includes `WAITING_FOR_HUMAN`
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 3: ArtifactStore

**Files:** Create `app/kernel/artifacts.py`, `tests/test_artifacts.py`

**Produces:** `ArtifactStore` protocol, `FilesystemArtifactStore(root)`, `.put()`, `.get()`, `.ref()`.

- [ ] Step 1: Failing test — put returns ref, get round-trips, path is `<root>/<project>/<id>.<ext>`
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement filesystem store
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 4: Persistence layer

**Files:** Create `app/db/models.py`, `app/db/repository.py`, `tests/test_repository.py`

**Produces:** `Repository` with task/event/run/artifact/subscription CRUD. Includes `awakeable_id` + `awaiting_since` on the run record from this first migration (decision D3).

- [ ] Step 1: Failing test on in-memory SQLite — save/load task, record run, set awakeable
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement SQLAlchemy models (portable `JSON`, not PG-only `JSONB`) + repository
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 5: Subscription registry

**Files:** Create `app/kernel/subscriptions.py`, `tests/test_subscriptions.py`

**Produces:** `SubscriptionRegistry.subscribers_for(topic) -> list[str]`, seeded from the PRD's default table.

- [ ] Step 1: Failing test — `proposal.ready` resolves to `critic` and `finance`; unknown topic → `[]`
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement registry backed by repository, with PRD default seed
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 6: KernelContext seam

**Files:** Create `app/kernel/context.py`, `tests/test_context.py`

**Produces:** `KernelContext` protocol (`generic_send`, `object_send`, `sleep`, `awakeable`, `run_typed`, `key`) and `FakeKernelContext` recording `.sends`, `.awakeables`.

This is the file that makes every later test possible with no infrastructure.

- [ ] Step 1: Failing test — FakeKernelContext records a send with agent, key, payload, delay
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement protocol + fake
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 7: Kernel primitives — the public API

**Files:** Create `app/kernel/runtime.py`, `tests/test_send.py`, `tests/test_publish.py`, `tests/test_timer.py`

**Consumes:** Tasks 2, 5, 6.
**Produces:** `send(ctx, agent, task, payload, project_id)`, `publish(ctx, topic, payload, project_id)`, `wake_later(ctx, agent, delay, payload, project_id)`.

- [ ] Step 1: `test_send` — one send, correct agent/key, stable task id
- [ ] Step 2: `test_publish` — `proposal.ready` produces exactly 2 sends (critic, finance); fan-out to distinct agents; unknown topic → 0 sends and no error
- [ ] Step 3: `test_timer` — `wake_later` sets `send_delay` equal to the delay and does not block
- [ ] Step 4: Run all three, expect fail
- [ ] Step 5: Implement runtime.py
- [ ] Step 6: Run, expect PASS
- [ ] Step 7: Commit

## Task 8: Human pause/resume

**Files:** Modify `app/kernel/runtime.py`; create `tests/test_human_resume.py`

**Produces:** `request_approval(ctx, repo, task, prompt) -> ApprovalDecision`, `resolve_approval(repo, task_id, decision)`.

- [ ] Step 1: Failing test — request_approval creates an awakeable, persists `awakeable_id`, sets status `WAITING_FOR_HUMAN`; resolve returns approve/reject; no LLM invoked while waiting
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement using `ctx.awakeable()`
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 9: ContextBuilder and isolation

**Files:** Create `app/kernel/context_builder.py`, `tests/test_context_isolation.py`

**Produces:** `build_context(agent, task, repo, store, limits) -> AgentPrompt` assembling only: role, task objective, selected project state, explicit artifact refs, latest relevant result.

- [ ] Step 1: Failing test — given another agent's scratchpad in the repo, the built context contains none of it; artifact bodies load only when referenced; size limit enforced; loaded refs are logged
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement builder
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 10: Agents and BusAdapter seam

**Files:** Create `app/agents/base.py`, the five agents, `app/kernel/bus.py`, `tests/test_agents.py`, `tests/test_bus.py`

**Produces:** `@agent(name)` registry, `AgentContext` exposing `ctx.send/publish/wake_later/artifacts/prompt`, `BusAdapter` protocol + `LocalBusAdapter`.

- [ ] Step 1: Failing tests — all five agents register; monitor schedules its own next wakeup then exits; BusAdapter leaks no Restate types
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 11: Restate wiring and compose

**Files:** Create `app/runtime/durable.py`, `app/main.py`, `docker-compose.yml`, `Makefile`

**Produces:** Virtual Object per agent keyed by `project_id`; `RestateKernelContext` adapting `restate.ObjectContext`.

- [ ] Step 1: Implement `RestateKernelContext` satisfying the protocol from Task 6
- [ ] Step 2: Register one `restate.VirtualObject` per agent
- [ ] Step 3: compose with Restate + Postgres; `make dev`
- [ ] Step 4: Test that the adapter satisfies the protocol structurally (no server needed)
- [ ] Step 5: Commit

## Task 12: Web timeline and approvals

**Files:** Create `app/web/app.py`, `app/web/templates/`, `tests/test_web.py`

**Produces:** `GET /` project timeline, `GET /run/{id}` detail, `POST /approve/{task_id}`, `POST /reject/{task_id}`.

- [ ] Step 1: Failing test using FastAPI TestClient — timeline lists runs chronologically; approve resolves the awakeable
- [ ] Step 2: Run, expect fail
- [ ] Step 3: Implement
- [ ] Step 4: Run, expect PASS
- [ ] Step 5: Commit

## Task 13: Crash recovery

**Files:** Create `tests/test_crash_recovery.py`

Two layers, because full recovery needs a live server:
- **Unit (runs here):** durable intent survives a repository round-trip — a task in `WAITING_FOR_HUMAN` with a persisted `awakeable_id` is recoverable after the process object is discarded; replaying a handler with the same idempotency key does not duplicate work.
- **Integration (`@pytest.mark.integration`, skipped without Docker):** start workflow, kill process, restart, assert durable state recovered and committed steps not re-run.

- [ ] Step 1: Write both layers
- [ ] Step 2: Run unit layer, expect PASS; integration auto-skips
- [ ] Step 3: Commit

---

## Acceptance mapping

| PRD criterion | Verified by |
|---|---|
| command durably wakes another agent | `test_send` |
| topic wakes multiple subscribers | `test_publish` |
| timer wakes dormant agent | `test_timer` |
| agent schedules own wakeup | `test_agents::test_monitor_reschedules` |
| isolated reconstructed context | `test_context_isolation` |
| artifacts passed by reference | `test_artifacts`, `test_context_isolation` |
| sleep for human, resume from button | `test_human_resume`, `test_web` |
| activity visible in timeline | `test_web` |
| restart loses no durable intent | `test_crash_recovery` (unit + integration) |
| runs locally without K8s/Kafka | `docker-compose.yml` |
