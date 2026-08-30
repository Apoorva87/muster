# Designing a Buzz room for a Muster team

## Channel layout

| Shape | When it fits | Cost |
|---|---|---|
| **One channel per team** | default; a small team, a few projects at a time | busy channels when several projects overlap |
| One channel per project | long-running projects, many watchers, an audit trail per engagement | someone must create and archive channels |
| One channel per environment | staging vs production teams sharing a workspace | duplicated wiring |

Start with one channel per team. Splitting later is a config change; merging a
history is not.

## Identity

Every agent is a keypair. Two modes:

```python
# Development — deterministic from a label. The seed IS the private key.
identities.for_agent("investment", "critic")

# Deployment — a real secret you generated and stored.
identities.register("investment", "critic", secret_hex)
```

Derived identities are reproducible across restarts, which is what makes a demo
legible: the same agent keeps the same key and the room shows a stable name.
That same property makes them unsafe anywhere the repo is readable by someone
who should not be able to post as your agents.

Publish a `kind:0` profile per agent (`announce_agents`) or the room shows raw
hex instead of names.

## Who may command

```python
BuzzCommandListener(
    transport=..., channel=..., control=...,
    allow={boss_pubkey, oncall_pubkey},   # empty = anyone in the channel
    ignore={every agent pubkey},          # never obey ourselves
)
```

`ignore` is not optional. Without it a line the team posts can be read back as
an instruction, and two agents can talk each other into a loop.

`allow` is empty by default because a private channel is already a boundary. If
the channel is shared, or the team can spend money or touch production, set it.

## What the room sees

`bus/adapters/buzz.py` holds two frozensets and a mapping:

- `SEMANTIC_TOPICS` — the only topics that may ever be posted
- `NEVER_PROJECTED` — named exclusions, so the rule is testable rather than folklore
- `RUN_EVENT_TO_TOPIC` / `ARTIFACT_TO_TOPIC` — how Muster's internal run events
  map onto that vocabulary

It is an **allow-list**: a new internal event is invisible until someone adds it
deliberately. That is the safe direction to fail. To surface something new, add
it to both the topic set and the mapping, and add a test.

Do not map an artifact to a topic that implies a later stage than it represents.
A `synthesis` is written *before* the human is asked, so mapping it to
`decision.completed` tells the room a decision was made while the workflow is
still parked waiting for one.

## Approvals

A parked run carries an `awakeable_id` — a durable promise. The room message
carries it in a `muster-awakeable` tag, and answering resolves it.

While parked, no model runs and no tokens are spent. The workflow can wait for
days across process restarts; the promise lives in Restate, not in memory.

Never post an approval prompt without binding the awakeable — the room would
show a question nothing can answer. `request_approval` refuses a run that is not
parked, for exactly this reason.

## What never goes in a room

- **Artifact bodies.** References only. The store is the source of truth.
- **Another agent's reasoning.** The isolation that makes an independent critic
  worth having does not survive being posted.
- **Credentials, customer data, anything you would not put in a group chat.**
  Room history is signed, replicated and searchable — that is a feature for an
  audit trail and a liability for a secret.
