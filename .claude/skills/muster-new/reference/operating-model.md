# How a Muster team works, and where you sit in it

Read this to the user after generating their team. A team they cannot explain,
watch or steer is not finished.

## The parts, and what each is for

| Part | What it does | You touch it when |
|---|---|---|
| **Agent** | Does one bounded job. Gets a task, never a transcript. | Changing what the team *does* |
| **Task** | One unit of work, with a stable id and explicit input refs | Never directly |
| **Event** | "This happened." Fans out to whoever subscribed | Adding a new reaction |
| **Artifact** | A large output, stored outside context, passed by reference | Reading what the team produced |
| **Kernel** | `send` / `publish` / `wake_later` / approvals | Never — this is the runtime |
| **Restate** | Durability: retries, timers, replay, human waits | Never |
| **Memory** | Markdown notes the team learned from past decisions | Correcting a wrong lesson |
| **Bus** | Routes between teams; Restate stays the durability authority | Adding a second team |

The one sentence that explains the whole design: **our code describes agent
semantics, Restate handles distributed-systems semantics.** If you find yourself
writing retries, queues, timers or locks, stop — that is the runtime's job and
you are about to fight it.

## Two ways to run, and why the difference matters

```
in-process                          durable
uv run python -m app.main run ...   make up
                                    uv run python -m app.main migrate
                                    make dev
no Docker, no Restate               Restate + Postgres
NOT durable — kill it, work is gone survives crashes, restarts, days of waiting
for developing and demoing          for anything you care about
```

`migrate` is not optional on the durable path: it creates the schema **and**
seeds the subscription table. Skip it and `publish()` resolves to nobody — the
team appears to run while quietly doing nothing.

Same agents, same `team.yaml`, same kernel. Only the `KernelContext` differs.
Confusing the two is the expensive mistake: in-process mode looks identical
right up to the moment something crashes.

## When you must intervene

Three of these are by design; the fourth is a failure.

1. **An approval parks the workflow.** The team asked and is waiting. It burns
   no tokens and can wait for days across restarts. Nothing proceeds until you
   answer — that is the point of asking.
2. **A side-effectful step needs a decision.** Anything that writes to the
   outside world runs as an `Effect` with an idempotency key. If its outcome is
   `UNKNOWN`, it will **not** retry blindly — it waits for reconciliation,
   because replay protection is not the same as exactly-once in the world.
3. **A memory note is wrong.** Notes are markdown in the repo. Fix it in an
   editor and commit; the next run uses the corrected version. Do not work
   around a bad note — delete or correct it.
4. **An agent or team failed.** Surfaced as `system.agent.failed` /
   `system.team.failed`. This is a bug, not a decision point.

## Where to watch

| Surface | Shows | Start it |
|---|---|---|
| **Timeline** | Every run in order, with timing, refs and errors. Approve/Reject live here | `uv run python -m app.main web` → :8000 |
| **Bus session view** | Which teams registered, health, running and waiting counts, the cross-team routing table | `bus/web/app.py` |
| **Buzz room** | Semantic progress only, and you can type `run …` / `approve` back at it | `make buzz-demo`, or `--relay ws://your-buzz` |
| **Memory** | What the team has learned | `uv run python -m app.main memory <team>` |
| **Restate UI** | Invocations, journals, retries — for when durability itself misbehaves | :9070 |
| **Postgres** | The canonical semantic state: tasks, events, runs, artifacts | `make migrate` created it |

The timeline is the default. Buzz is the one to add when other people need to
watch or steer, because it is the only surface built for more than one human.

Two things a room deliberately never shows: internal events (`event.delivered`,
`event.published`, tool calls, retries, token counts) and artifact **bodies**.
References only. A room is a decision log, not a log file.

## What good looks like

- Approvals are rare and meaningful. If every run parks, the gate is in the
  wrong place.
- The timeline reads as a story someone could follow.
- Memory grows slowly and every note has provenance.
- Nothing in `app/kernel/` or `bus/` had to change to build this team.

That last one is the real test. If a normal new team needs edits to the runtime,
retries, timers, routing, the artifact backend, human-resume or tracing, the
abstraction is wrong and gets fixed centrally — not copied around.
