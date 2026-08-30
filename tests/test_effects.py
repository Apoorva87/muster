"""Effect: the status machine, the idempotency key, and reconcile-before-retry.

Decision D4 draws a hard line these tests exist to defend: ``run_typed`` gives
replay-safe steps, and that is *not* the same thing as exactly-once external
side effects. See ``test_d4_replay_protection_is_not_exactly_once``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.kernel.context import FakeKernelContext
from bus.models.effect import (ALLOWED_TRANSITIONS, Effect, EffectStatus,
                               IllegalTransition, Reconciliation)
from bus.routing.effects import (AmbiguousOutcome, EffectExecutor, EffectStore,
                                 ReconciliationRequired)

S = EffectStatus


# --------------------------------------------------------------------- doubles


class TimedOutButApplied(TimeoutError):
    """The far side did the work; our socket died before we heard about it.

    This is the only interesting failure mode. Everything else is easy.
    """


class FakeApi:
    """Stand-in external API. Counts invocations *and* real state changes.

    ``calls`` is every request that reached it; ``charges`` is how many times
    the world actually changed. An idempotency key collapses a duplicate, so a
    safe retry moves ``calls`` without moving ``charges`` — which is exactly the
    property the Effect machinery has to preserve.
    """

    def __init__(self, *, outcomes: tuple[BaseException | None, ...] = ()) -> None:
        self.calls: list[dict] = []
        self.charges = 0
        self._applied: dict[str, str] = {}
        self._outcomes = list(outcomes)

    async def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        key = kwargs.get("idempotency_key")
        if key is not None and key in self._applied:
            return self._applied[key]          # collapsed duplicate, no new charge
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, BaseException):
            if isinstance(outcome, TimedOutButApplied):
                self.charges += 1
                if key is not None:
                    self._applied[key] = f"ref://receipt/{self.charges}"
            raise outcome
        self.charges += 1
        receipt = f"ref://receipt/{self.charges}"
        if key is not None:
            self._applied[key] = receipt
        return receipt

    @property
    def keys_used(self) -> list[str | None]:
        return [c.get("idempotency_key") for c in self.calls]


async def reconcile_applied(effect: Effect) -> Reconciliation:
    return Reconciliation(applied=True, result_ref="ref://receipt/reconciled")


async def reconcile_not_applied(effect: Effect) -> Reconciliation:
    return Reconciliation(applied=False, error="no such charge at the provider")


async def reconcile_inconclusive(effect: Effect) -> Reconciliation:
    raise AmbiguousOutcome("provider search API is down; still cannot tell")


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def effect_store():
    s = EffectStore.from_url("sqlite://")
    s.init_schema()
    return s


@pytest.fixture
def executor(effect_store, ctx):
    return EffectExecutor(effect_store, ctx)


def make_effect(status: EffectStatus = S.PENDING, *, idempotent: bool = False,
                effect_id: str = "eff_fixture0000001") -> Effect:
    return Effect(id=effect_id, task_id="task_1", operation="charge.card",
                  idempotency_key=effect_id if idempotent else None, status=status)


# ==================================================== the status machine (model)


def test_a_new_effect_starts_pending():
    effect = Effect(task_id="task_1", operation="charge.card")
    assert effect.status is S.PENDING
    assert effect.id.startswith("eff_")
    assert effect.attempts == 0
    assert effect.result_ref is None and effect.error is None


def test_happy_path_pending_sent_confirmed():
    effect = make_effect()
    assert effect.transition_to(S.SENT).status is S.SENT
    assert effect.transition_to(S.CONFIRMED).status is S.CONFIRMED
    assert effect.is_resolved and effect.is_terminal is False  # COMPENSATED remains


def test_transition_bumps_updated_at():
    effect = make_effect()
    effect.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    effect.transition_to(S.SENT)
    assert effect.updated_at.year > 2020


LEGAL_PAIRS = [(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets]
ILLEGAL_PAIRS = [(a, b) for a in EffectStatus for b in EffectStatus
                 if b not in ALLOWED_TRANSITIONS[a]]


@pytest.mark.parametrize("source,target", LEGAL_PAIRS,
                         ids=[f"{a.value}->{b.value}" for a, b in LEGAL_PAIRS])
def test_every_legal_transition_is_accepted(source, target):
    assert make_effect(source).transition_to(target).status is target


@pytest.mark.parametrize("source,target", ILLEGAL_PAIRS,
                         ids=[f"{a.value}->{b.value}" for a, b in ILLEGAL_PAIRS])
def test_every_illegal_transition_raises(source, target):
    effect = make_effect(source)
    with pytest.raises(IllegalTransition) as excinfo:
        effect.transition_to(target)
    assert excinfo.value.source is source and excinfo.value.target is target
    assert effect.status is source, "a rejected transition must not mutate"


def test_the_specific_edges_d4_cares_about():
    # Never claim an outcome for an operation that was never attempted.
    assert not make_effect(S.PENDING).can_transition_to(S.CONFIRMED)
    # Reconcile before retrying: UNKNOWN cannot slide straight back to SENT.
    assert not make_effect(S.UNKNOWN).can_transition_to(S.SENT)
    # ...but it may be resolved either way once someone has looked.
    assert make_effect(S.UNKNOWN).can_transition_to(S.CONFIRMED)
    assert make_effect(S.UNKNOWN).can_transition_to(S.FAILED)
    # CONFIRMED is terminal apart from being deliberately undone.
    assert ALLOWED_TRANSITIONS[S.CONFIRMED] == frozenset({S.COMPENSATED})
    # A definite failure changed nothing outside, so re-attempting is safe.
    assert make_effect(S.FAILED).can_transition_to(S.SENT)
    # Nothing follows COMPENSATED.
    assert make_effect(S.COMPENSATED).is_terminal
    # No status may transition to itself; that is the blind retry.
    for status in EffectStatus:
        assert not make_effect(status).can_transition_to(status)


# ============================================================ retry eligibility


def test_unknown_non_idempotent_effect_is_not_retryable():
    effect = make_effect(S.UNKNOWN, idempotent=False)
    assert effect.is_idempotent is False
    assert effect.may_retry is False
    assert effect.needs_reconciliation is True


def test_unknown_idempotent_effect_is_retryable():
    effect = make_effect(S.UNKNOWN, idempotent=True)
    assert effect.idempotency_key == effect.id
    assert effect.may_retry is True
    assert effect.needs_reconciliation is False


@pytest.mark.parametrize("status,expected", [
    (S.PENDING, True),      # not yet dispatched
    (S.SENT, False),        # in flight; a second fire is the bug
    (S.CONFIRMED, False),   # done
    (S.FAILED, True),       # definitively not applied
    (S.COMPENSATED, False), # done, then undone
])
def test_may_retry_by_status(status, expected):
    assert make_effect(status).may_retry is expected


def test_unresolved_statuses():
    assert [s for s in EffectStatus if not make_effect(s).is_resolved] == [
        S.PENDING, S.SENT, S.UNKNOWN]


# ================================================================ the executor


async def test_execute_walks_pending_sent_confirmed(executor, effect_store, ctx):
    api = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False, amount=100)
    assert effect.status is S.CONFIRMED
    assert effect.attempts == 1
    assert effect.result_ref == "ref://receipt/1"
    assert api.charges == 1 and len(api.calls) == 1
    assert api.calls[0] == {"amount": 100}       # no key: the API has none
    assert effect_store.get(effect.id).status is S.CONFIRMED
    assert ctx.journal_names() == [f"effect:{effect.id}"]


async def test_idempotent_execute_passes_the_effect_id_as_the_key(executor):
    api = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=True, amount=100)
    assert effect.idempotency_key == effect.id
    assert api.keys_used == [effect.id]


async def test_a_definite_failure_becomes_failed_and_stays_retryable(executor):
    api = FakeApi(outcomes=(ValueError("card declined"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    assert effect.status is S.FAILED
    assert "card declined" in effect.error
    assert api.charges == 0
    assert effect.may_retry is True

    # ...and the retry goes FAILED -> SENT -> CONFIRMED on the same effect.
    retried = await executor.execute(task_id="task_1", operation="charge.card",
                                     fn=api, idempotent=False)
    assert retried.id == effect.id
    assert retried.status is S.CONFIRMED and retried.attempts == 2


async def test_an_ambiguous_failure_becomes_unknown_not_failed(executor):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    assert effect.status is S.UNKNOWN
    assert "TimedOutButApplied" in effect.error
    assert api.charges == 1, "the world changed even though we never heard back"


async def test_result_ref_accepts_a_reference_bearing_dict(executor):
    async def fn(**_kwargs):
        return {"ref": "art_123", "body": "a megabyte of receipt HTML"}

    effect = await executor.execute(task_id="task_1", operation="file.report",
                                    fn=fn, idempotent=False)
    assert effect.result_ref == "art_123", "references only, never payloads"


# ================================================== reconcile-before-retry (D4)


async def test_unknown_non_idempotent_effect_refuses_a_blind_retry(executor):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    first = await executor.execute(task_id="task_1", operation="charge.card",
                                   fn=api, idempotent=False)
    assert first.status is S.UNKNOWN

    with pytest.raises(ReconciliationRequired) as excinfo:
        await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False)
    assert excinfo.value.effect.id == first.id
    assert len(api.calls) == 1, "the refusal must happen before the API is touched"
    assert api.charges == 1


async def test_unknown_idempotent_effect_retries_safely(executor):
    """The Effect ID is the key, so the duplicate collapses on the far side."""
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    first = await executor.execute(task_id="task_1", operation="charge.card",
                                   fn=api, idempotent=True, amount=100)
    assert first.status is S.UNKNOWN and first.may_retry

    second = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=True, amount=100)
    assert second.id == first.id
    assert second.status is S.CONFIRMED
    assert second.attempts == 2
    assert len(api.calls) == 2, "we did re-request"
    assert api.charges == 1, "but the customer was charged exactly once"
    assert api.keys_used == [first.id, first.id]


async def test_reconcile_resolves_unknown_to_confirmed(executor, effect_store):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)

    resolved = await executor.reconcile(effect.id, reconcile_applied)
    assert resolved.status is S.CONFIRMED
    assert resolved.result_ref == "ref://receipt/reconciled"
    assert resolved.error is None
    assert effect_store.get(effect.id).status is S.CONFIRMED
    assert len(api.calls) == 1, "reconciling is a read, not a re-send"


async def test_reconcile_resolves_unknown_to_failed_and_unblocks_retry(executor):
    api = FakeApi(outcomes=(AmbiguousOutcome("connection reset mid-request"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    assert effect.status is S.UNKNOWN and not effect.may_retry

    resolved = await executor.reconcile(effect.id, reconcile_not_applied)
    assert resolved.status is S.FAILED
    assert resolved.error == "no such charge at the provider"
    assert resolved.may_retry is True, "proven not applied, so a retry is safe now"


async def test_an_inconclusive_reconciler_leaves_the_effect_unknown(executor,
                                                                    effect_store):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)

    with pytest.raises(AmbiguousOutcome):
        await executor.reconcile(effect.id, reconcile_inconclusive)
    still = effect_store.get(effect.id)
    assert still.status is S.UNKNOWN and not still.may_retry


async def test_reconcile_rejects_an_effect_that_is_not_unknown(executor):
    api = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    with pytest.raises(ValueError, match="not UNKNOWN"):
        await executor.reconcile(effect.id, reconcile_applied)


async def test_reconcile_rejects_an_unknown_id(executor):
    with pytest.raises(KeyError):
        await executor.reconcile("eff_nope", reconcile_applied)


async def test_a_supplied_reconciler_resolves_the_ambiguity_inline(executor):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False,
                                    reconcile=reconcile_applied)
    assert effect.status is S.CONFIRMED
    assert api.charges == 1


async def test_reentry_reconciles_then_retries_when_nothing_was_applied(executor):
    api = FakeApi(outcomes=(AmbiguousOutcome("connection reset"),))
    first = await executor.execute(task_id="task_1", operation="charge.card",
                                   fn=api, idempotent=False)
    assert first.status is S.UNKNOWN

    second = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False,
                                    reconcile=reconcile_not_applied)
    assert second.status is S.CONFIRMED
    assert api.charges == 1, "reconciliation proved the first attempt did nothing"


# ================================================== the D4 boundary, explicitly


async def test_d4_replay_protection_is_not_exactly_once(effect_store, ctx):
    """Replay safety and exactly-once external side effects are different things.

    ``run_typed`` journals *completed* steps. A step that timed out never
    completed, so replay offers no opinion about it at all — and for a
    non-idempotent operation the workflow cannot re-run it either, because the
    money may already be gone. The only way forward is reconciliation. This is
    the entire justification for D4 and for this module existing.
    """
    executor = EffectExecutor(effect_store, ctx)
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))

    effect = await executor.execute(task_id="task_1", operation="wire.transfer",
                                    fn=api, idempotent=False, amount=50_000)

    # 1. Restate's journal is empty for this step: it never completed, so replay
    #    protection has nothing to say and would happily let it run again.
    assert ctx.journal_names() == []
    assert effect.status is S.UNKNOWN

    # 2. The world may already have changed. It has, in fact — and nothing in
    #    the durable-execution layer can tell us that.
    assert api.charges == 1

    # 3. So a replayed workflow must NOT re-fire it. The Effect status machine,
    #    not the journal, is what stops the second wire transfer.
    ctx.replay()
    replayed = EffectExecutor(effect_store, ctx)
    with pytest.raises(ReconciliationRequired, match="reconcile before retrying"):
        await replayed.execute(task_id="task_1", operation="wire.transfer",
                               fn=api, idempotent=False, amount=50_000)
    assert api.charges == 1 and len(api.calls) == 1

    # 4. Reconciliation — asking the bank what actually happened — is the only
    #    thing that resolves it, and it does so without moving any more money.
    resolved = await replayed.reconcile(effect.id, reconcile_applied)
    assert resolved.status is S.CONFIRMED
    assert api.charges == 1


# ======================================================================= replay


async def test_replay_does_not_rerun_the_side_effect(effect_store, ctx):
    executor = EffectExecutor(effect_store, ctx)
    api = FakeApi()
    first = await executor.execute(task_id="task_1", operation="charge.card",
                                   fn=api, idempotent=False, amount=100)
    assert len(api.calls) == 1

    ctx.replay()
    replayed = EffectExecutor(effect_store, ctx)
    again = await replayed.execute(task_id="task_1", operation="charge.card",
                                   fn=api, idempotent=False, amount=100)

    assert again.id == first.id
    assert again.status is S.CONFIRMED
    assert len(api.calls) == 1, "the side effect must not run twice"
    assert api.charges == 1


async def test_replay_protection_comes_from_run_typed_not_from_the_store(ctx):
    """Same journal, empty store: ``run_typed`` alone must suppress the call.

    Effect sits *on top of* the durable step and never substitutes for it, so
    losing the Effect row must not cost us replay safety.
    """
    api = FakeApi()
    store_a = EffectStore.from_url("sqlite://")
    store_a.init_schema()
    first = await EffectExecutor(store_a, ctx).execute(
        task_id="task_1", operation="charge.card", fn=api, idempotent=False)
    assert len(api.calls) == 1

    ctx.replay()
    store_b = EffectStore.from_url("sqlite://")   # nothing persisted here
    store_b.init_schema()
    again = await EffectExecutor(store_b, ctx).execute(
        task_id="task_1", operation="charge.card", fn=api, idempotent=False)

    assert again.id == first.id, "the Effect ID is derived, not minted"
    assert again.status is S.CONFIRMED
    assert again.result_ref == first.result_ref
    assert len(api.calls) == 1
    assert api.charges == 1


async def test_the_effect_id_is_stable_across_calls(executor):
    api = FakeApi()
    a = await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False)
    b = await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False)
    assert a.id == b.id and len(api.calls) == 1


async def test_effect_key_separates_two_effects_of_one_operation(executor):
    api = FakeApi()
    a = await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False, effect_key="deposit")
    b = await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False, effect_key="balance")
    assert a.id != b.id
    assert api.charges == 2


# ================================================================== persistence


def test_effects_persist_and_reload(effect_store):
    effect = Effect(task_id="task_9", operation="charge.card",
                    idempotency_key="eff_abc", status=S.UNKNOWN,
                    request_ref="art_req", result_ref=None, attempts=2,
                    error="TimeoutError: gateway timeout")
    effect_store.save(effect)

    loaded = effect_store.get(effect.id)
    assert loaded is not None
    assert loaded.model_dump() == effect.model_dump()
    assert loaded.status is S.UNKNOWN
    assert loaded.created_at.tzinfo is not None, "SQLite drops tzinfo; put it back"
    assert loaded.updated_at.tzinfo is not None


def test_saving_twice_updates_in_place(effect_store):
    effect = effect_store.save(make_effect())
    effect_store.save(effect.transition_to(S.SENT))
    assert effect_store.get(effect.id).status is S.SENT
    assert len(effect_store.list_by_task("task_1")) == 1


def test_get_missing_effect_returns_none(effect_store):
    assert effect_store.get("eff_missing") is None


def test_list_by_task_is_scoped(effect_store):
    effect_store.save(make_effect(effect_id="eff_1"))
    effect_store.save(make_effect(effect_id="eff_2"))
    other = make_effect(effect_id="eff_3")
    other.task_id = "task_2"
    effect_store.save(other)

    assert {e.id for e in effect_store.list_by_task("task_1")} == {"eff_1", "eff_2"}
    assert [e.id for e in effect_store.list_by_task("task_2")] == ["eff_3"]


def test_list_unresolved_finds_pending_sent_and_unknown(effect_store):
    for status in EffectStatus:
        effect_store.save(make_effect(status, effect_id=f"eff_{status.value}"))

    unresolved = effect_store.list_unresolved()
    assert {e.status for e in unresolved} == {S.PENDING, S.SENT, S.UNKNOWN}
    assert {e.id for e in unresolved} == {"eff_PENDING", "eff_SENT", "eff_UNKNOWN"}


def test_list_unresolved_can_be_scoped_to_a_task(effect_store):
    effect_store.save(make_effect(S.UNKNOWN, effect_id="eff_1"))
    other = make_effect(S.UNKNOWN, effect_id="eff_2")
    other.task_id = "task_2"
    effect_store.save(other)

    assert [e.id for e in effect_store.list_unresolved("task_2")] == ["eff_2"]


async def test_an_executed_effect_shows_up_unresolved_until_it_resolves(executor,
                                                                        effect_store):
    api = FakeApi(outcomes=(TimedOutButApplied("gateway timeout"),))
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    assert [e.id for e in effect_store.list_unresolved()] == [effect.id]

    await executor.reconcile(effect.id, reconcile_applied)
    assert effect_store.list_unresolved() == []


# ================================================================= compensation


async def test_a_confirmed_effect_can_be_compensated(executor, effect_store):
    api = FakeApi()
    refund = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False, amount=100)

    compensated = await executor.compensate(effect.id, refund, amount=100)
    assert compensated.status is S.COMPENSATED
    assert compensated.is_terminal
    assert refund.charges == 1
    assert effect_store.get(effect.id).status is S.COMPENSATED


async def test_compensation_may_be_a_bookkeeping_only_transition(executor):
    api = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    assert (await executor.compensate(effect.id)).status is S.COMPENSATED


async def test_compensating_an_unconfirmed_effect_is_illegal(executor, effect_store):
    effect = effect_store.save(make_effect(S.UNKNOWN))
    with pytest.raises(IllegalTransition):
        await executor.compensate(effect.id)
    assert effect_store.get(effect.id).status is S.UNKNOWN


async def test_a_compensated_effect_cannot_be_executed_again(executor, effect_store):
    api = FakeApi()
    effect = await executor.execute(task_id="task_1", operation="charge.card",
                                    fn=api, idempotent=False)
    await executor.compensate(effect.id)

    with pytest.raises(IllegalTransition):
        await executor.execute(task_id="task_1", operation="charge.card",
                               fn=api, idempotent=False)
    assert api.charges == 1


async def test_compensate_rejects_an_unknown_id(executor):
    with pytest.raises(KeyError):
        await executor.compensate("eff_nope")


# ======================================================================== docs


def test_both_modules_state_the_d4_boundary():
    """D4 is easy to un-learn during a refactor. Keep the reason next to the code."""
    import bus.models.effect as model_module
    import bus.routing.effects as routing_module

    for module in (model_module, routing_module):
        doc = module.__doc__ or ""
        assert "run_typed" in doc
        assert "never wrapping or replacing" in doc


def test_the_executor_never_reimplements_the_durable_step():
    """Effect sits on top of run_typed; every side effect goes through it."""
    import inspect

    import bus.routing.effects as routing_module

    source = inspect.getsource(routing_module.EffectExecutor)
    assert source.count("self.ctx.run_typed(") == 3   # execute, reconcile, compensate
    assert "await fn(" not in source, "fn must only ever be called via run_typed"
