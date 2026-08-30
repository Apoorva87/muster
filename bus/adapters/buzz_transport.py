"""Bind the Buzz control plane to a real Nostr relay.

``BuzzControlPlane`` only needs ``post`` and ``listen``; this supplies them from
a live ``NostrClient``. Keeping the binding separate is what let the control
plane be fully tested with no socket — and it means pointing at Block's Buzz
relay instead of the local dev relay is a URL change, nothing more.
"""

from __future__ import annotations

from typing import AsyncIterator

from bus.nostr.client import NostrClient, make_filter
from bus.nostr.events import KIND_CHAT, Event, Identity


class NostrChatTransport:
    """A ``ChatTransport`` over a connected ``NostrClient``."""

    def __init__(self, client: NostrClient) -> None:
        self._client = client

    async def post(self, event: Event) -> bool:
        return await self._client.publish(event)

    async def listen(self, channel: str, *,
                     since: int | None = None) -> AsyncIterator[Event]:
        """Live chat in one NIP-29 group.

        ``since`` skips the stored backlog, which is what you want for a
        command listener — replaying yesterday's "run ..." on restart would
        launch yesterday's work again.
        """
        async for event in self._client.stream(
                make_filter(kinds=[KIND_CHAT], channels=[channel], since=since)):
            yield event


async def connect(url: str, identity: Identity, **kwargs) -> tuple[NostrClient,
                                                                   NostrChatTransport]:
    """Open a relay connection and wrap it. Caller owns closing the client."""
    client = await NostrClient(url, identity, **kwargs).connect()
    return client, NostrChatTransport(client)
