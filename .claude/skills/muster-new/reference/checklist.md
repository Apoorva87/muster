# Per-team checklist

## The tests V3 requires

Every new team must ship one end-to-end scenario exercising its intended topology,
plus the generic checks below. The first four come free in `template/tests/test_smoke.py`;
the rest you write only if the team actually uses that feature. Skipping one because
"the team does not do that" is fine. Skipping one it does do is not.

| # | Check | How | Required when |
|---|---|---|---|
| 1 | Team config validates | `load_team_spec("teams/<id>").check()` | always |
| 2 | Every configured agent loads | `load_team_spec("teams/<id>").load_entrypoints()` | always |
| 3 | Subscriptions reference declared agents | assert each `spec.subscription_pairs()` agent is in `spec.agents` | always |
| 4 | A direct command works | `kernel().send(agent=..., task=...)` then `drive([runner])`; assert the run record and the artifact | always |
| 5 | Expected event fan-out works | publish the topic; assert **every** declared subscriber woke, and nothing else did | any team with `subscriptions` |
| 6 | Artifact refs resolve | `await ctx.artifacts.get(ref)` round-trips; the receiving agent got a **reference**, never the body inline | any team passing work between agents |
| 7 | Context builder excludes unrelated history | assert the built context contains no other agent's scratchpad, reasoning, or an unrelated project's transcript | always — this is a tested property, not a convention |
| 8 | Timed wakeup works | assert `ctx.sends` holds a delayed send, then that firing it reschedules the next one | only if the team uses `ctx.wake_later` |
| 9 | Human pause/resume works | run parks with an `awakeable_id` persisted; resolving it resumes and takes the right branch — test **both** approve and reject | only if the team calls `ctx.request_approval` |
| 10 | Restart recovery | drop every in-memory object mid-workflow, rebuild the kernel over the same repository, assert durable intent survived and committed steps did not re-run | always for a team with more than one step |

Reference implementations to copy from, in `tests/`: `test_publish.py` (5),
`test_artifacts.py` (6), `test_context_isolation.py` (7), `test_timer.py` (8),
`test_human_resume.py` (9), `test_crash_recovery.py` (10).

Run them with:

```bash
uv run pytest teams/<id>/tests -q
uv run pytest -q
```

## Extra checks worth adding

- **Agent import purity.** `ast`-parse each agent module and assert its top-level
  imports are a subset of `{"__future__", "app"}`. `tests/test_two_team_demo.py`
  does exactly this — a team author must never import `restate` or `bus`.
- **Public topics are namespaced** with the team id, so they stay meaningful outside
  the team.
- **Agent names do not collide** with another team's if both will run in one bus
  session.
- **Effects are idempotent.** Any side-effectful operation carries a stable
  operation/idempotency ID, and an ambiguous outcome is reconciled before retry.

## What you must never need to edit

From `template/README.md`. If a new team needs any of these changed, the abstraction
is wrong and should be fixed centrally — not copied around. Stop and say so rather
than editing them.

- Restate internals
- retry implementation
- durable timer implementation
- bus routing
- the artifact backend
- generic task/event schemas
- human-resume plumbing
- tracing

## What you should be editing

Inside `teams/<id>/` and nowhere else: `team.yaml`, `.env`, and the
`teams/<id>/agents/`, `teams/<id>/prompts/`, `teams/<id>/tools/`,
`teams/<id>/workflows/`, `teams/<id>/domain/` and `teams/<id>/tests/`
directories.
