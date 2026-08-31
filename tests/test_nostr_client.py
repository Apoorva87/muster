"""NostrClient against an in-process fake relay — no sockets, no ports, no Docker.

The client's only transport dependency is the ``connector`` callable, so a fake
that implements ``send`` / ``recv`` / ``__aiter__`` is a complete relay for test
purposes. Nothing here binds a port or touches the network; the one test that
would need a live Buzz relay is marked ``integration`` and excluded by default.
"""

import asyncio
import json

import pytest

pytest.importorskip("coincurve", reason="needs: uv sync --extra buzz")
pytest.importorskip("websockets", reason="needs: uv sync --extra buzz")

from bus.nostr.client import (  # noqa: E402
    EOSE, NostrClient, RelayError, RelayRejected, RelayTimeout, make_filter)
from bus.nostr.events import (  # noqa: E402
    KIND_AUTH, KIND_CHAT, Event, Identity, chat_message)

RELAY_URL = "ws://relay.test/nostr"


# --------------------------------------------------------------- fake relay


class FakeRelay:
    """One end of a websocket, in memory.

    ``on_send`` lets a test script the relay's behaviour: it is called with the
    decoded client frame and may push replies.
    """

    def __init__(self, on_send=None):
        self.sent: list[str] = []
        self.closed = False
        self.on_send = on_send
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._eof = object()

    # -- transport surface the client uses -------------------------------
    async def send(self, raw: str) -> None:
        self.sent.append(raw)
        if self.on_send is not None:
            await self.on_send(json.loads(raw), self)

    async def recv(self):
        item = await self._inbox.get()
        if item is self._eof:
            raise ConnectionError("relay closed")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is self._eof:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(self._eof)

    # -- test-side controls ----------------------------------------------
    def push(self, message) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def drop(self) -> None:
        """Simulate the socket dying under us."""
        self._inbox.put_nowait(self._eof)

    @property
    def frames(self) -> list[list]:
        return [json.loads(raw) for raw in self.sent]

    def frames_of(self, verb: str) -> list[list]:
        return [f for f in self.frames if f and f[0] == verb]

    async def connect(self, url):  # matches the connector signature
        return self

    async def wait_frame(self, verb: str, index: int = 0, timeout: float = 1.0):
        await until(lambda: len(self.frames_of(verb)) > index, timeout)
        return self.frames_of(verb)[index]


async def until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


def sequence_connector(*relays):
    """A connector handing out each relay in turn — the reconnect harness."""
    pending = list(relays)

    async def connect(url):
        if not pending:
            raise ConnectionError("no relay left")
        return pending.pop(0)

    return connect


@pytest.fixture
def identity():
    return Identity.derive("test-nostr-client")


def client_for(relay, identity, **kwargs):
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("retry_delay", 0.0)
    return NostrClient(RELAY_URL, identity,
                       connector=getattr(relay, "connect", relay), **kwargs)


# ------------------------------------------------------------------ NIP-42


async def test_auth_challenge_is_answered_with_a_signed_22242(identity):
    relay = FakeRelay()
    async with client_for(relay, identity,
                          relay_name="wss://buzz.example") as client:
        relay.push(["AUTH", "challenge-abc123"])
        frame = await relay.wait_frame("AUTH")

        assert frame[0] == "AUTH"
        event = Event.from_dict(frame[1])
        assert event.kind == KIND_AUTH
        assert event.verify(), "auth event must be a valid signed event"
        assert event.pubkey == identity.pubkey
        assert event.tag_value("challenge") == "challenge-abc123"
        assert event.tag_value("relay") == "wss://buzz.example"
        assert client.challenge == "challenge-abc123"

        assert client.authenticated is False
        relay.push(["OK", event.id, True, ""])
        assert await client.wait_authenticated(1.0) is True
        assert client.authenticated is True


async def test_auth_rejection_leaves_client_unauthenticated(identity):
    relay = FakeRelay()
    async with client_for(relay, identity) as client:
        relay.push(["AUTH", "nope"])
        frame = await relay.wait_frame("AUTH")
        relay.push(["OK", frame[1]["id"], False, "invalid: bad challenge"])
        assert await client.wait_authenticated(1.0) is False
        assert client.authenticated is False


async def test_relay_may_challenge_at_any_time(identity):
    """Buzz sends AUTH proactively, but the spec allows it mid-session too."""
    relay = FakeRelay()
    async with client_for(relay, identity) as client:
        relay.push(["NOTICE", "hello"])
        relay.push(["AUTH", "first"])
        first = await relay.wait_frame("AUTH", 0)
        relay.push(["OK", first[1]["id"], True, ""])
        await until(lambda: client.authenticated)

        relay.push(["AUTH", "second"])
        second = await relay.wait_frame("AUTH", 1)
        assert Event.from_dict(second[1]).tag_value("challenge") == "second"
        # A fresh challenge invalidates the old session until re-acked.
        await until(lambda: client.authenticated is False)


# ----------------------------------------------------------------- publish


async def test_publish_returns_true_on_ok(identity):
    async def auto_ok(frame, relay):
        if frame[0] == "EVENT":
            relay.push(["OK", frame[1]["id"], True, ""])

    relay = FakeRelay(on_send=auto_ok)
    async with client_for(relay, identity) as client:
        event = chat_message(identity, "ops", "hello")
        assert await client.publish(event) is True
        sent = relay.frames_of("EVENT")[0]
        assert sent[1]["id"] == event.id
        assert sent[1]["kind"] == KIND_CHAT
        assert ["h", "ops"] in sent[1]["tags"]


async def test_publish_raises_with_the_relays_reason(identity):
    async def refuse(frame, relay):
        if frame[0] == "EVENT":
            relay.push(["OK", frame[1]["id"], False, "blocked: pubkey not in group"])

    relay = FakeRelay(on_send=refuse)
    async with client_for(relay, identity) as client:
        with pytest.raises(RelayRejected) as excinfo:
            await client.publish(chat_message(identity, "ops", "hi"))
    assert "blocked: pubkey not in group" in str(excinfo.value)
    assert excinfo.value.message == "blocked: pubkey not in group"


async def test_publish_times_out_cleanly(identity):
    relay = FakeRelay()  # never answers
    async with client_for(relay, identity) as client:
        with pytest.raises(RelayTimeout):
            await client.publish(chat_message(identity, "ops", "hi"),
                                 timeout=0.05)
        assert client._pending_ok == {}, "timed-out publish must not leak state"
        # The connection is still usable afterwards.
        assert client.connected


async def test_publish_retries_once_after_auth_required(identity):
    """Auth is transparent: the caller never sees the auth-required round trip."""
    state = {"attempts": 0}

    async def gatekeeper(frame, relay):
        if frame[0] == "EVENT":
            state["attempts"] += 1
            if state["attempts"] == 1:
                relay.push(["OK", frame[1]["id"], False,
                            "auth-required: we only take signed members"])
                relay.push(["AUTH", "late-challenge"])
            else:
                relay.push(["OK", frame[1]["id"], True, ""])
        elif frame[0] == "AUTH":
            relay.push(["OK", frame[1]["id"], True, ""])

    relay = FakeRelay(on_send=gatekeeper)
    async with client_for(relay, identity) as client:
        assert await client.publish(chat_message(identity, "ops", "hi")) is True
    assert state["attempts"] == 2
    assert len(relay.frames_of("AUTH")) == 1


async def test_publish_fails_fast_when_the_socket_drops(identity):
    async def kill(frame, relay):
        if frame[0] == "EVENT":
            relay.drop()

    first, second = FakeRelay(on_send=kill), FakeRelay()
    client = NostrClient(RELAY_URL, identity, timeout=1.0, retry_delay=0.0,
                         connector=sequence_connector(first, second))
    async with client:
        with pytest.raises(RelayError):
            await client.publish(chat_message(identity, "ops", "hi"))


# ------------------------------------------------------ filters and fetches


def test_make_filter_spells_the_channel_tag_key():
    f = make_filter(kinds=[KIND_CHAT], channels=["ops"], authors=["ab" * 32],
                    ids=["cd" * 32], since=100, until=200, limit=5,
                    tags={"e": ["ef" * 32], "#p": ["ff" * 32]})
    assert f == {
        "kinds": [9],
        "authors": ["ab" * 32],
        "ids": ["cd" * 32],
        "#h": ["ops"],
        "#e": ["ef" * 32],
        "#p": ["ff" * 32],
        "since": 100,
        "until": 200,
        "limit": 5,
    }
    assert "channels" not in f and "h" not in f


def test_make_filter_omits_absent_fields():
    assert make_filter(kinds=[9]) == {"kinds": [9]}


async def test_req_frame_carries_the_filters_verbatim(identity):
    relay = FakeRelay()
    async with client_for(relay, identity) as client:
        f = make_filter(kinds=[KIND_CHAT], channels=["ops"], limit=10)
        sub_id = await client.subscribe(f, make_filter(kinds=[0]))
        frame = await relay.wait_frame("REQ")
    assert frame == ["REQ", sub_id,
                     {"kinds": [9], "#h": ["ops"], "limit": 10},
                     {"kinds": [0]}]
    assert '"#h"' in relay.sent[0]


async def test_fetch_collects_until_eose(identity):
    events = [chat_message(identity, "ops", f"msg {i}") for i in range(3)]

    async def backlog(frame, relay):
        if frame[0] == "REQ":
            sub_id = frame[1]
            for event in events:
                relay.push(["EVENT", sub_id, event.to_dict()])
            relay.push(["EOSE", sub_id])
            # Live traffic after EOSE must not land in the fetch result.
            relay.push(["EVENT", sub_id,
                        chat_message(identity, "ops", "later").to_dict()])

    relay = FakeRelay(on_send=backlog)
    async with client_for(relay, identity) as client:
        got = await client.fetch(make_filter(kinds=[KIND_CHAT], channels=["ops"]))

    assert [e.content for e in got] == ["msg 0", "msg 1", "msg 2"]
    assert relay.frames_of("CLOSE"), "fetch must close its subscription"


async def test_fetch_times_out_without_eose(identity):
    relay = FakeRelay()
    async with client_for(relay, identity) as client:
        with pytest.raises(RelayTimeout):
            await client.fetch(make_filter(kinds=[9]), timeout=0.05)


async def test_stream_marks_the_backlog_boundary(identity):
    stored = chat_message(identity, "ops", "stored")
    live = chat_message(identity, "ops", "live")

    async def script(frame, relay):
        if frame[0] == "REQ":
            sub_id = frame[1]
            relay.push(["EVENT", sub_id, stored.to_dict()])
            relay.push(["EOSE", sub_id])
            relay.push(["EVENT", sub_id, live.to_dict()])

    relay = FakeRelay(on_send=script)
    seen = []
    async with client_for(relay, identity) as client:
        stream = client.stream(make_filter(kinds=[KIND_CHAT]),
                               include_eose=True)
        async for item in stream:
            seen.append(item)
            if len(seen) == 3:
                break
        await stream.aclose()

    assert seen[0].content == "stored"
    assert seen[1] is EOSE
    assert seen[2].content == "live"


# ------------------------------------------------------------ verification


async def test_tampered_events_are_dropped_not_yielded(identity, caplog):
    good = chat_message(identity, "ops", "authentic")
    tampered = chat_message(identity, "ops", "authentic")
    tampered.content = "I never said this"          # id/sig no longer match
    forged_id = chat_message(identity, "ops", "x")
    forged_id.id = "00" * 32                         # id does not hash the body

    async def script(frame, relay):
        if frame[0] == "REQ":
            sub_id = frame[1]
            relay.push(["EVENT", sub_id, tampered.to_dict()])
            relay.push(["EVENT", sub_id, forged_id.to_dict()])
            relay.push(["EVENT", sub_id, good.to_dict()])
            relay.push(["EOSE", sub_id])

    relay = FakeRelay(on_send=script)
    with caplog.at_level("WARNING", logger="bus.nostr.client"):
        async with client_for(relay, identity) as client:
            got = await client.fetch(make_filter(kinds=[KIND_CHAT]))

    assert [e.content for e in got] == ["authentic"]
    assert sum("unverified" in r.message for r in caplog.records) == 2


async def test_garbage_frames_do_not_kill_the_reader(identity):
    async def script(frame, relay):
        if frame[0] == "EVENT":
            relay.push(["OK", frame[1]["id"], True, ""])

    relay = FakeRelay(on_send=script)
    async with client_for(relay, identity) as client:
        relay._inbox.put_nowait("not json at all")
        relay.push({"not": "a list"})
        relay.push([])
        relay.push(["EVENT"])                      # truncated
        relay.push(["OK", "deadbeef", True, ""])   # unmatched
        relay.push(["WAT", "unknown verb"])
        await asyncio.sleep(0.02)
        assert await client.publish(chat_message(identity, "ops", "still alive"))


# ---------------------------------------------------------- NOTICE / CLOSED


async def test_notice_is_logged_and_ignored(identity, caplog):
    async def script(frame, relay):
        if frame[0] == "EVENT":
            relay.push(["OK", frame[1]["id"], True, ""])

    relay = FakeRelay(on_send=script)
    with caplog.at_level("INFO", logger="bus.nostr.client"):
        async with client_for(relay, identity) as client:
            relay.push(["NOTICE", "restricted: slow down"])
            await asyncio.sleep(0.02)
            assert await client.publish(chat_message(identity, "ops", "hi"))
    assert any("slow down" in r.getMessage() for r in caplog.records)


async def test_closed_ends_the_subscription_without_crashing(identity):
    async def script(frame, relay):
        if frame[0] == "REQ":
            relay.push(["CLOSED", frame[1], "auth-required: authenticate first"])
        elif frame[0] == "EVENT":
            relay.push(["OK", frame[1]["id"], True, ""])

    relay = FakeRelay(on_send=script)
    async with client_for(relay, identity) as client:
        with pytest.raises(RelayError) as excinfo:
            await client.fetch(make_filter(kinds=[9]))
        assert "authenticate first" in str(excinfo.value)
        # Client survives: a later publish still works.
        assert await client.publish(chat_message(identity, "ops", "hi"))


async def test_events_iterator_stops_when_client_closes(identity):
    relay = FakeRelay()
    client = client_for(relay, identity)
    await client.connect()
    sub_id = await client.subscribe(make_filter(kinds=[9]))

    seen = []

    async def drain():
        async for event in client.events(sub_id):
            seen.append(event)

    task = asyncio.create_task(drain())
    await asyncio.sleep(0.02)
    await client.close()
    await asyncio.wait_for(task, 1.0)
    assert seen == []


# ------------------------------------------------------------- reconnection


async def test_reconnect_resends_active_reqs(identity):
    first, second = FakeRelay(), FakeRelay()
    client = NostrClient(RELAY_URL, identity, timeout=1.0, retry_delay=0.0,
                         connector=sequence_connector(first, second))
    async with client:
        f = make_filter(kinds=[KIND_CHAT], channels=["ops"])
        sub_id = await client.subscribe(f, sub_id="sub-fixed")
        await first.wait_frame("REQ")

        first.drop()
        restored = await second.wait_frame("REQ")
        assert restored == ["REQ", "sub-fixed", f]

        # The restored subscription is live: events flow again.
        event = chat_message(identity, "ops", "after reconnect")
        second.push(["EVENT", sub_id, event.to_dict()])
        second.push(["EOSE", sub_id])
        assert await client.wait_eose(sub_id, 1.0)
        iterator = client.events(sub_id)
        assert (await asyncio.wait_for(iterator.__anext__(), 1.0)).content == \
            "after reconnect"
        await iterator.aclose()


async def test_reconnect_gives_up_after_bounded_retries(identity):
    first = FakeRelay()
    attempts = {"n": 0}

    async def flaky(url):
        if attempts["n"] == 0:
            attempts["n"] += 1
            return first
        attempts["n"] += 1
        raise ConnectionError("relay down")

    client = NostrClient(RELAY_URL, identity, timeout=1.0, retry_delay=0.0,
                         max_retries=3, connector=flaky)
    async with client:
        sub_id = await client.subscribe(make_filter(kinds=[9]))
        seen = []

        async def drain():
            async for event in client.events(sub_id):
                seen.append(event)

        task = asyncio.create_task(drain())
        first.drop()
        await asyncio.wait_for(task, 1.0)   # iterator ends, no hang
        assert attempts["n"] == 4           # 1 initial + 3 bounded retries
        assert client.connected is False


async def test_reconnect_reauthenticates(identity):
    async def challenger(frame, relay):
        if frame[0] == "AUTH":
            relay.push(["OK", frame[1]["id"], True, ""])

    first, second = FakeRelay(on_send=challenger), FakeRelay(on_send=challenger)
    client = NostrClient(RELAY_URL, identity, timeout=1.0, retry_delay=0.0,
                         relay_name="wss://buzz.example",
                         connector=sequence_connector(first, second))
    async with client:
        first.push(["AUTH", "one"])
        await until(lambda: client.authenticated)

        first.drop()
        await until(lambda: client.authenticated is False)
        second.push(["AUTH", "two"])
        frame = await second.wait_frame("AUTH")
        assert Event.from_dict(frame[1]).tag_value("challenge") == "two"
        await until(lambda: client.authenticated)


# ------------------------------------------------------------- missing extra


async def test_default_connector_names_the_extra(monkeypatch, identity):
    monkeypatch.setattr("bus.nostr.client.websockets", None)
    client = NostrClient(RELAY_URL, identity)
    with pytest.raises(RuntimeError) as excinfo:
        await client.connect()
    assert "uv sync --extra buzz" in str(excinfo.value)


# ------------------------------------------------------------- integration


@pytest.mark.integration
async def test_against_a_live_buzz_relay():
    """Requires a running Buzz relay; excluded from the default suite."""
    import os

    # No default: ws://localhost:8080 is Restate's ingress on a Muster machine,
    # and pointing a Nostr client at it produces a confusing HTTP 400 rather
    # than an honest skip.
    url = os.environ.get("BUZZ_RELAY_URL", "")
    if not url:
        pytest.skip("set BUZZ_RELAY_URL to a real Buzz relay to run this")
    identity = Identity.derive("muster-integration")
    async with NostrClient(url, identity, relay_name=url) as client:
        assert await client.wait_authenticated(5.0)
        event = chat_message(identity, "muster-test", "integration ping")
        assert await client.publish(event)
        found = await client.fetch(
            make_filter(kinds=[KIND_CHAT], channels=["muster-test"], limit=10),
            timeout=5.0)
        assert any(e.id == event.id for e in found)
