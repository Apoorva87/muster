"""NIP-01 relay client: the websocket half of talking to Buzz.

:mod:`bus.nostr.events` owns the wire *format* — keys, ids, signatures. This
module owns the wire *protocol* — the five verbs a Nostr client speaks and the
six a relay answers with. It knows nothing about Muster: no kernel, no agents,
no topics. Anything domain-shaped belongs in an adapter above this one.

Frames we send::

    ["EVENT", <event>]
    ["REQ", "<sub_id>", <filter>, ...]
    ["CLOSE", "<sub_id>"]
    ["AUTH", <signed kind-22242 event>]

Frames we accept::

    ["EVENT", "<sub_id>", <event>]
    ["OK", "<event_id>", true|false, "<message>"]
    ["EOSE", "<sub_id>"]
    ["AUTH", "<challenge>"]
    ["NOTICE", "<message>"]
    ["CLOSED", "<sub_id>", "<message>"]

Design notes
------------
*Auth is transparent.* Buzz sends ``["AUTH", challenge]`` proactively on
connect, and may send it again at any point. The reader answers every challenge
the moment it arrives, so no caller ever has to think about NIP-42.
:attr:`NostrClient.authenticated` is there for tests and diagnostics, not as a
step callers must perform.

*Nothing unverified escapes.* Every inbound event is re-hashed and its Schnorr
signature checked before it reaches a subscriber. A relay that lies gets its
events dropped with a warning; the iterator simply never sees them.

*Reading is one task.* A single background reader owns the socket, so the
protocol is dispatched in one place: OK resolves a publish future, EVENT/EOSE
feed a subscription queue, AUTH answers itself. Callers only ever touch queues
and futures, never ``recv()``.

Subscriptions
-------------
Two shapes, same machinery::

    sub_id = await client.subscribe(make_filter(kinds=[9], channels=["ops"]))
    async for event in client.events(sub_id):
        ...

    async for event in client.stream(make_filter(kinds=[9])):   # sugar
        ...

``events()``/``stream()`` yield only :class:`~bus.nostr.events.Event` by
default. Pass ``include_eose=True`` to also get the :data:`EOSE` sentinel once,
at the boundary between stored backlog and live traffic; :meth:`wait_eose` is
the same signal for callers that would rather await it than match on it.

The websocket dependency is optional (``uv sync --extra buzz``), guarded the
same way ``app/runtime/durable.py`` guards the Restate SDK: importing this
module always works, and the error only fires if you actually connect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from bus.nostr.events import Event, Identity, auth_response

try:  # pragma: no cover - exercised by whichever extra is installed
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: A NIP-01 filter. Kept as a plain dict so any filter the relay grows support
#: for is expressible without a change here; :func:`make_filter` is convenience.
Filter = dict[str, Any]

_WEBSOCKETS_MISSING = (
    "The 'websockets' package is not installed. Muster keeps the Buzz relay "
    "client an optional extra so the unit suite needs no network.\n"
    "Install it with:  uv sync --extra buzz\n"
    "(equivalently: uv add websockets coincurve)"
)


class RelayError(RuntimeError):
    """The relay refused, dropped or never answered a request."""


class RelayRejected(RelayError):
    """The relay answered ``["OK", id, false, message]``."""

    def __init__(self, event_id: str, message: str) -> None:
        super().__init__(f"relay rejected event {event_id[:12]}…: {message}")
        self.event_id = event_id
        self.message = message


class RelayTimeout(RelayError, TimeoutError):
    """The relay did not answer in time. Also a ``TimeoutError``."""


class _Marker:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self._name


#: Yielded once by ``events(..., include_eose=True)`` when the relay has
#: finished replaying stored events. Everything after it is live.
EOSE = _Marker("EOSE")
_CLOSED = _Marker("CLOSED")


def make_filter(*, kinds: Sequence[int] | None = None,
                authors: Sequence[str] | None = None,
                channels: Sequence[str] | None = None,
                ids: Sequence[str] | None = None,
                since: int | None = None, until: int | None = None,
                limit: int | None = None,
                tags: dict[str, Sequence[str]] | None = None) -> Filter:
    """Build a NIP-01 filter dict.

    ``channels`` is the NIP-29 group filter and serializes to the tag key
    ``"#h"`` — the spelling matters, and getting it wrong silently matches
    nothing. ``tags={"e": [...]}`` covers every other single-letter tag; keys
    are prefixed with ``#`` if they are not already.
    """
    out: Filter = {}
    if kinds is not None:
        out["kinds"] = [int(k) for k in kinds]
    if authors is not None:
        out["authors"] = list(authors)
    if ids is not None:
        out["ids"] = list(ids)
    if channels is not None:
        out["#h"] = list(channels)
    for name, values in (tags or {}).items():
        out[name if name.startswith("#") else f"#{name}"] = list(values)
    if since is not None:
        out["since"] = int(since)
    if until is not None:
        out["until"] = int(until)
    if limit is not None:
        out["limit"] = int(limit)
    return out


@dataclass
class _Subscription:
    sub_id: str
    filters: tuple[Filter, ...]
    queue: "asyncio.Queue[Any]" = field(default_factory=asyncio.Queue)
    eose: asyncio.Event = field(default_factory=asyncio.Event)
    close_reason: str = ""


class NostrClient:
    """A relay connection that speaks NIP-01, NIP-42 and NIP-29 group filters.

    ``connector`` exists so tests can inject an in-process transport: any async
    callable ``url -> object`` where the object has ``send(str)``, ``close()``
    and is an async iterator of inbound frames. The default one opens a real
    websocket.
    """

    def __init__(self, url: str, identity: Identity, *,
                 relay_name: str | None = None,
                 connector: Callable[[str], Awaitable[Any]] | None = None,
                 timeout: float = 10.0,
                 max_retries: int = 3,
                 retry_delay: float = 0.5) -> None:
        self.url = url
        self.identity = identity
        #: What goes in the auth event's ``relay`` tag; the relay compares it.
        self.relay_name = relay_name or url
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._connector = connector or self._default_connector
        self._ws: Any | None = None
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._subs: dict[str, _Subscription] = {}
        self._pending_ok: dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._authenticated = False
        self._auth_event_id: str | None = None
        self._auth_done = asyncio.Event()
        self._challenge: str | None = None

    # ------------------------------------------------------------ lifecycle

    @staticmethod
    async def _default_connector(url: str) -> Any:
        if websockets is None:
            raise RuntimeError(_WEBSOCKETS_MISSING)
        return await websockets.connect(url)

    async def connect(self) -> "NostrClient":
        """Open the socket and start the reader. Idempotent."""
        if self._ws is not None:
            return self
        self._closing = False
        self._ws = await self._connector(self.url)
        self._reader = asyncio.create_task(self._read_loop(), name="nostr-reader")
        return self

    async def close(self) -> None:
        """Close the socket, stop the reader, wake every waiter."""
        self._closing = True
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):  # noqa: B014 - reader is dead either way
                pass
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("closing %s failed: %s", self.url, exc)
        self._fail_pending("the client closed the connection")
        self._shutdown_subs()

    async def __aenter__(self) -> "NostrClient":
        return await self.connect()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def authenticated(self) -> bool:
        """True once the relay accepted our NIP-42 auth event on this socket."""
        return self._authenticated

    @property
    def challenge(self) -> str | None:
        """The last challenge the relay sent, if any."""
        return self._challenge

    async def wait_authenticated(self, timeout: float | None = None) -> bool:
        """Await the relay's verdict on our auth event. Never raises on timeout."""
        try:
            await asyncio.wait_for(self._auth_done.wait(),
                                   self.timeout if timeout is None else timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return self._authenticated

    # ------------------------------------------------------------- outbound

    async def _send(self, message: list[Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RelayError(f"not connected to {self.url}")
        await ws.send(json.dumps(message, separators=(",", ":")))

    async def publish(self, event: Event, *, timeout: float | None = None) -> bool:
        """Send an event and wait for the relay's ``OK``.

        Returns ``True`` when accepted. A rejection raises :class:`RelayRejected`
        carrying the relay's own message — silently returning ``False`` loses the
        reason, which is the only useful part. A silent relay raises
        :class:`RelayTimeout` rather than hanging forever.

        If the relay rejects with ``auth-required:`` we wait for the in-flight
        NIP-42 handshake and retry exactly once; auth stays invisible to callers.
        """
        timeout = self.timeout if timeout is None else timeout
        try:
            return await self._publish_once(event, timeout)
        except RelayRejected as exc:
            if not self._is_auth_required(exc.message) or self._authenticated:
                raise
            logger.info("relay wants auth before accepting %s; retrying",
                        event.id[:12])
            if not await self.wait_authenticated(timeout):
                raise
            return await self._publish_once(event, timeout)

    @staticmethod
    def _is_auth_required(message: str) -> bool:
        head = message.strip().lower()
        return head.startswith("auth-required") or head.startswith("restricted")

    async def _publish_once(self, event: Event, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._pending_ok[event.id] = future
        try:
            await self._send(["EVENT", event.to_dict()])
            accepted, message = await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise RelayTimeout(
                f"relay {self.url} did not acknowledge event "
                f"{event.id[:12]}… within {timeout}s") from None
        finally:
            self._pending_ok.pop(event.id, None)
        if not accepted:
            raise RelayRejected(event.id, message)
        return True

    async def subscribe(self, *filters: Filter, sub_id: str | None = None) -> str:
        """Open a subscription and return its id.

        The filters are remembered so a reconnect can re-establish them.
        """
        sub_id = sub_id or f"sub-{secrets.token_hex(6)}"
        sub = _Subscription(sub_id=sub_id, filters=tuple(filters or ({},)))
        self._subs[sub_id] = sub
        await self._send(["REQ", sub_id, *sub.filters])
        return sub_id

    async def unsubscribe(self, sub_id: str) -> None:
        """Tell the relay to stop, and terminate any active iterator."""
        sub = self._subs.pop(sub_id, None)
        if sub is not None:
            sub.queue.put_nowait(_CLOSED)
        if self._ws is not None:
            try:
                await self._send(["CLOSE", sub_id])
            except Exception as exc:  # pragma: no cover - socket already gone
                logger.debug("CLOSE %s failed: %s", sub_id, exc)

    # -------------------------------------------------------------- inbound

    async def events(self, sub_id: str, *,
                     include_eose: bool = False) -> AsyncIterator[Event]:
        """Yield verified events for ``sub_id`` until it is closed.

        With ``include_eose=True`` the :data:`EOSE` sentinel is yielded once,
        marking the end of the stored backlog.
        """
        sub = self._subs.get(sub_id)
        if sub is None:
            raise KeyError(f"no such subscription: {sub_id}")
        while True:
            item = await sub.queue.get()
            if item is _CLOSED:
                return
            if item is EOSE:
                if include_eose:
                    yield item  # type: ignore[misc]
                continue
            yield item

    async def stream(self, *filters: Filter, sub_id: str | None = None,
                     include_eose: bool = False) -> AsyncIterator[Event]:
        """``subscribe`` + ``events`` in one, closing the subscription on exit."""
        sub_id = await self.subscribe(*filters, sub_id=sub_id)
        try:
            async for item in self.events(sub_id, include_eose=include_eose):
                yield item
        finally:
            await self.unsubscribe(sub_id)

    async def wait_eose(self, sub_id: str, timeout: float | None = None) -> bool:
        """Await the end of the stored backlog for ``sub_id``."""
        sub = self._subs.get(sub_id)
        if sub is None:
            raise KeyError(f"no such subscription: {sub_id}")
        try:
            await asyncio.wait_for(sub.eose.wait(),
                                   self.timeout if timeout is None else timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def fetch(self, *filters: Filter,
                    timeout: float | None = None) -> list[Event]:
        """One-shot query: subscribe, collect the backlog, close at ``EOSE``."""
        timeout = self.timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        sub_id = await self.subscribe(*filters)
        sub = self._subs[sub_id]
        collected: list[Event] = []
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RelayTimeout(
                        f"relay {self.url} sent no EOSE for {sub_id} "
                        f"within {timeout}s")
                try:
                    item = await asyncio.wait_for(sub.queue.get(), remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    raise RelayTimeout(
                        f"relay {self.url} sent no EOSE for {sub_id} "
                        f"within {timeout}s") from None
                if item is EOSE:
                    return collected
                if item is _CLOSED:
                    if sub.close_reason:
                        raise RelayError(
                            f"relay closed {sub_id}: {sub.close_reason}")
                    return collected
                collected.append(item)
        finally:
            await self.unsubscribe(sub_id)

    # ------------------------------------------------------- reader / codec

    async def _read_loop(self) -> None:
        while True:
            ws = self._ws
            try:
                async for raw in ws:  # type: ignore[union-attr]
                    await self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("read from %s failed: %s", self.url, exc)
            if self._closing:
                break
            logger.warning("connection to %s dropped", self.url)
            self._fail_pending("the relay connection dropped")
            if not await self._reconnect():
                break
        self._shutdown_subs()

    async def _dispatch(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("relay %s sent non-JSON frame: %.80r", self.url, raw)
            return
        if not isinstance(message, list) or not message:
            logger.warning("relay %s sent a malformed frame: %.80r", self.url, raw)
            return
        verb = message[0]
        try:
            if verb == "EVENT":
                self._on_event(message[1], message[2])
            elif verb == "OK":
                self._on_ok(message[1], bool(message[2]),
                            message[3] if len(message) > 3 else "")
            elif verb == "EOSE":
                self._on_eose(message[1])
            elif verb == "AUTH":
                await self._on_auth(message[1])
            elif verb == "CLOSED":
                self._on_closed(message[1],
                                message[2] if len(message) > 2 else "")
            elif verb == "NOTICE":
                logger.info("relay %s notice: %s", self.url,
                            message[1] if len(message) > 1 else "")
            else:
                logger.debug("ignoring unknown verb %r from %s", verb, self.url)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            logger.warning("relay %s sent a bad %s frame (%s): %.120r",
                           self.url, verb, exc, raw)

    def _on_event(self, sub_id: str, payload: dict[str, Any]) -> None:
        event = Event.from_dict(payload)
        if not event.verify():
            logger.warning("dropping unverified event %s from %s on %s",
                           (event.id or "?")[:12], self.url, sub_id)
            return
        sub = self._subs.get(sub_id)
        if sub is None:
            logger.debug("event for unknown subscription %s", sub_id)
            return
        sub.queue.put_nowait(event)

    def _on_ok(self, event_id: str, accepted: bool, message: str) -> None:
        if event_id and event_id == self._auth_event_id:
            self._authenticated = accepted
            self._auth_done.set()
            if accepted:
                logger.debug("authenticated to %s as %s", self.url,
                             self.identity.pubkey[:12])
            else:
                logger.warning("relay %s refused our auth event: %s",
                               self.url, message)
            return
        future = self._pending_ok.get(event_id)
        if future is None or future.done():
            logger.debug("unmatched OK for %s", event_id[:12])
            return
        future.set_result((accepted, message))

    def _on_eose(self, sub_id: str) -> None:
        sub = self._subs.get(sub_id)
        if sub is None:
            return
        sub.eose.set()
        sub.queue.put_nowait(EOSE)

    def _on_closed(self, sub_id: str, message: str) -> None:
        logger.warning("relay %s closed subscription %s: %s",
                       self.url, sub_id, message)
        sub = self._subs.pop(sub_id, None)
        if sub is not None:
            sub.close_reason = message
            sub.eose.set()
            sub.queue.put_nowait(_CLOSED)

    async def _on_auth(self, challenge: str) -> None:
        """NIP-42: answer the challenge immediately, without asking anyone."""
        self._challenge = challenge
        self._authenticated = False
        self._auth_done.clear()
        event = auth_response(self.identity, challenge=challenge,
                              relay=self.relay_name)
        self._auth_event_id = event.id
        await self._send(["AUTH", event.to_dict()])

    # ------------------------------------------------------------ reconnect

    async def _reconnect(self) -> bool:
        delay = self.retry_delay
        for attempt in range(1, self.max_retries + 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                self._ws = await self._connector(self.url)
            except Exception as exc:
                logger.warning("reconnect %d/%d to %s failed: %s",
                               attempt, self.max_retries, self.url, exc)
                delay *= 2
                continue
            # A new socket means a new NIP-42 challenge; the relay will send it
            # and the reader will answer it.
            self._authenticated = False
            self._auth_event_id = None
            self._auth_done.clear()
            logger.info("reconnected to %s (attempt %d)", self.url, attempt)
            await self._resubscribe()
            return True
        logger.error("giving up on %s after %d attempts", self.url,
                     self.max_retries)
        self._ws = None
        return False

    async def _resubscribe(self) -> None:
        for sub in list(self._subs.values()):
            sub.eose.clear()
            try:
                await self._send(["REQ", sub.sub_id, *sub.filters])
            except Exception as exc:  # pragma: no cover - lost again already
                logger.warning("could not restore subscription %s: %s",
                               sub.sub_id, exc)

    # ---------------------------------------------------------------- waking

    def _fail_pending(self, reason: str) -> None:
        for event_id, future in list(self._pending_ok.items()):
            if not future.done():
                future.set_exception(
                    RelayError(f"{reason} before {event_id[:12]}… was acked"))
            self._pending_ok.pop(event_id, None)

    def _shutdown_subs(self) -> None:
        for sub in list(self._subs.values()):
            sub.eose.set()
            sub.queue.put_nowait(_CLOSED)


__all__ = [
    "EOSE",
    "Filter",
    "NostrClient",
    "RelayError",
    "RelayRejected",
    "RelayTimeout",
    "make_filter",
]
