"""A real (if minimal) Nostr relay for local development.

This is **not a mock**. It speaks the actual NIP-01 wire protocol over
WebSocket, recomputes and verifies every event's id and schnorr signature,
enforces the NIP-29 rule that a ``kind:9`` message must carry an ``h`` tag,
and issues NIP-42 ``AUTH`` challenges on connect exactly like Block's Buzz
relay does. Code written against this relay runs unchanged against real
Buzz — that is the whole point of building it rather than stubbing the
transport. Docker is not always available; a laptop always is.

What it deliberately does **not** do (use real Buzz for any of it):

* **No persistence.** Everything lives in a Python list and dies with the
  process. There is no Postgres, no migration, no replay after restart.
* **No auth policy.** ``require_auth`` is a single boolean. There are no
  groups-with-members, no admin roles, no NIP-29 moderation events, no
  invite or kick handling, no per-channel ACLs.
* **No rate limiting, no spam control, no payments, no bans.**
* **No S3/MinIO blob offload** for large content, and no attachment handling.
* **No search** (NIP-50), no NIP-45 counts, no NIP-40 expiration, no event
  deletion (NIP-09), no replaceable/ephemeral-kind semantics — a kind:0
  profile is stored alongside every other event rather than replacing the
  previous one.
* **No TLS, no reverse proxy, no clustering.** It binds loopback by default.

Wire protocol implemented (both directions, exactly):

client -> relay
    ``["EVENT", <event>]``
    ``["REQ", "<sub_id>", <filter>, ...]``
    ``["CLOSE", "<sub_id>"]``
    ``["AUTH", <signed kind-22242 event>]``

relay -> client
    ``["EVENT", "<sub_id>", <event>]``
    ``["OK", "<id>", true|false, "<message>"]``
    ``["EOSE", "<sub_id>"]``
    ``["AUTH", "<challenge>"]``
    ``["NOTICE", "<message>"]``
    ``["CLOSED", "<sub_id>", "<message>"]``

Filters follow NIP-01: ``kinds``, ``authors``, ``ids``, ``since``, ``until``,
``limit`` and generic single-letter tag filters (``#h``, ``#e``, ``#p``, ...).
Fields inside one filter are AND-ed, list values inside a field are OR-ed, and
multiple filters in one ``REQ`` are OR-ed.

Usage::

    async with DevRelay() as relay:
        url = relay.url                    # ws://127.0.0.1:<free port>
        ...
        relay.channel("#general")          # what got posted, for a demo

This module imports nothing from ``app/`` and nothing from ``bus/`` other than
``bus.nostr.events``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from bus.nostr.events import KIND_AUTH, KIND_CHAT, Event

try:  # ``websockets`` ships in the optional ``buzz`` extra.
    import websockets
    from websockets.asyncio.server import ServerConnection, serve
except ImportError:  # pragma: no cover - exercised only without the extra
    websockets = None  # type: ignore[assignment]
    ServerConnection = Any  # type: ignore[misc,assignment]
    serve = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_MISSING_H = ("invalid: kind:9 events require an 'h' tag naming the NIP-29 "
              "group — Buzz rejects these too")


# --------------------------------------------------------------- filtering


def _matches(event: Event, flt: dict[str, Any]) -> bool:
    """NIP-01 filter match: fields AND-ed, values within a field OR-ed."""
    if not isinstance(flt, dict):
        return False
    for key, want in flt.items():
        if key == "limit":
            continue
        if key == "since":
            if event.created_at < int(want):
                return False
        elif key == "until":
            if event.created_at > int(want):
                return False
        elif key == "ids":
            if event.id not in want:
                return False
        elif key == "authors":
            if event.pubkey not in want:
                return False
        elif key == "kinds":
            if event.kind not in want:
                return False
        elif key.startswith("#") and len(key) == 2:
            if not set(event.tag_values(key[1])) & set(want):
                return False
        # Unknown filter keys are ignored rather than failing the match,
        # which is how relays stay forward-compatible with new NIPs.
    return True


def _matches_any(event: Event, filters: list[dict[str, Any]]) -> bool:
    return any(_matches(event, f) for f in filters) if filters else False


# ------------------------------------------------------------- connection


class _Connection:
    """Per-client state: its challenge, its auth status, its subscriptions."""

    __slots__ = ("ws", "challenge", "pubkey", "subscriptions")

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.challenge = secrets.token_hex(16)
        self.pubkey: str | None = None  # set once NIP-42 AUTH succeeds
        self.subscriptions: dict[str, list[dict[str, Any]]] = {}

    @property
    def authenticated(self) -> bool:
        return self.pubkey is not None

    async def send(self, message: list[Any]) -> None:
        await self.ws.send(json.dumps(message))

    async def try_send(self, message: list[Any]) -> None:
        """Fan-out send: a client that vanished must not break the relay."""
        try:
            await self.send(message)
        except Exception:  # noqa: BLE001 - closed/broken peer, drop it
            log.debug("dev relay: dropping message to a closed connection")


# ------------------------------------------------------------------ relay


class DevRelay:
    """An in-memory NIP-01/29/42 relay you can start inside a test.

    :param host: interface to bind. Loopback by default.
    :param port: ``0`` asks the OS for a free port — read the real one back
        from :meth:`start` or :attr:`url`.
    :param require_auth: when true, ``EVENT`` and ``REQ`` from a connection
        that has not completed NIP-42 ``AUTH`` are refused with an
        ``auth-required:`` message. The default is false so local dev has no
        friction; the strict path exists so client code can be tested against
        a relay that behaves like a locked-down deployment.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *,
                 require_auth: bool = False) -> None:
        self.host = host
        self.port = port
        self.require_auth = require_auth
        self._events: list[Event] = []
        self._ids: set[str] = set()
        self._seq = 0
        self._order: dict[str, int] = {}
        self._connections: set[_Connection] = set()
        self._server: Any = None
        self._url: str | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("relay is not running — await start() first")
        return self._url

    async def start(self) -> str:
        """Bind and begin serving. Returns the real ``ws://host:port``."""
        if websockets is None:  # pragma: no cover - needs the extra missing
            raise RuntimeError(
                "DevRelay needs the 'websockets' package, which lives in the "
                "optional 'buzz' extra. Install it with: uv sync --extra buzz")
        if self._server is not None:
            return self.url
        self._server = await serve(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._url = f"ws://{self.host}:{self.port}"
        log.info("dev relay listening on %s", self._url)
        return self._url

    async def stop(self) -> None:
        """Close the listener and every live connection. Idempotent."""
        if self._server is None:
            return
        server, self._server, self._url = self._server, None, None
        server.close()
        await server.wait_closed()
        self._connections.clear()

    async def __aenter__(self) -> "DevRelay":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # -------------------------------------------------------- test helpers

    @property
    def events(self) -> list[Event]:
        """Every accepted event, oldest first (arrival order)."""
        return list(self._events)

    def channel(self, name: str) -> list[Event]:
        """Accepted events tagged into one NIP-29 group, oldest first."""
        return [e for e in self._events if e.channel == name]

    def clear(self) -> None:
        """Forget every stored event. Live subscriptions stay open."""
        self._events.clear()
        self._ids.clear()
        self._order.clear()
        self._seq = 0

    # ------------------------------------------------------------- serving

    async def _handle(self, ws: ServerConnection) -> None:
        conn = _Connection(ws)
        self._connections.add(conn)
        try:
            # NIP-42: Buzz challenges proactively on connect, so we do too.
            await conn.send(["AUTH", conn.challenge])
            async for raw in ws:
                await self._dispatch(conn, raw)
        except Exception:  # noqa: BLE001 - a dead client is not an error
            log.debug("dev relay: connection ended", exc_info=True)
        finally:
            self._connections.discard(conn)

    async def _dispatch(self, conn: _Connection, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            await conn.try_send(["NOTICE", "invalid: message is not valid JSON"])
            return
        if not isinstance(message, list) or not message:
            await conn.try_send(
                ["NOTICE", "invalid: message must be a non-empty JSON array"])
            return

        verb = message[0]
        try:
            if verb == "EVENT":
                await self._on_event(conn, message)
            elif verb == "REQ":
                await self._on_req(conn, message)
            elif verb == "CLOSE":
                await self._on_close(conn, message)
            elif verb == "AUTH":
                await self._on_auth(conn, message)
            else:
                await conn.try_send(
                    ["NOTICE", f"unsupported: unknown message type {verb!r}"])
        except Exception as exc:  # noqa: BLE001 - never take the relay down
            log.exception("dev relay: failed handling %r", verb)
            await conn.try_send(["NOTICE", f"error: {exc}"])

    # ------------------------------------------------------------- verbs

    async def _on_event(self, conn: _Connection, message: list[Any]) -> None:
        if len(message) < 2 or not isinstance(message[1], dict):
            await conn.try_send(["NOTICE", "invalid: EVENT needs an event object"])
            return
        raw = message[1]
        try:
            event = Event.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            event_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
            await conn.try_send(
                ["OK", event_id, False, f"invalid: malformed event ({exc})"])
            return

        if self.require_auth and not conn.authenticated:
            await conn.try_send(["OK", event.id, False,
                                 "auth-required: authenticate with NIP-42 first"])
            return

        reason = self._reject_reason(event)
        if reason is not None:
            await conn.try_send(["OK", event.id, False, reason])
            return

        if event.id in self._ids:
            await conn.try_send(["OK", event.id, True, "duplicate: already have it"])
            return

        self._store(event)
        await conn.try_send(["OK", event.id, True, ""])
        await self._broadcast(event)

    def _reject_reason(self, event: Event) -> str | None:
        """Why this event is unacceptable, or ``None`` if it is fine."""
        if event.id != event.compute_id():
            return "invalid: event id does not match its serialized content"
        if not event.verify():
            return "invalid: signature verification failed"
        # NIP-29: a group chat message is meaningless without its group.
        if event.kind == KIND_CHAT and not event.channel:
            return _MISSING_H
        return None

    def _store(self, event: Event) -> None:
        self._events.append(event)
        self._ids.add(event.id)
        self._order[event.id] = self._seq
        self._seq += 1

    async def _broadcast(self, event: Event) -> None:
        for conn in list(self._connections):
            for sub_id, filters in list(conn.subscriptions.items()):
                if _matches_any(event, filters):
                    await conn.try_send(["EVENT", sub_id, event.to_dict()])

    async def _on_req(self, conn: _Connection, message: list[Any]) -> None:
        if len(message) < 2 or not isinstance(message[1], str):
            await conn.try_send(["NOTICE", "invalid: REQ needs a subscription id"])
            return
        sub_id = message[1]
        if self.require_auth and not conn.authenticated:
            await conn.try_send(["CLOSED", sub_id,
                                 "auth-required: authenticate with NIP-42 first"])
            return
        filters = [f for f in message[2:] if isinstance(f, dict)]
        # A REQ with no filter at all subscribes to everything, which is the
        # same thing an empty filter object {} means.
        if not filters:
            filters = [{}]
        conn.subscriptions[sub_id] = filters

        for event in self._stored_matching(filters):
            await conn.try_send(["EVENT", sub_id, event.to_dict()])
        await conn.try_send(["EOSE", sub_id])

    def _newest_first(self, events: list[Event]) -> list[Event]:
        """Newest first, ties broken by arrival order (latest arrival first)."""
        return sorted(events, key=lambda e: (e.created_at, self._order[e.id]),
                      reverse=True)

    def _stored_matching(self, filters: list[dict[str, Any]]) -> list[Event]:
        """Stored matches, newest first. ``limit`` applies per filter (NIP-01)."""
        picked: dict[str, Event] = {}
        for flt in filters:
            hits = self._newest_first([e for e in self._events if _matches(e, flt)])
            limit = flt.get("limit")
            if isinstance(limit, int) and limit >= 0:
                hits = hits[:limit]
            for event in hits:
                picked[event.id] = event
        return self._newest_first(list(picked.values()))

    async def _on_close(self, conn: _Connection, message: list[Any]) -> None:
        if len(message) < 2 or not isinstance(message[1], str):
            await conn.try_send(["NOTICE", "invalid: CLOSE needs a subscription id"])
            return
        conn.subscriptions.pop(message[1], None)

    async def _on_auth(self, conn: _Connection, message: list[Any]) -> None:
        if len(message) < 2 or not isinstance(message[1], dict):
            await conn.try_send(["NOTICE", "invalid: AUTH needs a signed event"])
            return
        try:
            event = Event.from_dict(message[1])
        except (KeyError, TypeError, ValueError) as exc:
            await conn.try_send(["NOTICE", f"invalid: malformed AUTH event ({exc})"])
            return

        reason = self._auth_reason(conn, event)
        if reason is not None:
            await conn.try_send(["OK", event.id, False, reason])
            return
        conn.pubkey = event.pubkey
        await conn.try_send(["OK", event.id, True, ""])

    def _auth_reason(self, conn: _Connection, event: Event) -> str | None:
        if event.kind != KIND_AUTH:
            return f"invalid: AUTH event must be kind {KIND_AUTH}"
        if event.id != event.compute_id() or not event.verify():
            return "invalid: AUTH signature verification failed"
        if event.tag_value("challenge") != conn.challenge:
            return "invalid: challenge does not match the one this relay issued"
        return None


__all__ = ["DevRelay"]
