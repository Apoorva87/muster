"""Demo: hand a Muster team over to a Buzz room and drive it from chat.

Runs a real Nostr relay in-process, connects a human and the team's agents as
separate cryptographic identities, and then does everything through chat:

    human   > run Evaluate whether Acme is attractive at 31x earnings
    agents  ... work, posting semantic progress only
    director> needs your decision
    human   > approve
    director> decision recorded

No Docker, no Buzz binary, no model endpoint required. The relay is Muster's
own ``DevRelay`` — a real NIP-01/29/42 relay, not a mock — so pointing
``--relay`` at a Block Buzz deployment runs the identical code path.

    uv run --extra buzz python -m demo.buzz_session
    uv run --extra buzz python -m demo.buzz_session --provider ollama --model llama3.2:3b
    uv run --extra buzz python -m demo.buzz_session --relay ws://localhost:8080
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

from app.config import load_settings
from app.launcher import Launcher
from app.runtime.llm import LLMRegistry
from bus.adapters.buzz_live import (AgentIdentities, BuzzCommandListener,
                                    BuzzControlPlane)
from bus.adapters.buzz_transport import connect
from bus.nostr.dev_relay import DevRelay
from bus.nostr.events import Identity, chat_message

CHANNEL = "muster-investment"
TEAM = "investment"
AGENTS = ["director", "research", "finance", "critic", "monitor"]

DIM, BOLD, CYAN, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[33m", "\033[0m")


def banner(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}\n{DIM}{'─' * len(text)}{RESET}")


class Room:
    """Prints the channel as a human in Buzz would see it."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.seen: set[str] = set()

    def show(self, event) -> None:
        if event.id in self.seen or event.kind != 9:
            return
        self.seen.add(event.id)
        who = self.names.get(event.pubkey, event.pubkey[:8])
        colour = CYAN if who == "you" else GREEN
        stamp = time.strftime("%H:%M:%S", time.localtime(event.created_at))
        first, *rest = event.content.splitlines()
        print(f"  {DIM}{stamp}{RESET} {colour}{who:>9}{RESET}  {first}")
        for line in (r for r in rest if r.strip()):
            print(f"           {DIM}│{RESET} {line}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", default="", help="an existing Buzz/Nostr relay")
    parser.add_argument("--provider", default="", help="stub | ollama | anthropic | ...")
    parser.add_argument("--model", default="")
    parser.add_argument("--objective",
                        default="Evaluate whether Acme Corp is attractive at 31x earnings")
    parser.add_argument("--reject", action="store_true", help="take the rejection path")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.provider:
        settings.llm_provider = args.provider
    if args.model:
        settings.llm_model = args.model

    relay: DevRelay | None = None
    relay_url = args.relay
    if not relay_url:
        relay = DevRelay()
        relay_url = await relay.start()

    banner("1. A Buzz room, and everyone in it")
    print(f"  relay      {relay_url}"
          + ("" if args.relay else f"  {DIM}(local dev relay — real protocol){RESET}"))
    print(f"  channel    #{CHANNEL}")

    identities = AgentIdentities()
    human = Identity.derive("human/apoorva")
    names = {human.pubkey: "you"}
    for agent in AGENTS:
        names[identities.for_agent(TEAM, agent).pubkey] = agent

    human_client, human_tx = await connect(relay_url, human)
    bot_client, bot_tx = await connect(relay_url, identities.for_agent(TEAM, "director"))

    control = BuzzControlPlane(transport=bot_tx, channel=CHANNEL, team_id=TEAM,
                               identities=identities)
    await control.announce_agents(AGENTS)
    for agent in AGENTS:
        print(f"  {agent:<10} {DIM}{identities.for_agent(TEAM, agent).pubkey[:32]}…{RESET}")
    print(f"  {'you':<10} {DIM}{human.pubkey[:32]}…{RESET}")
    print(f"\n  {DIM}Every agent is its own keypair — the room shows who actually spoke.{RESET}")

    room = Room(names)
    since = int(time.time())
    listener = BuzzCommandListener(transport=human_tx, channel=CHANNEL,
                                   control=control,
                                   ignore={i for i in names if names[i] != "you"})

    # Mirror the channel to the terminal exactly as a Buzz client would render it.
    async def mirror() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            async for event in human_tx.listen(CHANNEL, since=since):
                room.show(event)

    mirror_task = asyncio.create_task(mirror())

    llm = LLMRegistry(provider=settings.llm_provider, model=settings.llm_model)
    launcher = Launcher(teams=[f"teams/{TEAM}"], settings=settings, llm=llm,
                        artifact_root=Path(settings.artifact_root))

    banner(f"2. Driving the team from chat  {DIM}(model: {llm.describe()}){RESET}")

    async def human_says(text: str) -> None:
        await human_tx.post(chat_message(human, CHANNEL, text))
        await asyncio.sleep(0.15)

    await human_says(f"run {args.objective}")

    command = await anext(aiter(listener.commands()))
    result = await launcher.launch(command.argument, team=TEAM, auto_approve=None)

    artifact_types = {a.task_id: a.type for a in result.artifacts}
    await control.project_timeline(
        result.runs, artifact_types={r.id: artifact_types.get(r.task_id)
                                     for r in result.runs})

    parked = result.waiting[0] if result.waiting else None
    if parked is not None:
        await control.request_approval(parked, f"Approve {parked.task_id}?")
    await asyncio.sleep(0.3)

    banner("3. The human decides — in the room, not a CLI")
    decision = "reject" if args.reject else "approve"
    await human_says(decision)

    answer = await anext(aiter(listener.commands()))
    if parked is not None and answer.is_decision:
        await launcher.resolve(parked.id, answer.verb)
        await control.say("director",
                          f"🏁 decision recorded: **{answer.verb}** — project complete")
    await asyncio.sleep(0.3)

    banner("4. What the relay actually holds")
    if relay is not None:
        chat = [e for e in relay.events if e.kind == 9]
        print(f"  {len(relay.events)} signed events stored, {len(chat)} of them chat")
        print(f"  every one verifies: {all(e.verify() for e in relay.events)}")
        internal = {"event.delivered", "event.published", "wakeup.scheduled"}
        leaked = [e for e in chat if (e.tag_value('t') or '') in internal]
        print(f"  internal events leaked into the room: {len(leaked)}")
        bodies = [e for e in chat if len(e.content) > 400]
        print(f"  artifact bodies posted: {len(bodies)}  {DIM}(references only){RESET}")

    print(f"\n  {DIM}Same code against a real Buzz deployment: --relay ws://your-buzz{RESET}")

    mirror_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await mirror_task
    await human_client.close()
    await bot_client.close()
    if relay is not None:
        await relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
