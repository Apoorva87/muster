"""The status machine for an externally visible side effect (decision D4).

``ctx.run_typed()`` (Restate) ALREADY gives durable, replay-safe steps. What it
does NOT give is reconciliation of ``UNKNOWN`` outcomes against external APIs
that lack idempotency keys — the genuinely hard part. So ``Effect`` owns ONLY
the status machine, the idempotency key, and reconcile-before-retry. It sits ON
TOP of ``run_typed``, never wrapping or replacing it.

The rule this module encodes, verbatim from the V2 PRD:

    if an external API supports idempotency keys, use the Effect ID. If the
    outcome is unknown and the external API does not provide idempotency,
    reconcile before retrying. Never equate workflow replay protection with
    universal exactly-once side effects.

The last sentence is the whole point. A replayed workflow does not re-run a
journalled step, but that says nothing about whether the payment API on the
other side of a timed-out socket took the money. Only reconciliation answers
that, and until it does, an ``UNKNOWN`` non-idempotent effect is not retryable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from app.kernel.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EffectStatus(str, Enum):
    """The V2 PRD's status set."""

    PENDING = "PENDING"          # created, nothing left the process
    SENT = "SENT"                # dispatched, outcome not yet known
    CONFIRMED = "CONFIRMED"      # the external system applied it
    UNKNOWN = "UNKNOWN"          # ambiguous — it may or may not have applied
    FAILED = "FAILED"            # definitively not applied
    COMPENSATED = "COMPENSATED"  # applied, then deliberately undone


#: Which status may follow which. Everything absent here is illegal.
#:
#: * ``PENDING -> FAILED`` covers rejection before dispatch (policy, approval
#:   denial): nothing left the process, so nothing external happened.
#: * ``PENDING -> CONFIRMED`` is absent — never claim an outcome for an
#:   operation that was never attempted.
#: * ``SENT -> SENT`` is absent — the blind retry this module exists to prevent.
#: * ``UNKNOWN -> SENT`` is absent — reconcile-before-retry, the core of D4. An
#:   idempotent retry does not re-enter SENT; the effect *stays* UNKNOWN until
#:   the retry returns a real outcome.
#: * ``CONFIRMED`` is terminal except for ``COMPENSATED`` — a confirmed external
#:   effect is undone by a compensating action, never by relabelling it.
#: * ``FAILED -> SENT`` is legal: a *definite* failure means nothing was applied
#:   externally, so re-attempting is safe. This is the contrast case to UNKNOWN,
#:   and it is why reconciling ``UNKNOWN -> FAILED`` is what unblocks a retry.
#: * ``FAILED -> COMPENSATED`` is absent — there is nothing to compensate.
ALLOWED_TRANSITIONS: dict[EffectStatus, frozenset[EffectStatus]] = {
    EffectStatus.PENDING: frozenset({EffectStatus.SENT, EffectStatus.FAILED}),
    EffectStatus.SENT: frozenset({EffectStatus.CONFIRMED, EffectStatus.UNKNOWN,
                                  EffectStatus.FAILED}),
    EffectStatus.UNKNOWN: frozenset({EffectStatus.CONFIRMED, EffectStatus.FAILED}),
    EffectStatus.CONFIRMED: frozenset({EffectStatus.COMPENSATED}),
    EffectStatus.FAILED: frozenset({EffectStatus.SENT}),
    EffectStatus.COMPENSATED: frozenset(),
}

#: Statuses that still owe somebody an answer.
UNRESOLVED_STATUSES: frozenset[EffectStatus] = frozenset({
    EffectStatus.PENDING, EffectStatus.SENT, EffectStatus.UNKNOWN,
})


class IllegalTransition(ValueError):
    """Raised when a status change is not in ``ALLOWED_TRANSITIONS``."""

    def __init__(self, effect_id: str, source: EffectStatus,
                 target: EffectStatus) -> None:
        self.effect_id = effect_id
        self.source = source
        self.target = target
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[source])
        super().__init__(
            f"effect {effect_id}: {source.value} -> {target.value} is illegal; "
            f"{source.value} may only become {allowed or ['<terminal>']}")


class Reconciliation(BaseModel):
    """A reconciler's verdict on what the external system actually did.

    ``applied`` is a claim about the *external world*, not about our process.
    A reconciler that cannot tell must raise rather than guess — an inconclusive
    reconciliation leaves the effect UNKNOWN, which is the correct answer.
    """

    applied: bool
    result_ref: str | None = None
    error: str | None = None


class Effect(BaseModel):
    """One externally visible side effect and its status machine.

    ``idempotency_key`` is set to the Effect ID exactly when the external API
    accepts one; that is what makes a duplicate request safe, so the field is
    also the authoritative answer to "is this operation idempotent?".

    ``request_ref``/``result_ref`` are references, never payloads — large bodies
    belong in the artifact store (CLAUDE.md: artifacts passed by reference).
    """

    id: str = Field(default_factory=lambda: new_id("eff"))
    task_id: str
    operation: str
    idempotency_key: str | None = None
    status: EffectStatus = EffectStatus.PENDING
    request_ref: str | None = None
    result_ref: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    attempts: int = 0
    error: str | None = None

    # ------------------------------------------------------------ properties

    @property
    def is_idempotent(self) -> bool:
        """True when the external API will collapse a duplicate for us."""
        return self.idempotency_key is not None

    @property
    def is_terminal(self) -> bool:
        return not ALLOWED_TRANSITIONS[self.status]

    @property
    def is_resolved(self) -> bool:
        return self.status not in UNRESOLVED_STATUSES

    @property
    def needs_reconciliation(self) -> bool:
        """An ambiguous outcome we cannot safely re-fire our way out of."""
        return self.status is EffectStatus.UNKNOWN and not self.is_idempotent

    @property
    def may_retry(self) -> bool:
        """May the external operation be invoked (again) right now?

        The interesting case is UNKNOWN: safe only when the Effect ID is the
        external idempotency key, because then a duplicate request is collapsed
        on the far side. Without that, the answer is no until a reconciler
        establishes what actually happened.
        """
        if self.status in (EffectStatus.PENDING, EffectStatus.FAILED):
            return True
        if self.status is EffectStatus.UNKNOWN:
            return self.is_idempotent
        # SENT is still in flight; CONFIRMED and COMPENSATED are done.
        return False

    # ------------------------------------------------------------ transitions

    def can_transition_to(self, status: EffectStatus) -> bool:
        return status in ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, status: EffectStatus) -> "Effect":
        """Move to ``status`` or raise ``IllegalTransition``. Returns self."""
        if not self.can_transition_to(status):
            raise IllegalTransition(self.id, self.status, status)
        self.status = status
        self.updated_at = _utcnow()
        return self
