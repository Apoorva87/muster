"""Stable, prefixed identifiers.

Every task, event, artifact and run carries a stable ID so Restate can key
durable invocations and so replay is idempotent (V1 PRD, "Idempotency and
side effects").
"""

from __future__ import annotations

import uuid

_HEX_LEN = 16


def new_id(prefix: str) -> str:
    """Return a fresh ID of the form ``<prefix>_<16 hex chars>``."""
    if not prefix or "_" in prefix:
        raise ValueError(f"prefix must be non-empty and contain no underscore: {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex[:_HEX_LEN]}"


def deterministic_id(prefix: str, *parts: str) -> str:
    """Return an ID derived from ``parts``.

    Used where the same logical operation must produce the same ID across a
    replay — for example a fan-out send, which must not create a new task ID
    each time Restate replays the publishing handler.
    """
    seed = "\x1f".join(parts)
    digest = uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:_HEX_LEN]
    return f"{prefix}_{digest}"
