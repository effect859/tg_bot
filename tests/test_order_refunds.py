"""Возврат кредитов за сбой генерации после оплаты (сервис и проход восстановления).

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_order_refunds_concurrency.py. Сбои вызываются
настоящими путями 3.3–3.4: генерация (FakeLLMClient с постоянной ошибкой) и
завершение (рендерер с некорректным документом).
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BalanceTransaction, Order, User
from app.domain.billing import order_price
from app.domain.enums import DocType, OrderStatus, TransactionReason
from app.domain.errors import InvalidStatusTransitionError, OrderNotRefundedError
from app.domain.report_document import DocumentErrorCode, InvalidDocumentError
from app.domain.report_generation import ReportOutcomeKind
from app.llm.base import LLMPermanentError
from app.rendering.docx import PracticeReportDocxRenderer
from app.services import billing as billing_service
from app.services import order_refunds as service
from app.services import orders
from app.services.billing import (
    ChargeOutcomeKind,
    RefundOutcome,
    RefundOutcomeKind,
    ledger_balance,
    pay_order,
    refund_failed_order,
)
from app.services.order_refunds import RefundRecovery, find_orders_for_refund, run_refund_pass
from app.services.report_completion import CompletionOutcomeKind, complete_report
from app.services.report_generation import generate_next_section
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from fake_llm import FakeLLMClient
from report_samples import ALL_GOOD, generating_order, payment_pending_order
from test_report_completion import RecordingRenderer

S = OrderStatus
K = RefundOutcomeKind
PRICE = order_price(DocType.PRACTICE_REPORT)
TG_ID = 777_555_444


async def _user_id(session: AsyncSession, order_id: int) -> int:
    user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
    assert user_id is not None
    return user_id


async def _fresh(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _reasons(session: AsyncSession, user_id: int) -> list[str]:
    rows = await session.scalars(
        select(BalanceTransaction.reason)
        .where(BalanceTransaction.user_id == user_id)
        .order_by(BalanceTransaction.id)
    )
    return [reason.value for reason in rows]


async def _ledger_ok(session: AsyncSession, user_id: int) -> int:
    """users.balance == SUM(balance_transactions.amount); возвращает баланс."""
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    assert user.balance == await ledger_balance(session, user_id)
    assert user.balance >= 0
    return user.balance


async def _generation_failed(session: AsyncSession, tg_id: int = TG_ID) -> int:
    """Оплаченный заказ, генерация которого упала (постоянная ошибка модели)."""
    order_id = await generating_order(session, tg_id=tg_id)
    outcome = await generate_next_section(
        session, FakeLLMClient(LLMPermanentError("http_400")), order_id
    )
    assert outcome.kind is ReportOutcomeKind.FAILED
    return order_id


async def _completion_failed(session: AsyncSession, tmp_path: Path, tg_id: int = TG_ID) -> int:
    """Оплаченный заказ: разделы готовы, завершение упало (документ некорректен)."""
    order_id = await generating_order(session, tg_id=tg_id)
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        assert (await generate_next_section(session, llm, order_id)).kind is ReportOutcomeKind.SAVED
    renderer = RecordingRenderer(error=InvalidDocumentError(DocumentErrorCode.TOO_LARGE))
    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)
    assert outcome.kind is CompletionOutcomeKind.FAILED
    return order_id


async def _failed_from(session: AsyncSession, failed_from: S, tg_id: int) -> int:
    """Заказ в FAILED с заданным шагом сбоя, выставленный в обход сервиса."""
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    await session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(status=S.FAILED, failed_from_status=failed_from)
    )
    await session.commit()
    return order.id


# --- Возврат --------------------------------------------------------------------------


async def test_generation_failure_is_refunded_and_cancelled(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)

    outcome = await refund_failed_order(session, order_id)

    assert not session.in_transaction()  # один commit, транзакция закрыта
    assert outcome == RefundOutcome(K.REFUNDED, order_id, PRICE, balance=PRICE, chat_id=TG_ID)
    order = await _fresh(session, order_id)
    assert (order.status, order.failed_from_status) == (S.CANCELLED, None)
    assert order.cancelled_at is not None
    assert await _reasons(session, user_id) == ["bonus", "order_charge", "order_refund"]
    [refund] = await session.scalars(
        select(BalanceTransaction).where(
            BalanceTransaction.order_id == order_id,
            BalanceTransaction.reason == TransactionReason.ORDER_REFUND,
        )
    )
    assert (refund.amount, refund.user_id, refund.payment_id) == (PRICE, user_id, None)
    assert await _ledger_ok(session, user_id) == PRICE


async def test_completion_failure_is_refunded(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _completion_failed(session, tmp_path)
    user_id = await _user_id(session, order_id)

    outcome = await refund_failed_order(session, order_id)

    assert outcome.kind is K.REFUNDED
    assert (await _fresh(session, order_id)).status is S.CANCELLED
    assert await _ledger_ok(session, user_id) == PRICE


async def test_repeated_refund_changes_nothing(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)
    await refund_failed_order(session, order_id)

    for _ in range(3):
        assert (await refund_failed_order(session, order_id)).kind is K.SKIPPED

    assert await _reasons(session, user_id) == ["bonus", "order_charge", "order_refund"]
    assert await _ledger_ok(session, user_id) == PRICE
    assert order_id not in await find_orders_for_refund(session, limit=10)


async def test_refund_amount_is_the_actual_charge_not_price(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)
    # Цена в заказе «уехала» — возвращается фактическое списание.
    await session.execute(update(Order).where(Order.id == order_id).values(price=PRICE * 3))
    await session.commit()

    outcome = await refund_failed_order(session, order_id)

    assert (outcome.kind, outcome.amount) == (K.REFUNDED, PRICE)
    assert await _ledger_ok(session, user_id) == PRICE


async def test_failed_generation_without_charge_is_skipped(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """OD-5: заказ без ORDER_CHARGE не отменяется и не получает возврат."""
    order_id = await _failed_from(session, S.GENERATING, TG_ID)
    user_id = await _user_id(session, order_id)

    outcome = await refund_failed_order(session, order_id)

    assert outcome == RefundOutcome(K.SKIPPED, order_id)
    assert f"no credit charge: order_id={order_id}" in caplog.text
    assert (await _fresh(session, order_id)).status is S.FAILED
    assert await _reasons(session, user_id) == []


@pytest.mark.parametrize("failed_from", [S.ANALYZING_MATERIALS, S.PREPARING_PLAN])
async def test_failure_before_payment_is_not_refunded(
    session: AsyncSession, failed_from: S
) -> None:
    order_id = await _failed_from(session, failed_from, TG_ID)
    user_id = await _user_id(session, order_id)

    assert (await refund_failed_order(session, order_id)).kind is K.SKIPPED

    assert (await _fresh(session, order_id)).status is S.FAILED
    assert await _reasons(session, user_id) == []
    # Отмена сбоя до оплаты — как раньше, без возврата.
    order = await orders.change_status(session, order_id, S.CANCELLED)
    assert order.status is S.CANCELLED


async def test_other_statuses_are_not_refunded(session: AsyncSession, tmp_path: Path) -> None:
    generating = await generating_order(session, tg_id=TG_ID)
    pending = await payment_pending_order(session, tg_id=TG_ID + 1)
    completed = await generating_order(session, tg_id=TG_ID + 2)
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        await generate_next_section(session, llm, completed)
    await complete_report(
        session, LocalFileStorage(tmp_path), PracticeReportDocxRenderer(), completed
    )
    assert (await _fresh(session, completed)).status is S.COMPLETED

    for order_id in (generating, pending, completed, 10**9):
        assert (await refund_failed_order(session, order_id)).kind is K.SKIPPED
    for order_id in (generating, pending, completed):
        reasons = await _reasons(session, await _user_id(session, order_id))
        assert "order_refund" not in reasons


async def test_cancel_paid_failed_order_without_refund_is_refused(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)

    with pytest.raises(OrderNotRefundedError) as error:
        await orders.change_status(session, order_id, S.CANCELLED)
    # Для хендлеров это обычный отказ в переходе.
    assert isinstance(error.value, InvalidStatusTransitionError)
    await session.rollback()

    assert (await _fresh(session, order_id)).status is S.FAILED


async def test_failure_after_refund_insert_rolls_everything_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)
    real_change_status = billing_service.change_status

    async def broken(session: AsyncSession, order_id: int, target: S, **kwargs: Any) -> Order:
        if target is S.CANCELLED:
            raise RuntimeError("connection lost")
        return await real_change_status(session, order_id, target, **kwargs)

    monkeypatch.setattr(billing_service, "change_status", broken)
    with pytest.raises(RuntimeError):
        await refund_failed_order(session, order_id)
    await session.rollback()

    assert (await _fresh(session, order_id)).status is S.FAILED
    assert await _reasons(session, user_id) == ["bonus", "order_charge"]
    assert await _ledger_ok(session, user_id) == 0


async def test_unique_refund_is_the_last_line(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Возврат уже вставлен другим, а проверка его «не увидела»: IntegrityError, откат."""
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)
    session.add(
        BalanceTransaction(
            user_id=user_id, amount=PRICE, reason=TransactionReason.ORDER_REFUND, order_id=order_id
        )
    )
    user = await session.get(User, user_id)
    assert user is not None
    user.balance += PRICE
    await session.commit()
    real_transaction = billing_service._transaction

    async def blind(session: AsyncSession, oid: int, reason: TransactionReason) -> Any:
        if reason is TransactionReason.ORDER_REFUND:
            return None
        return await real_transaction(session, oid, reason)

    monkeypatch.setattr(billing_service, "_transaction", blind)
    with pytest.raises(IntegrityError, match="uq_balance_transactions_order_id_reason"):
        await refund_failed_order(session, order_id)
    await session.rollback()
    monkeypatch.setattr(billing_service, "_transaction", real_transaction)

    # Откат целиком: один возврат, баланс сходится, заказ не отменён этой попыткой.
    assert await _reasons(session, user_id) == ["bonus", "order_charge", "order_refund"]
    assert await _ledger_ok(session, user_id) == PRICE
    assert (await refund_failed_order(session, order_id)).kind is K.ALREADY_REFUNDED


async def test_new_order_after_refund_can_be_paid(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)
    await refund_failed_order(session, order_id)

    # Заказ неактивен: можно оформить новый и оплатить возвращёнными кредитами.
    new_order = await payment_pending_order(session, tg_id=TG_ID)
    outcome = await pay_order(session, user_id, new_order)

    assert outcome.kind is ChargeOutcomeKind.PAID
    assert await _ledger_ok(session, user_id) == 0
    assert (await _fresh(session, new_order)).status is S.GENERATING


# --- Проход восстановления ------------------------------------------------------------


async def test_find_orders_for_refund(session: AsyncSession, tmp_path: Path) -> None:
    no_charge = await _failed_from(session, S.GENERATING, TG_ID + 10)
    charged = await _generation_failed(session, tg_id=TG_ID + 11)
    refunded = await _generation_failed(session, tg_id=TG_ID + 12)
    await refund_failed_order(session, refunded)
    pre_payment = await _failed_from(session, S.PREPARING_PLAN, TG_ID + 13)
    generating = await generating_order(session, tg_id=TG_ID + 14)

    found = await find_orders_for_refund(session, limit=10)

    # Со списанием — первыми: заказы без списания не занимают пакет.
    assert found == [charged, no_charge]
    assert not {refunded, pre_payment, generating} & set(found)
    assert await find_orders_for_refund(session, limit=1) == [charged]
    assert not session.in_transaction()


async def test_pass_refunds_and_notifies_after_commit(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    order_id = await _generation_failed(session)
    notified: list[tuple[int, RefundOutcome, bool]] = []

    async def notify(chat_id: int, outcome: RefundOutcome) -> None:
        # Уведомление — после commit: заказ уже отменён в БД.
        async with sessionmaker() as other:
            order = await other.get(Order, order_id)
            assert order is not None
            notified.append((chat_id, outcome, order.status is S.CANCELLED))

    outcomes = await run_refund_pass(sessionmaker, notify)

    assert [(o.order_id, o.kind) for o in outcomes] == [(order_id, K.REFUNDED)]
    assert [(chat, o.amount, cancelled) for chat, o, cancelled in notified] == [
        (TG_ID, PRICE, True)
    ]
    # Второй проход: возвращать нечего, уведомлений нет.
    assert await run_refund_pass(sessionmaker, notify) == []
    assert len(notified) == 1


async def test_notification_failure_keeps_refund(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    order_id = await _generation_failed(session)
    user_id = await _user_id(session, order_id)

    async def broken(chat_id: int, outcome: RefundOutcome) -> None:
        raise RuntimeError(f"telegram down for {chat_id}")

    outcomes = await run_refund_pass(sessionmaker, broken)

    assert [o.kind for o in outcomes] == [K.REFUNDED]
    assert (await _fresh(session, order_id)).status is S.CANCELLED
    assert await _ledger_ok(session, user_id) == PRICE
    assert f"Refund notification failed: order_id={order_id} error=RuntimeError" in caplog.text
    assert str(TG_ID) not in caplog.text


async def test_pass_continues_after_order_exception(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = await _generation_failed(session, tg_id=TG_ID + 20)
    ok = await _generation_failed(session, tg_id=TG_ID + 21)
    real_refund = service.refund_failed_order

    async def refund(session: AsyncSession, order_id: int) -> RefundOutcome:
        if order_id == broken:
            raise RuntimeError("secret details")
        return await real_refund(session, order_id)

    monkeypatch.setattr(service, "refund_failed_order", refund)

    async def notify(chat_id: int, outcome: RefundOutcome) -> None:
        pass

    outcomes = await run_refund_pass(sessionmaker, notify)

    assert [(o.order_id, o.kind) for o in outcomes] == [(ok, K.REFUNDED)]
    assert f"order_id={broken} error=RuntimeError" in caplog.text
    assert "secret details" not in caplog.text
    assert (await _fresh(session, broken)).status is S.FAILED


async def test_pass_respects_batch(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for offset in range(3):
        await _generation_failed(session, tg_id=TG_ID + 30 + offset)

    async def notify(chat_id: int, outcome: RefundOutcome) -> None:
        pass

    sizes = [len(await run_refund_pass(sessionmaker, notify, batch=2)) for _ in range(3)]

    assert sizes == [2, 1, 0]


async def test_recovery_runs_first_pass_at_start(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    order_id = await _generation_failed(session)
    passes: list[list[RefundOutcome]] = []

    async def notify(chat_id: int, outcome: RefundOutcome) -> None:
        pass

    class Recording(RefundRecovery):
        async def run_pass(self) -> list[RefundOutcome]:
            outcomes = await super().run_pass()
            passes.append(outcomes)
            return outcomes

    recovery = Recording(sessionmaker, notify, interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(250):
            if passes:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert [(o.order_id, o.kind) for o in passes[0]] == [(order_id, K.REFUNDED)]
    assert not recovery.running


async def test_logs_have_no_telegram_id(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await _generation_failed(session)
    await _failed_from(session, S.GENERATING, TG_ID + 1)

    async def notify(chat_id: int, outcome: RefundOutcome) -> None:
        pass

    await run_refund_pass(sessionmaker, notify)

    assert "Failed order refunded" in caplog.text and "no credit charge" in caplog.text
    assert str(TG_ID) not in caplog.text and str(TG_ID + 1) not in caplog.text


def test_refund_constants() -> None:
    assert service.RECOVERY_INTERVAL_SECONDS == 60.0
    assert service.RECOVERY_BATCH == 5


async def test_failed_generation_status_consistent_in_database(session: AsyncSession) -> None:
    """Возврат и CANCELLED: CHECK failed_from_status_consistent соблюдён (failed_from NULL)."""
    order_id = await _generation_failed(session)
    await refund_failed_order(session, order_id)

    row = (
        await session.execute(
            text("SELECT status, failed_from_status FROM orders WHERE id = :id"), {"id": order_id}
        )
    ).one()
    assert tuple(row) == ("cancelled", None)
