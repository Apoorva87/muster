"""Command routing — one message, exactly one logical destination.

Two jobs, both deterministic:

1. **Qualify and validate.** A bare address (``finance``) means "inside my own
   team", so it is qualified with the caller's ``source_team`` before being
   checked against the registry. An unqualified-but-unknown destination is a
   caller error, not a delivery retry, so it surfaces as ``DeliveryError`` with
   the roll of what *does* exist.
2. **Suppress duplicates.** A redelivered ``message_id`` must not cause a second
   unit of logical work (V2 acceptance criterion 8). This is a cheap in-session
   guard in front of Restate's own idempotency — not a replacement for it.
   Restate remains the durability authority; see ``bus/adapters/restate.py``.

No LLM is involved in routing. Deterministic code routes; agents reason.
"""

from __future__ import annotations

from collections import OrderedDict

from bus.adapters.base import DeliveryError
from bus.models.address import Address
from bus.models.message import Message
from bus.routing.registry import TeamRegistry, UnknownAgent, UnknownTeam

#: How many recently seen message IDs to remember. Bounded so a long-lived bus
#: session cannot grow this without limit; Restate is the durable authority for
#: anything older than the window.
DEFAULT_SEEN_CAPACITY = 10_000


class CommandRouter:
    """Resolves a command's destination against the team registry."""

    def __init__(self, registry: TeamRegistry,
                 seen_capacity: int = DEFAULT_SEEN_CAPACITY) -> None:
        if seen_capacity < 1:
            raise ValueError("seen_capacity must be at least 1")
        self.registry = registry
        self.seen_capacity = seen_capacity
        # Ordered so the oldest ID is the one evicted; the value is unused.
        self._seen: OrderedDict[str, None] = OrderedDict()

    async def route(self, source_team: str, destination: str | Address,
                    message: Message) -> Address:
        """Qualify ``destination`` with ``source_team`` and validate it.

        Returns the fully qualified :class:`Address`. Raises ``DeliveryError``
        when the team or the agent is not on the muster roll.
        """
        address = self._as_address(destination)
        if address.is_local:
            if not source_team:
                raise DeliveryError(
                    f"cannot route {address} for message {message.id}: it names "
                    "no team and the caller supplied no source_team to qualify "
                    "it with")
            address = address.qualified(source_team)

        try:
            team_id, agent = self.registry.resolve(address)
        except UnknownTeam as exc:
            raise DeliveryError(
                f"cannot route message {message.id} from "
                f"{source_team!r} to {address}: {exc.args[0]}") from exc
        except UnknownAgent as exc:
            raise DeliveryError(
                f"cannot route message {message.id} from "
                f"{source_team!r} to {address}: {exc.args[0]}") from exc

        return Address(agent=agent, team=team_id)

    def seen(self, message_id: str) -> bool:
        """Record ``message_id`` and report whether it had been seen before.

        ``True`` means "this is a redelivery, drop it". The check and the record
        are one operation on purpose: a caller cannot forget to remember.
        """
        if message_id in self._seen:
            self._seen.move_to_end(message_id)
            return True
        self._seen[message_id] = None
        while len(self._seen) > self.seen_capacity:
            self._seen.popitem(last=False)
        return False

    def forget(self, message_id: str) -> None:
        """Drop one ID from the window. Test/administration affordance."""
        self._seen.pop(message_id, None)

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    @staticmethod
    def _as_address(destination: str | Address) -> Address:
        if isinstance(destination, Address):
            return destination
        try:
            return Address.parse(destination)
        except ValueError as exc:
            raise DeliveryError(f"bad destination {destination!r}: {exc}") from exc
