# Operating a team from a Buzz room

## Day to day

```
run <objective>      start a project
start <objective>    alias for run
approve / reject     answer the pending decision
status               what is running, what is waiting
help                 the command list
```

A failure is projected too: if an agent or a team fails, the room says so
rather than going quiet — `system.agent.failed` and `system.team.failed` are
in the allow-list for exactly that reason.

`@muster` is optional. Ordinary conversation is never a command — only those
verbs, at the start of a message.

## Starting the connection

```bash
uv sync --extra buzz
uv run --extra buzz python -m demo.buzz_session --relay ws://your-buzz
```

For a long-running process, the wiring is the same four objects the demo uses:

```python
client, transport = await connect(relay_url, identity)
control  = BuzzControlPlane(transport=transport, channel=CHANNEL, team_id=TEAM)
listener = BuzzCommandListener(transport=transport, channel=CHANNEL,
                               control=control, ignore=agent_pubkeys)
launcher = Launcher(teams=[f"teams/{TEAM}"])
```

Then: read commands from `listener.commands()`, call `launcher.launch(...)` with
`auto_approve=None`, project the resulting runs, and resolve decisions with
`launcher.resolve(run_id, verb)`.

## Restart behaviour

A command listener should subscribe with `since=<now>` rather than replaying the
stored backlog. Otherwise a restart re-reads yesterday's `run …` messages and
launches yesterday's work again. `NostrChatTransport.listen(channel, since=…)`
takes that parameter for this reason.

Projection is idempotent per run id within a process (`control.posted`), but that
set does not survive a restart. If double-posting after a restart matters,
persist it or re-read the channel and skip runs already mentioned.

## When the relay is down

The team keeps working. Restate owns durability; Buzz only observes. Expect:

- progress lines missing for the outage window
- commands typed during the outage never seen (they were never delivered)
- approvals still pending afterwards — the promise is durable, so answer late

Do not add retry-and-replay of missed projections without deciding what a
duplicate line costs. Usually a gap is better than a flood.

## Cost and noise

Every projected line is a signed event stored forever in the relay's Postgres.
The allow-list keeps that proportional to *decisions*, not to work done. If a
room feels noisy, the fix is to remove a topic from `SEMANTIC_TOPICS` — not to
add filtering downstream.

An open channel means anyone who can post can spend model budget. Set `allow`
before that matters, not after.

## Health checks

```bash
uv run --extra buzz pytest tests/test_buzz.py tests/test_buzz_demo.py -q
make buzz-demo          # full loop against a local relay
```

The demo prints three invariants worth watching in any deployment:

```
internal events leaked into the room: 0
artifact bodies posted: 0
every one verifies: True
```
