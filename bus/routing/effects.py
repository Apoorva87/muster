"""Persistence and execution for ``Effect`` — decision D4.

``ctx.run_typed()`` (Restate) ALREADY gives durable, replay-safe steps. What it
does NOT give is reconciliation of ``UNKNOWN`` outcomes against external APIs
that lack idempotency keys — the genuinely hard part. So ``Effect`` owns ONLY
the status machine, the idempotency key, and reconcile-before-retry. It sits ON
TOP of ``run_typed``, never wrapping or replacing it.

``EffectExecutor`` therefore does three small things around the durable step:

1. records the status machine transitions in a table;
2. passes the Effect ID through as the external idempotency key when — and only
   when — the operation supports one;
3. refuses to re-fire an ambiguous, non-idempotent operation until a caller
   supplied reconciler has established what the external system actually did.

Everything about *not re-executing on replay* is ``run_typed``'s job, and this
module never duplicates it. That distinction is the point of D4: workflow replay
protection is not universal exactly-once side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import DateTime, Engine, Integer, String, Text, create_engine, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.kernel.context import KernelContext
from app.kernel.ids import deterministic_id
from bus.models.effect import (Effect, EffectStatus, IllegalTransition,
                               Reconciliation, UNRESOLVED_STATUSES)

# Re-exported so callers need one import for the whole Effect surface.
__all__ = ["AmbiguousOutcome", "Effect", "EffectExecutor", "EffectRow",
           "EffectStatus", "EffectStore", "Reconciliation",
           "ReconciliationRequired"]


class AmbiguousOutcome(Exception):
    """Raised by an operation whose external outcome cannot be determined.

    A caller that cannot tell whether the far side applied the request must
    raise this (or let a timeout escape) rather than a plain error — the two
    are treated very differently: ambiguous becomes UNKNOWN and blocks retry,
    definite becomes FAILED and permits one.
    """


class ReconciliationRequired(RuntimeError):
    """Refusal to re-fire an ambiguous, non-idempotent effect.

    This is the D4 boundary made loud: replay protection got us this far, and
    it cannot get us any further. Call ``EffectExecutor.reconcile`` first.
    """

    def __init__(self, effect: Effect) -> None:
        self.effect = effect
        super().__init__(
            f"effect {effect.id} ({effect.operation}) is {effect.status.value} and "
            f"{'has no external idempotency key' if not effect.is_idempotent else 'is in flight'}; "
            "reconcile before retrying — replay protection is not exactly-once")


#: Failures where the request may already have reached the far side.
AMBIGUOUS_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, AmbiguousOutcome)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo. Everything we store is UTC, so put it back."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _as_ref(result: Any) -> str | None:
    """Coerce an operation's return value to a *reference*, never a payload."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("ref", "result_ref", "id"):
            if key in result and result[key] is not None:
                return str(result[key])
    return None


class EffectRow(Base):
    """The V2 PRD's minimum Effect model, plus attempt/audit columns.

    Portable column types only (``String``/``Text``/``Integer``/``DateTime``) so
    the same schema runs on SQLite in unit tests — no Docker (app/db/models.py).
    """

    __tablename__ = "effects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    request_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EffectStore:
    """Synchronous persistence, matching ``app/db/repository.py``.

    Deliberately sync for the same reason the Repository is: at laptop scale the
    round-trip is sub-millisecond and a sync store reads far more plainly.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> "EffectStore":
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            # An in-memory SQLite DB lives inside one connection; StaticPool
            # shares it rather than handing each thread an empty database.
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        return cls(create_engine(url, future=True, **kwargs))

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def save(self, effect: Effect) -> Effect:
        with self._session.begin() as s:
            data = effect.model_dump()
            data["status"] = effect.status.value
            s.merge(EffectRow(**data))
        return effect

    def get(self, effect_id: str) -> Effect | None:
        with self._session() as s:
            row = s.get(EffectRow, effect_id)
            return self._to_effect(row) if row else None

    def list_by_task(self, task_id: str) -> list[Effect]:
        with self._session() as s:
            rows = s.scalars(
                select(EffectRow).where(EffectRow.task_id == task_id)
                .order_by(EffectRow.created_at, EffectRow.id)
            ).all()
            return [self._to_effect(r) for r in rows]

    def list_unresolved(self, task_id: str | None = None) -> list[Effect]:
        """Every effect that still owes an answer: PENDING, SENT or UNKNOWN.

        This is the operator's work queue — anything here either never got a
        verdict from the outside world or never asked for one.
        """
        with self._session() as s:
            stmt = select(EffectRow).where(
                EffectRow.status.in_([s_.value for s_ in UNRESOLVED_STATUSES]))
            if task_id is not None:
                stmt = stmt.where(EffectRow.task_id == task_id)
            rows = s.scalars(stmt.order_by(EffectRow.created_at, EffectRow.id)).all()
            return [self._to_effect(r) for r in rows]

    # -------------------------------------------------------------- mapping

    @staticmethod
    def _to_effect(row: EffectRow) -> Effect:
        data = {name: _aware(getattr(row, name)) if name.endswith("_at")
                else getattr(row, name)
                for name in Effect.model_fields}
        data["status"] = EffectStatus(row.status)
        return Effect(**data)


class EffectExecutor:
    """Runs side effects through ``ctx.run_typed`` and keeps their status.

    Outcomes are *returned* on the Effect (check ``effect.status``); misuse —
    re-firing something ambiguous, reconciling something that is not UNKNOWN —
    *raises*. A swallowed failure would be a lie about the external world; a
    refused retry is the whole feature.
    """

    def __init__(self, store: EffectStore, ctx: KernelContext) -> None:
        self.store = store
        self.ctx = ctx

    # -------------------------------------------------------------- execute

    async def execute(self, *, task_id: str, operation: str,
                      fn: Callable[..., Awaitable[Any]], idempotent: bool,
                      reconcile: Callable[[Effect], Awaitable[Reconciliation]] | None = None,
                      request_ref: str | None = None,
                      effect_key: str | None = None,
                      **kwargs: Any) -> Effect:
        """Create (or resume) an Effect and run ``fn`` durably exactly once.

        ``idempotent=True`` means the external API accepts an idempotency key;
        the Effect ID is passed to ``fn`` as ``idempotency_key=`` so a retry is
        safe. ``reconcile``, when given, is used automatically to resolve an
        ambiguous outcome before this call returns.

        The Effect ID is derived deterministically from ``task_id``/``operation``
        so a replay addresses the *same* effect row and the *same* journal entry
        rather than minting a new identity — pass ``effect_key`` to distinguish
        two effects of the same operation within one task.
        """
        effect_id = deterministic_id("eff", task_id, operation, effect_key or operation)
        effect = self.store.get(effect_id)
        if effect is None:
            effect = Effect(id=effect_id, task_id=task_id, operation=operation,
                            idempotency_key=effect_id if idempotent else None,
                            request_ref=request_ref)
            self.store.save(effect)

        if effect.status is EffectStatus.CONFIRMED:
            return effect
        if effect.status is EffectStatus.COMPENSATED:
            raise IllegalTransition(effect.id, effect.status, EffectStatus.SENT)
        if not effect.may_retry:
            # UNKNOWN + non-idempotent, or still in flight. Never a blind retry.
            if effect.needs_reconciliation and reconcile is not None:
                effect = await self.reconcile(effect.id, reconcile)
                if effect.status is EffectStatus.CONFIRMED:
                    return effect
                if not effect.may_retry:
                    raise ReconciliationRequired(effect)
            else:
                raise ReconciliationRequired(effect)

        # An idempotent retry of an UNKNOWN effect stays UNKNOWN: the outcome is
        # genuinely still unknown until the duplicate request comes back.
        if effect.status in (EffectStatus.PENDING, EffectStatus.FAILED):
            effect.transition_to(EffectStatus.SENT)
        effect.attempts += 1
        self.store.save(effect)

        call_kwargs = dict(kwargs)
        if effect.is_idempotent:
            call_kwargs["idempotency_key"] = effect.idempotency_key

        try:
            # run_typed is what makes this replay-safe. Effect adds nothing here.
            result = await self.ctx.run_typed(self._step_name(effect), fn, **call_kwargs)
        except AMBIGUOUS_ERRORS as exc:
            effect.error = f"{type(exc).__name__}: {exc}"
            if effect.status is not EffectStatus.UNKNOWN:
                effect.transition_to(EffectStatus.UNKNOWN)
            else:
                effect.updated_at = _utcnow()
            self.store.save(effect)
            if reconcile is not None:
                return await self.reconcile(effect.id, reconcile)
            return effect
        except Exception as exc:  # definite failure: nothing was applied
            effect.error = f"{type(exc).__name__}: {exc}"
            effect.transition_to(EffectStatus.FAILED)
            self.store.save(effect)
            return effect

        effect.result_ref = _as_ref(result)
        effect.error = None
        effect.transition_to(EffectStatus.CONFIRMED)
        return self.store.save(effect)

    # ------------------------------------------------------------ reconcile

    async def reconcile(self, effect_id: str,
                        reconciler: Callable[[Effect], Awaitable[Reconciliation]]) -> Effect:
        """Ask the outside world what actually happened, then resolve UNKNOWN.

        The reconciler reports the true external outcome; this method only maps
        that verdict onto ``UNKNOWN -> CONFIRMED`` or ``UNKNOWN -> FAILED``. If
        the reconciler cannot tell, it must raise — the effect then stays
        UNKNOWN, which is the honest answer, and remains un-retryable.
        """
        effect = self.store.get(effect_id)
        if effect is None:
            raise KeyError(f"unknown effect: {effect_id}")
        if effect.status is not EffectStatus.UNKNOWN:
            raise ValueError(
                f"effect {effect_id} is {effect.status.value}, not UNKNOWN; "
                "only an ambiguous outcome needs reconciling")

        # One durable verdict per effect: re-asking on replay could answer
        # differently and desynchronise the status machine from the world.
        verdict: Reconciliation = await self.ctx.run_typed(
            f"reconcile:{effect.id}", reconciler, effect=effect)

        if verdict.applied:
            effect.result_ref = verdict.result_ref or effect.result_ref
            effect.error = None
            effect.transition_to(EffectStatus.CONFIRMED)
        else:
            effect.error = verdict.error or "reconciled: not applied"
            effect.transition_to(EffectStatus.FAILED)
        return self.store.save(effect)

    # ----------------------------------------------------------- compensate

    async def compensate(self, effect_id: str,
                         fn: Callable[..., Awaitable[Any]] | None = None,
                         **kwargs: Any) -> Effect:
        """Undo a CONFIRMED effect by running a compensating operation.

        A confirmed external effect is never un-confirmed; it is superseded by
        a compensating one, which is why COMPENSATED is reachable only from
        CONFIRMED.
        """
        effect = self.store.get(effect_id)
        if effect is None:
            raise KeyError(f"unknown effect: {effect_id}")
        if not effect.can_transition_to(EffectStatus.COMPENSATED):
            raise IllegalTransition(effect.id, effect.status, EffectStatus.COMPENSATED)
        if fn is not None:
            call_kwargs = dict(kwargs)
            if effect.is_idempotent:
                call_kwargs["idempotency_key"] = f"{effect.id}:compensate"
            await self.ctx.run_typed(f"compensate:{effect.id}", fn, **call_kwargs)
        effect.transition_to(EffectStatus.COMPENSATED)
        return self.store.save(effect)

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _step_name(effect: Effect) -> str:
        """The journal key. Stable across replays because the Effect ID is."""
        return f"effect:{effect.id}"
