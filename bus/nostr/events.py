"""NIP-01 events: identity, serialization, signing, verification.

Buzz is a Nostr relay. An agent's identity is a secp256k1 keypair, not a bot
token, and every message it posts is a signed event stored in the relay's
Postgres. This module is the wire format — no transport, no Muster concepts.

Kinds we care about (from Buzz's own docs):

======  ======================================================
kind    meaning
======  ======================================================
0       profile metadata (NIP-01) — how an agent gets a name
9       group chat message (NIP-29) — REQUIRES an ``h`` tag
7       reaction (NIP-25)
22242   client authentication (NIP-42)
======  ======================================================

A kind:9 without a channel-scoped ``h`` tag is rejected by the relay.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from coincurve import PrivateKey, PublicKeyXOnly

KIND_PROFILE = 0
KIND_REACTION = 7
KIND_CHAT = 9
KIND_AUTH = 22242


class SignatureError(ValueError):
    """An event's id or signature does not check out."""


@dataclass(frozen=True)
class Identity:
    """An agent's cryptographic identity. The public key *is* the account."""

    private_key: PrivateKey

    @classmethod
    def generate(cls) -> "Identity":
        return cls(PrivateKey())

    @classmethod
    def from_hex(cls, secret_hex: str) -> "Identity":
        return cls(PrivateKey(bytes.fromhex(secret_hex.removeprefix("0x"))))

    @classmethod
    def derive(cls, seed: str) -> "Identity":
        """A deterministic identity from a label.

        Lets a demo (or a dev deployment) give every agent a stable pubkey
        without a key-management story. Never use this for anything real —
        the seed is the key.
        """
        return cls(PrivateKey(hashlib.sha256(seed.encode()).digest()))

    @property
    def secret_hex(self) -> str:
        return self.private_key.secret.hex()

    @property
    def pubkey(self) -> str:
        """32-byte x-only public key, hex. This is the Nostr identity."""
        return PublicKeyXOnly.from_secret(self.private_key.secret).format().hex()

    def sign(self, digest: bytes) -> str:
        return self.private_key.sign_schnorr(digest).hex()


@dataclass
class Event:
    """A NIP-01 event."""

    kind: int
    content: str
    pubkey: str
    created_at: int = field(default_factory=lambda: int(time.time()))
    tags: list[list[str]] = field(default_factory=list)
    id: str = ""
    sig: str = ""

    # ------------------------------------------------------------ identity

    def serialize(self) -> str:
        """The exact NIP-01 preimage. Whitespace and escaping are load-bearing."""
        return json.dumps(
            [0, self.pubkey, self.created_at, self.kind, self.tags, self.content],
            separators=(",", ":"), ensure_ascii=False)

    def compute_id(self) -> str:
        return hashlib.sha256(self.serialize().encode("utf-8")).hexdigest()

    def finalize(self, identity: Identity) -> "Event":
        """Stamp the id and signature. The event is immutable afterwards."""
        self.pubkey = identity.pubkey
        self.id = self.compute_id()
        self.sig = identity.sign(bytes.fromhex(self.id))
        return self

    def verify(self) -> bool:
        if self.id != self.compute_id():
            return False
        try:
            return PublicKeyXOnly(bytes.fromhex(self.pubkey)).verify(
                bytes.fromhex(self.sig), bytes.fromhex(self.id))
        except (ValueError, TypeError):
            return False

    def require_valid(self) -> "Event":
        if not self.verify():
            raise SignatureError(f"event {self.id[:12]}… failed verification")
        return self

    # ---------------------------------------------------------------- tags

    def tag_value(self, name: str) -> str | None:
        for tag in self.tags:
            if len(tag) >= 2 and tag[0] == name:
                return tag[1]
        return None

    def tag_values(self, name: str) -> list[str]:
        return [t[1] for t in self.tags if len(t) >= 2 and t[0] == name]

    @property
    def channel(self) -> str | None:
        """The NIP-29 group this belongs to."""
        return self.tag_value("h")

    # --------------------------------------------------------------- codec

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pubkey": self.pubkey, "created_at": self.created_at,
                "kind": self.kind, "tags": self.tags, "content": self.content,
                "sig": self.sig}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        return cls(kind=int(raw["kind"]), content=raw.get("content", ""),
                   pubkey=raw.get("pubkey", ""),
                   created_at=int(raw.get("created_at", 0)),
                   tags=[list(t) for t in raw.get("tags", [])],
                   id=raw.get("id", ""), sig=raw.get("sig", ""))


# ----------------------------------------------------------------- builders


def chat_message(identity: Identity, channel: str, content: str, *,
                 reply_to: str | None = None,
                 extra_tags: list[list[str]] | None = None) -> Event:
    """A NIP-29 group chat message. The ``h`` tag is mandatory."""
    tags: list[list[str]] = [["h", channel]]
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    tags.extend(extra_tags or [])
    return Event(kind=KIND_CHAT, content=content, pubkey=identity.pubkey,
                 tags=tags).finalize(identity)


def profile(identity: Identity, *, name: str, about: str = "",
            picture: str = "") -> Event:
    """kind:0 metadata — what gives an agent a display name in the UI."""
    body = {"name": name, "display_name": name, "about": about}
    if picture:
        body["picture"] = picture
    return Event(kind=KIND_PROFILE, content=json.dumps(body, separators=(",", ":")),
                 pubkey=identity.pubkey).finalize(identity)


def auth_response(identity: Identity, *, challenge: str, relay: str) -> Event:
    """NIP-42 response to the relay's AUTH challenge."""
    return Event(kind=KIND_AUTH, content="", pubkey=identity.pubkey,
                 tags=[["relay", relay], ["challenge", challenge]]).finalize(identity)


def reaction(identity: Identity, target: Event, symbol: str = "+") -> Event:
    tags = [["e", target.id], ["p", target.pubkey]]
    if target.channel:
        tags.append(["h", target.channel])
    return Event(kind=KIND_REACTION, content=symbol, pubkey=identity.pubkey,
                 tags=tags).finalize(identity)
