"""The dev relay is exercised over a real WebSocket, not in-process.

Every test binds port 0, connects with a raw ``websockets`` client and speaks
the wire protocol by hand — deliberately not through any Muster client, so
these tests pin the protocol itself. Every network wait has a timeout, so a
regression fails instead of hanging CI.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("coincurve")
pytest.importorskip("websockets")

from websockets.asyncio.client import connect  # noqa: E402

from bus.nostr.dev_relay import DevRelay  # noqa: E402
from bus.nostr.events import (  # noqa: E402
    KIND_CHAT,
    KIND_PROFILE,
    Event,
    Identity,
    auth_response,
    chat_message,
    profile,
)

TIMEOUT = 5.0
SILENCE = 0.3


# ------------------------------------------------------------------ harness


class Client:
    """A raw relay client that buffers messages it was not waiting for."""

    def __init__(self, ws, challenge):
        self.ws = ws
        self.challenge = challenge
        self._pending: list[list] = []

    async def send(self, message: list) -> None:
        await self.ws.send(json.dumps(message))

    async def send_raw(self, text: str) -> None:
        await self.ws.send(text)

    async def recv(self) -> list:
        if self._pending:
            return self._pending.pop(0)
        return json.loads(await asyncio.wait_for(self.ws.recv(), TIMEOUT))

    async def recv_where(self, predicate) -> list:
        for i, message in enumerate(self._pending):
            if predicate(message):
                return self._pending.pop(i)
        deadline = time.monotonic() + TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a matching message")
            message = json.loads(await asyncio.wait_for(self.ws.recv(), remaining))
            if predicate(message):
                return message
            self._pending.append(message)

    async def publish(self, event: Event) -> list:
        await self.send(["EVENT", event.to_dict()])
        return await self.recv_where(
            lambda m: m[0] == "OK" and m[1] == event.id)

    async def req(self, sub_id: str, *filters) -> tuple[list[Event], list]:
        """Send a REQ and drain until EOSE (or CLOSED). Returns (events, end)."""
        await self.send(["REQ", sub_id, *filters])
        events: list[Event] = []
        while True:
            message = await self.recv_where(
                lambda m: m[0] in ("EVENT", "EOSE", "CLOSED") and m[1] == sub_id)
            if message[0] == "EVENT":
                events.append(Event.from_dict(message[2]))
            else:
                return events, message

    async def authenticate(self, identity: Identity, relay_url: str) -> list:
        event = auth_response(identity, challenge=self.challenge, relay=relay_url)
        await self.send(["AUTH", event.to_dict()])
        return await self.recv_where(
            lambda m: m[0] == "OK" and m[1] == event.id)

    async def round_trip(self) -> None:
        """Force the relay to finish everything sent before this call."""
        await self.send(["PING-ISH"])
        await self.recv_where(lambda m: m[0] == "NOTICE")

    async def assert_silent(self, seconds: float = SILENCE) -> None:
        assert not self._pending, f"unexpected buffered messages: {self._pending}"
        try:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), seconds))
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"expected no message, received {message}")


@asynccontextmanager
async def client(relay: DevRelay, *, expect_challenge: bool = True):
    async with connect(relay.url, open_timeout=TIMEOUT) as ws:
        challenge = None
        if expect_challenge:
            hello = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
            assert hello[0] == "AUTH", hello
            challenge = hello[1]
        yield Client(ws, challenge)


@pytest.fixture
async def relay():
    async with DevRelay() as r:
        yield r


@pytest.fixture
async def strict_relay():
    async with DevRelay(require_auth=True) as r:
        yield r


@pytest.fixture
def alice():
    return Identity.derive("alice")


@pytest.fixture
def bob():
    return Identity.derive("bob")


def message(identity: Identity, channel: str, text: str, *, at: int | None = None):
    if at is None:
        return chat_message(identity, channel, text)
    return Event(kind=KIND_CHAT, content=text, pubkey=identity.pubkey,
                 created_at=at, tags=[["h", channel]]).finalize(identity)


# ----------------------------------------------------------------- lifecycle


async def test_start_binds_a_real_free_port():
    relay = DevRelay()
    url = await relay.start()
    try:
        assert url.startswith("ws://127.0.0.1:")
        assert relay.port != 0
        assert relay.url == url
    finally:
        await relay.stop()
    with pytest.raises(RuntimeError):
        _ = relay.url
    await relay.stop()  # idempotent


async def test_context_manager_serves_and_closes():
    async with DevRelay() as relay:
        async with client(relay) as c:
            assert c.challenge


# --------------------------------------------------------------- NIP-42 auth


async def test_auth_challenge_arrives_on_connect(relay):
    async with connect(relay.url, open_timeout=TIMEOUT) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT))
        assert hello[0] == "AUTH"
        assert isinstance(hello[1], str) and len(hello[1]) >= 16


async def test_each_connection_gets_its_own_challenge(relay):
    async with client(relay) as one, client(relay) as two:
        assert one.challenge != two.challenge


async def test_auth_response_is_accepted(relay, alice):
    async with client(relay) as c:
        ok = await c.authenticate(alice, relay.url)
        assert ok[2] is True, ok


async def test_auth_with_wrong_challenge_is_rejected(relay, alice):
    async with client(relay) as c:
        event = auth_response(alice, challenge="not-the-challenge",
                              relay=relay.url)
        await c.send(["AUTH", event.to_dict()])
        ok = await c.recv_where(lambda m: m[0] == "OK" and m[1] == event.id)
        assert ok[2] is False
        assert "challenge" in ok[3]


async def test_require_auth_rejects_unauthenticated_publish(strict_relay, alice):
    async with client(strict_relay) as c:
        ok = await c.publish(message(alice, "#general", "hi"))
        assert ok[2] is False
        assert ok[3].startswith("auth-required:")
    assert strict_relay.events == []


async def test_require_auth_accepts_after_authenticating(strict_relay, alice):
    async with client(strict_relay) as c:
        assert (await c.authenticate(alice, strict_relay.url))[2] is True
        ok = await c.publish(message(alice, "#general", "hi"))
        assert ok[2] is True, ok
    assert len(strict_relay.events) == 1


async def test_require_auth_closes_unauthenticated_subscription(strict_relay):
    async with client(strict_relay) as c:
        _, end = await c.req("s1", {"kinds": [KIND_CHAT]})
        assert end[0] == "CLOSED"
        assert end[2].startswith("auth-required:")


# ------------------------------------------------------------- EVENT / NIP-29


async def test_valid_chat_event_is_accepted(relay, alice):
    async with client(relay) as c:
        event = message(alice, "#general", "hello world")
        ok = await c.publish(event)
        assert ok == ["OK", event.id, True, ""]
    assert [e.content for e in relay.channel("#general")] == ["hello world"]


async def test_kind9_without_h_tag_is_rejected(relay, alice):
    naked = Event(kind=KIND_CHAT, content="no group here",
                  pubkey=alice.pubkey, tags=[]).finalize(alice)
    async with client(relay) as c:
        ok = await c.publish(naked)
        assert ok[2] is False, ok
        assert ok[3].startswith("invalid:")
        assert "'h' tag" in ok[3]
        assert "kind:9" in ok[3]
    assert relay.events == []


async def test_non_chat_kinds_do_not_need_an_h_tag(relay, alice):
    async with client(relay) as c:
        ok = await c.publish(profile(alice, name="Alice"))
        assert ok[2] is True, ok
    assert relay.events[0].kind == KIND_PROFILE


async def test_tampered_content_is_rejected(relay, alice):
    event = message(alice, "#general", "transfer $5")
    event.content = "transfer $5000"
    async with client(relay) as c:
        ok = await c.publish(event)
        assert ok[2] is False
        assert "id does not match" in ok[3]
    assert relay.events == []


async def test_forged_signature_is_rejected(relay, alice, bob):
    event = message(alice, "#general", "signed by alice")
    event.content = "actually written by bob"
    event.id = event.compute_id()  # id now consistent, signature is not
    async with client(relay) as c:
        ok = await c.publish(event)
        assert ok[2] is False
        assert "signature" in ok[3]
    assert relay.events == []


async def test_duplicate_event_is_stored_once(relay, alice):
    event = message(alice, "#general", "once")
    async with client(relay) as c:
        assert (await c.publish(event))[2] is True
        second = await c.publish(event)
        assert second[2] is True
        assert second[3].startswith("duplicate:")
    assert len(relay.events) == 1


# ----------------------------------------------------------------------- REQ


async def test_req_returns_stored_events_then_eose(relay, alice):
    async with client(relay) as c:
        for text in ("one", "two"):
            assert (await c.publish(message(alice, "#general", text)))[2] is True
        events, end = await c.req("history", {"kinds": [KIND_CHAT]})
        assert end == ["EOSE", "history"]
        assert {e.content for e in events} == {"one", "two"}


async def test_live_subscription_receives_later_events(relay, alice, bob):
    async with client(relay) as reader, client(relay) as writer:
        _, end = await reader.req("live", {"#h": ["#general"]})
        assert end[0] == "EOSE"
        published = message(bob, "#general", "published after subscribing")
        assert (await writer.publish(published))[2] is True
        pushed = await reader.recv_where(
            lambda m: m[0] == "EVENT" and m[1] == "live")
        assert Event.from_dict(pushed[2]).content == "published after subscribing"


async def test_h_filter_only_returns_that_channel(relay, alice):
    async with client(relay) as c:
        await c.publish(message(alice, "#general", "general talk"))
        await c.publish(message(alice, "#random", "random talk"))
        events, _ = await c.req("scoped", {"#h": ["#general"]})
        assert [e.content for e in events] == ["general talk"]


async def test_kinds_filter(relay, alice):
    async with client(relay) as c:
        await c.publish(message(alice, "#general", "a chat"))
        await c.publish(profile(alice, name="Alice"))
        events, _ = await c.req("profiles", {"kinds": [KIND_PROFILE]})
        assert [e.kind for e in events] == [KIND_PROFILE]


async def test_authors_filter(relay, alice, bob):
    async with client(relay) as c:
        await c.publish(message(alice, "#general", "from alice"))
        await c.publish(message(bob, "#general", "from bob"))
        events, _ = await c.req("mine", {"authors": [bob.pubkey]})
        assert [e.content for e in events] == ["from bob"]


async def test_ids_and_time_range_filters(relay, alice):
    now = int(time.time())
    old = message(alice, "#general", "old", at=now - 500)
    new = message(alice, "#general", "new", at=now)
    async with client(relay) as c:
        await c.publish(old)
        await c.publish(new)

        by_id, _ = await c.req("byid", {"ids": [old.id]})
        assert [e.content for e in by_id] == ["old"]

        recent, _ = await c.req("recent", {"since": now - 10})
        assert [e.content for e in recent] == ["new"]

        ancient, _ = await c.req("ancient", {"until": now - 100})
        assert [e.content for e in ancient] == ["old"]


async def test_multiple_filters_are_ored(relay, alice, bob):
    async with client(relay) as c:
        await c.publish(message(alice, "#general", "alice here"))
        await c.publish(message(bob, "#random", "bob elsewhere"))
        events, _ = await c.req(
            "either", {"#h": ["#general"]}, {"authors": [bob.pubkey]})
        assert {e.content for e in events} == {"alice here", "bob elsewhere"}


async def test_limit_returns_newest_first(relay, alice):
    now = int(time.time())
    async with client(relay) as c:
        for offset, text in enumerate(("oldest", "middle", "newest")):
            await c.publish(message(alice, "#general", text, at=now - 100 + offset))
        events, _ = await c.req("tail", {"#h": ["#general"], "limit": 2})
        assert [e.content for e in events] == ["newest", "middle"]


# ------------------------------------------------------------------- fan-out


async def test_two_clients_in_a_channel_both_receive(relay, alice, bob):
    async with client(relay) as one, client(relay) as two, client(relay) as poster:
        for reader, sub in ((one, "a"), (two, "b")):
            _, end = await reader.req(sub, {"#h": ["#standup"]})
            assert end[0] == "EOSE"
        sent = message(alice, "#standup", "morning, everyone")
        assert (await poster.publish(sent))[2] is True
        for reader, sub in ((one, "a"), (two, "b")):
            pushed = await reader.recv_where(
                lambda m, s=sub: m[0] == "EVENT" and m[1] == s)
            assert pushed[2]["id"] == sent.id


async def test_fanout_respects_each_subscriptions_filter(relay, alice):
    async with client(relay) as reader, client(relay) as writer:
        await reader.req("only-random", {"#h": ["#random"]})
        assert (await writer.publish(message(alice, "#general", "nope")))[2] is True
        await reader.assert_silent()


async def test_close_stops_delivery(relay, alice):
    async with client(relay) as reader, client(relay) as writer:
        await reader.req("live", {"#h": ["#general"]})
        await reader.send(["CLOSE", "live"])
        await reader.round_trip()  # relay has definitely processed the CLOSE
        assert (await writer.publish(message(alice, "#general", "after close")))[2]
        await reader.assert_silent()


# ------------------------------------------------------------------ robustness


async def test_malformed_json_gets_a_notice_and_the_connection_survives(
        relay, alice):
    async with client(relay) as c:
        await c.send_raw("{not json at all")
        notice = await c.recv_where(lambda m: m[0] == "NOTICE")
        assert "JSON" in notice[1]
        assert (await c.publish(message(alice, "#general", "still alive")))[2]


async def test_unknown_message_type_gets_a_notice(relay):
    async with client(relay) as c:
        await c.send(["FROBNICATE", "whatever"])
        notice = await c.recv_where(lambda m: m[0] == "NOTICE")
        assert "unsupported" in notice[1]


async def test_non_array_and_garbage_shapes_never_crash(relay, alice):
    async with client(relay) as c:
        for junk in ('{"a": 1}', "[]", '["EVENT"]', '["EVENT", 7]',
                     '["REQ"]', '["CLOSE"]', '["AUTH"]',
                     '["EVENT", {"kind": "nope"}]'):
            await c.send_raw(junk)
            reply = await c.recv_where(lambda m: m[0] in ("NOTICE", "OK"))
            assert reply[0] in ("NOTICE", "OK")
        assert (await c.publish(message(alice, "#general", "survived")))[2]


async def test_many_concurrent_clients(relay, alice):
    async def one(index: int) -> str:
        async with client(relay) as c:
            ok = await c.publish(message(alice, "#load", f"msg-{index}"))
            assert ok[2] is True, ok
            return ok[1]

    ids = await asyncio.wait_for(
        asyncio.gather(*(one(i) for i in range(12))), TIMEOUT)
    assert len(set(ids)) == 12
    assert len(relay.channel("#load")) == 12


# -------------------------------------------------------------- test helpers


async def test_events_channel_and_clear_helpers(relay, alice):
    async with client(relay) as c:
        await c.publish(message(alice, "#general", "general"))
        await c.publish(message(alice, "#random", "random"))
    assert len(relay.events) == 2
    assert [e.content for e in relay.channel("#general")] == ["general"]
    assert relay.channel("#nobody") == []
    relay.clear()
    assert relay.events == []
    assert relay.channel("#general") == []
