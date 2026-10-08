import pytest

from app.domain.enums import OrderStatus as S
from app.domain.errors import InvalidStatusTransitionError
from app.domain.order_flow import (
    FAILABLE_STATUSES,
    PRE_PAYMENT_STATUSES,
    TERMINAL_STATUSES,
    TRANSITIONS,
    allowed_transitions,
    check_transition,
    is_active,
)

ALLOWED = [(src, dst) for src, targets in TRANSITIONS.items() for dst in sorted(targets)]
FORBIDDEN = [
    (S.NEW, S.PAID),
    (S.NEW, S.COMPLETED),
    (S.NEW, S.GENERATING),
    (S.WAITING_FOR_DATA_CONFIRMATION, S.PAID),
    (S.WAITING_FOR_PLAN_CONFIRMATION, S.PAID),
    (S.PAYMENT_PENDING, S.GENERATING),
    (S.PAID, S.CANCELLED),
    (S.GENERATING, S.CANCELLED),
    (S.COMPLETED, S.CANCELLED),
    (S.NEW, S.FAILED),
    (S.PAYMENT_PENDING, S.FAILED),
]


def test_map_covers_every_status_except_failed() -> None:
    # FAILED — динамический: переходы зависят от failed_from_status.
    assert set(TRANSITIONS) == set(S) - {S.FAILED}


@pytest.mark.parametrize(("src", "dst"), ALLOWED)
def test_allowed_transition(src: S, dst: S) -> None:
    check_transition(src, dst)


@pytest.mark.parametrize(("src", "dst"), FORBIDDEN)
def test_forbidden_transition(src: S, dst: S) -> None:
    with pytest.raises(InvalidStatusTransitionError):
        check_transition(src, dst)


def test_only_failable_steps_can_fail() -> None:
    can_fail = {src for src, targets in TRANSITIONS.items() if S.FAILED in targets}
    assert can_fail == FAILABLE_STATUSES


def test_cancel_only_before_payment() -> None:
    can_cancel = {src for src, targets in TRANSITIONS.items() if S.CANCELLED in targets}
    assert can_cancel == PRE_PAYMENT_STATUSES


@pytest.mark.parametrize("dst", list(S))
def test_cancelled_is_terminal(dst: S) -> None:
    assert {S.CANCELLED} == TERMINAL_STATUSES
    with pytest.raises(InvalidStatusTransitionError):
        check_transition(S.CANCELLED, dst)


def test_failed_before_payment_allows_retry_or_cancel_only() -> None:
    assert allowed_transitions(S.FAILED, S.ANALYZING_MATERIALS) == {
        S.ANALYZING_MATERIALS,
        S.CANCELLED,
    }
    # Сбой до оплаты не открывает путь к генерации.
    with pytest.raises(InvalidStatusTransitionError):
        check_transition(S.FAILED, S.GENERATING, S.ANALYZING_MATERIALS)
    with pytest.raises(InvalidStatusTransitionError):
        check_transition(S.FAILED, S.PAID, S.PREPARING_PLAN)


def test_failed_after_payment_allows_retry_or_refunded_cancel() -> None:
    # Отмена после сбоя генерации — только с возвратом кредитов (Stage 3.7):
    # это проверяет services.orders.change_status.
    assert allowed_transitions(S.FAILED, S.GENERATING) == {S.GENERATING, S.CANCELLED}
    assert allowed_transitions(S.FAILED, S.REGENERATING) == {S.REGENERATING}


def test_failed_without_origin_allows_nothing() -> None:
    assert allowed_transitions(S.FAILED, None) == frozenset()


def test_active_statuses() -> None:
    assert not is_active(S.COMPLETED)
    assert not is_active(S.CANCELLED)
    assert is_active(S.FAILED)
    assert is_active(S.NEW)
