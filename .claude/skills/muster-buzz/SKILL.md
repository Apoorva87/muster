---
name: muster-buzz
description: Put a Muster team into a Buzz chat room so humans can start projects and approve decisions from chat. Use when the user says "connect my team to Buzz", "run muster from chat", "set up a Buzz room", "drive agents from Buzz", "start a project from chat", or asks how humans and agents share a room.
---

# Put a Muster team in a Buzz room

Buzz is Block's open-source Nostr workspace where humans and agents share
channels. Muster teams can post progress into a room and take commands back
from it — so a project starts by someone typing `run <objective>` instead of
touching a CLI.

**Before anything else, say this out loud to the user**, because getting it
wrong is the expensive mistake:

> Buzz is the **control plane**, not the transport. Durable coordination stays
> on Restate. If the relay goes down, the team keeps working and the room just
> goes quiet. Buzz is where humans watch and decide — never where agents
> coordinate with each other.

If the user wants agent-to-agent messaging through Buzz, stop and explain that
agent↔agent goes over the bus as references (`bus/adapters/restate.py`); the
room is for human↔agent only. A chat transcript is not agent memory.

---

## Step 0 — Does a team exist?

```bash
ls teams/
uv run python -c "from app.kernel.team_spec import load_team_spec as l; print(l('teams/<id>').agent_names)"
```

If there is no suitable team, **use the `muster-new` skill first** and come back.
Do not invent a team here — that skill owns the topology critique.

---

## Step 1 — Interview, in one batch

Ask all of these together (use AskUserQuestion where the harness offers it).
Do not interrogate one at a time. Defaults in brackets are good enough to
proceed with if the user shrugs.

**Room and reach**
1. Which team is going into the room? [the only one in `teams/`]
2. Where is the relay? — a Buzz deployment you self-host, or the local dev
   relay for trying it out? [dev relay]
3. One channel for the whole team, or one per project? [one per team]

**Who may command**
4. Who is allowed to start work by typing in the room — anyone in the channel,
   or a named allow-list? [allow-list; see the security note below]
5. Should the team obey messages from *other* agents, or only humans? [humans only]

**What the room sees**
6. Which moments should humans see? The default is: task started, task
   completed, proposal ready, critique ready, approval waiting, decision
   completed, and when an agent failed or a team failed. [default]
7. Anything that must **never** appear in the room — customer data, credentials,
   internal reasoning? [artifact bodies are already never posted]

**Decisions**
8. Which steps need a human to approve before the team continues?
9. What should happen if nobody answers — wait indefinitely, or time out? [wait]

**Identity**
10. Development (keys derived from a label) or real deployment (proper secrets)?
    [dev]

### The two answers that actually matter

Push back if either of these is loose — everything else is cosmetic.

- **Q4, who may command.** An open channel means anyone who can post can spend
  your model budget and trigger side effects. Recommend an allow-list of
  pubkeys from the start; it is one line of config and painful to retrofit
  after an incident.
- **Q10, identity.** `Identity.derive("muster/<team>/<agent>")` is a *development*
  convenience: the seed **is** the private key, so anyone who reads the repo can
  impersonate your agents. Fine locally, never in a shared or public room. For a
  real deployment, generate secrets and register them.

---

## Step 2 — Explain what the room will look like

Show the user this before wiring anything, so expectations are right:

```
14:59:55       you  run Evaluate whether Acme Corp is attractive at 31x earnings
15:00:09  director  ▶ director started
15:00:09  research  ▶ research started
15:00:09  director  📋 proposal ready — art_dd3af1a76e014bf1
15:00:09    critic  ⚔ critique ready — art_65b65bec9570405b
15:00:09  director  ⏸ needs your decision
                    │ Approve task_6fd4002a2178424e?
                    │ Reply approve or reject.
15:00:09       you  approve
15:00:10  director  🏁 decision recorded: approve — project complete
```

Three things to point out:

- **Each agent posts under its own key**, so the room shows who actually spoke —
  not one "muster-bot" saying everything.
- **Artifacts cross as references** (`art_…`), never bodies. The room stays
  readable and nothing large or sensitive is duplicated into chat history.
- **Internal events never appear.** `event.delivered`, `event.published`,
  `wakeup.scheduled`, tool calls, retries and token counts are filtered out by
  an allow-list in `bus/adapters/buzz.py`. A room is not a log file.

---

## Step 3 — Try it locally first

Always run the local demo before touching a real deployment. It uses Muster's
own `DevRelay`, a genuine NIP-01/29/42 relay, so the code path is identical.

```bash
uv sync --extra buzz
make buzz-demo
```

Then with the user's own team and objective:

```bash
uv run --extra buzz python -m demo.buzz_session \
    --objective "<their objective>"
```

Read `demo/buzz_session.py` with them — it is ~150 lines and is the reference
wiring: connect, `announce_agents`, `project_timeline`, `request_approval`,
`BuzzCommandListener`.

---

## Step 4 — Point at a real Buzz relay

Self-hosting Buzz needs Docker (relay + Postgres + Redis + MinIO) — see
`github.com/block/buzz`. Once it is up:

```bash
uv run --extra buzz python -m demo.buzz_session --relay ws://localhost:8080
```

Nothing else changes. If the relay requires authentication, the client performs
the NIP-42 challenge/response automatically.

For a persistent deployment, set in `.env`:

```env
BUZZ_RELAY_URL=ws://localhost:8080
BUZZ_CHANNEL=<channel-uuid>
BUZZ_TEAM=<team-id>
```

**Channel note:** Buzz channels are NIP-29 groups and a message must carry an
`h` tag naming the channel. Get the channel UUID from the Buzz UI or its REST
API; a made-up name will be rejected by a real relay (the dev relay accepts any
string, which is a difference worth stating).

---

## Step 5 — Verify, with output

Do not report success without showing these:

```bash
uv run --extra buzz pytest tests/test_buzz.py tests/test_buzz_demo.py -q
```

Then confirm, from the demo's own summary:

- `internal events leaked into the room: 0`
- `artifact bodies posted: 0`
- `every one verifies: True`

---

## Step 6 — Teach the commands

| In the room | What happens |
|---|---|
| `run <objective>` | starts a project; the team works and posts progress |
| `start <objective>` | the same thing — an alias, because people type both |
| `approve` / `reject` | answers the pending decision, resuming the workflow |
| `status` | what is running and what is waiting |
| `help` | the same list, posted by the director |

`@muster` is optional — a dedicated channel needs no prefix. Ordinary
conversation is never treated as a command; only these verbs are.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Message rejected by a real relay | kind:9 needs an `h` tag naming a real channel UUID |
| Nothing appears in the room | the topic is not in `SEMANTIC_TOPICS` — that is the allow-list working |
| The team ignores you | your pubkey is not in the listener's allow-list |
| The team replies to itself | the agents' pubkeys are missing from `ignore` |
| Room is quiet but work continues | correct — Buzz is not the transport |

## Files worth reading with the user

- `bus/adapters/buzz.py` — the projection allow-list; the one file to edit to
  change what a room sees
- `bus/adapters/buzz_live.py` — projection and command parsing
- `demo/buzz_session.py` — the reference wiring
- `reference/rooms.md` — how to lay out channels, identities and permissions
- `reference/operating.md` — running it day to day
