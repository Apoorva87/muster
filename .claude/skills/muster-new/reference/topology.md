# Choosing a topology

## The rule

From `docs/prd/v3-custom-teams.md`:

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

Six clauses. Every agent must claim at least one, out loud, by name. "It is a
different kind of work" is not one of them — that describes a function.

## How to apply it

Start from one agent. For each additional agent the user wants, ask in order:

1. **Different tools or permissions?** The strongest clause, because it is checkable.
   A read-only researcher and a deploy executor must be separate: one of them holds a
   credential the other must never be able to reach.
2. **Different wakeup lifecycle?** Also checkable. A timer-woken checker and a
   command-driven analyst are different agents because they are woken by different
   things and one of them must be cheap enough to run every ten minutes.
3. **Adversarial or independent evaluation?** A critic that receives *facts plus the
   proposal* — never the proposer's scratchpad — genuinely reasons differently. This
   is the one case where "same tools, same lifecycle" still justifies a split.
4. **Parallelism that actually saves wall-clock time?** Two branches that both take
   thirty seconds and merge. Not two steps where the second needs the first's output.
5. **Independent context that improves reasoning?** Real, but the weakest and most
   over-claimed. Ask what specifically would pollute the shared context.
6. **Different system role/perspective?** Weakest of all on its own. If nothing but
   this clause applies, it is a prompt section, not an agent.

If none apply: **write a function.** A helper in the agent module costs one import.
An agent costs a context reconstruction, a model call, a subscription row, a prompt
file and a failure mode.

## Three worked examples

### 1. Security triage

*"Findings arrive from a scanner. Someone assesses each one, someone decides if the
severity is right, someone writes the ticket, and a human signs off before we file."*

**Survives the critique — two agents:**

```text
finding.received --> analyst --(assessment.ready)--> reviewer --> human gate --> file
```

| Agent | Clause it claims |
|---|---|
| `analyst` | reproduces the finding; needs the scanner and repo read tools |
| `reviewer` | adversarial/independent evaluation of severity, given facts + assessment, not the analyst's reasoning |

Ticket filing is **not** an agent. It is a side-effectful step inside the flow: an
Effect with a stable idempotency key, gated on `ctx.request_approval`, executed
through one restricted tool. Giving it an agent adds a mind that holds no opinion.

**Does not survive — five agents:**

```text
intake -> analyst -> severity-rater -> ticket-writer -> pm -> human
```

- `intake` parses the scanner payload. No LLM judgement, no tools of its own — that is
  deterministic routing, which V3 assigns to code, not to a model.
- `severity-rater` and `analyst` share tools, lifecycle and context. Rating *is* the
  assessment. Merge.
- `ticket-writer` only reformats the reviewer's output. Same tools, same lifecycle,
  strictly less context. Function.
- `pm` is a job title. It claims no clause at all.

### 2. Website monitoring (timer-driven)

*"Check these pages every ten minutes; tell me when something meaningful changes."*

**Survives the critique — two agents:**

```text
wake_later(10m) --> checker (deterministic diff, no LLM)
                        |
                  site.changed  (only when something material moved)
                        |
                        v
                    explainer (LLM: what changed and does it matter)
```

| Agent | Clause it claims |
|---|---|
| `checker` | different wakeup lifecycle — timer-woken, and it must be cheap enough to run 144 times a day |
| `explainer` | different tools and cost profile; woken by an event, not a clock |

The split exists because of clause 2 and clause 1, not because "checking" and
"explaining" sound like different jobs. The checker uses `ctx.probe(...)` and
`ctx.publish(...)`, reschedules itself with `ctx.wake_later(...)`, and returns. Copy
`app/agents/monitor.py`.

**Does not survive — one LLM agent on a loop:**

```text
agent wakes every 10m -> LLM reads the page -> LLM decides if it changed -> repeat
```

This is an LLM polling loop, which V3 forbids outright ("No LLM polling loops"). It
also burns a model call on 143 no-op checks a day. The rule: **a cheap deterministic
checker runs first, and only a material change wakes a model.**

Also does not survive: a third `notifier` agent. Notification is a side-effectful
tool call at the end of `explainer`, not a mind.

### 3. Code review

*"Review a diff for correctness, style, security and test coverage, then summarise."*

**Survives the critique — one agent, or two:**

```text
review_requested --> reviewer --(review.ready)--> [optional] security-reviewer
```

Start with **one** `reviewer`. Correctness, style and coverage are four prompt
sections and four checklist passes over the same diff with the same tools and the
same lifecycle — no clause is claimed by splitting them.

Add `security-reviewer` as a second agent **only if** it claims a real clause: it runs
a different toolchain (SAST, dependency audit), or it deliberately re-reads the diff
without the first reviewer's conclusions so it is not anchored by them. Say which.

**Does not survive — four reviewers and a synthesiser:**

```text
correctness / style / security / tests  --> synthesiser --> report
```

- Four agents share tools, permissions, context needs and lifecycle. Their only
  distinction is which paragraph of the prompt they were given.
- Parallelism does not rescue it: four model calls over the same diff cost four times
  as much to save a few seconds on work nobody is blocking on.
- `synthesiser` is a reformatter — it takes four outputs and concatenates them with
  judgement that the reviewer already had. Function.

If review latency genuinely blocks a merge queue and you have measured it, split by
**file set**, not by concern — that is real parallelism over disjoint inputs.

## Sentences that should trigger pushback

| What you hear | What to say |
|---|---|
| "We need a PM agent to coordinate." | Coordination with two agents is a `ctx.send`. A director earns its place when there is real fan-out and a synthesis decision. `teams/research/` has no director. |
| "Add a writer agent to clean up the output." | Same tools, same lifecycle, less context. That is a function, or a better prompt. |
| "Let's have them debate." | Debate is a shared transcript. Muster agents are separate minds — see `app/kernel/context_builder.py`. A critic given facts + proposal gets the benefit without the transcript. |
| "One agent per data source." | One agent with per-source tools, unless the sources need different credentials or the fetches are slow and parallel. |
| "We'll start with seven and prune later." | Nobody prunes. Start with one and let a named limitation add the second. |
| "The critic should see the analyst's reasoning." | No. Facts and the proposal only. Exposing a scratchpad as coordination state is V3 agent-design rule 9. |
