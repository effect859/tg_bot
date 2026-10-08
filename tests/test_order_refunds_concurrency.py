"""Гонки возврата кредитов между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker).
Апдейты бота моделируются как в production: upsert пользователя (UserMiddleware),
затем хендлер, затем commit (DbSessionMiddleware).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_report_completion_concurrency as completion_races
from app.bot.handlers.order_intake import cancel_order
from app.bot.handlers.plan import pay_order_button
from app.bot.keyboards import OrderAction, OrderCallback, PayOrderCallback
from app.db.models import BalanceTransaction, Order, Payment, User
from app.domain.billing import CREDIT_PACKAGES, order_price
from app.domain.enums import DocType, OrderStatus, PaymentStatus, TransactionReason
from app.domain.report_generation import ReportOutcomeKind
from app.llm.base import LLMPermanentError
from app.services import billing as billing_service
from app.services import orders
from app.services.billing import (
    RefundOutcomeKind,
    TopupOutcomeKind,
    confirm_topup,
    create_topup,
    ledger_balance,
    refund_failed_order,
    topup_payload,
)
from app.services.payments.base import PaymentNotification
from app.services.report_generation import generate_next_section
from app.services.users import register_or_update_user
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient
from report_samples import generating_order

S = OrderStatus
K = RefundOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]
PRICE = order_price(DocType.PRACTICE_REPORT)
PACKAGE = CREDIT_PACKAGES[0]

# Та же фикстура, что у гонок завершения: удаляет разделы, файлы, llm_calls и версии
# тестовых заказов; журнал и платежи удаляет committed_sessionmaker (conftest).
sessions = completion_races.sessions


async def _failed(sessions: Sessionmaker, offset: int) -> tuple[int, int, int]:
    """(order_id, user_id, tg_id) оплаченного заказа, генерация которого упала."""
    tg_id = COMMITTED_TG_ID_BASE + offset
    async with sessions() as session:
        order_id = await generating_order(session, tg_id=tg_id)
        outcome = await generate_next_section(
            session, FakeLLMClient(LLMPermanentError("http_400")), order_id
        )
        assert outcome.kind is ReportOutcomeKind.FAILED
        user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
        assert user_id is not None
    return order_id, user_id, tg_id


async def _refund(sessions: Sessionmaker, order_id: int) -> K:
    async with sessions() as session:
        return (await refund_failed_order(session, order_id)).kind


async def _state(sessions: Sessionmaker, user_id: int) -> tuple[int, int, list[str]]:
    """(users.balance, сумма журнала, причины проводок)."""
    async with sessions() as session:
        user = await session.get(User, user_id)
        assert user is not None
        rows = await session.scalars(
            select(BalanceTransaction.reason)
            .where(BalanceTransaction.user_id == user_id)
            .order_by(BalanceTransaction.id)
        )
        return user.balance, await ledger_balance(session, user_id), [r.value for r in rows]


async def _status(sessions: Sessionmaker, order_id: int) -> S:
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        return order.status


def _callback(tg_id: int) -> AsyncMock:
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.message.chat.id = tg_id
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


async def _upsert(session: AsyncSession, tg_id: int) -> User:
    """Как UserMiddleware: upsert пользователя в начале транзакции апдейта."""
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


REFUNDED = (PRICE, PRICE, ["bonus", "order_charge", "order_refund"])


async def test_concurrent_refunds_refund_once(sessions: Sessionmaker) -> None:
    order_id, user_id, _ = await _failed(sessions, 901)
    barrier = asyncio.Barrier(3)

    async def run() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _refund(sessions, order_id)

    kinds = await asyncio.gather(run(), run(), run())

    assert sorted(kinds) == sorted([K.REFUNDED, K.SKIPPED, K.SKIPPED])
    assert await _state(sessions, user_id) == REFUNDED
    assert await _status(sessions, order_id) is S.CANCELLED


async def test_refund_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id, user_id, _ = await _failed(sessions, 902)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        task = asyncio.create_task(_refund(sessions, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.REFUNDED
    assert await _state(sessions, user_id) == REFUNDED


async def test_refund_waits_for_user_middleware(sessions: Sessionmaker) -> None:
    """Апдейт того же пользователя держит строку пользователя: возврат ждёт её."""
    order_id, user_id, tg_id = await _failed(sessions, 903)

    async with sessions() as update_session:
        await _upsert(update_session, tg_id)
        task = asyncio.create_task(_refund(sessions, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # пользователь — первая блокировка возврата
        await update_session.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.REFUNDED
    assert await _state(sessions, user_id) == REFUNDED


@pytest.mark.parametrize("handler", ["cancel", "pay"])
async def test_refund_vs_order_update_no_deadlock(
    sessions: Sessionmaker, monkeypatch: pytest.MonkeyPatch, handler: str
) -> None:
    """Возврат задержан сразу после блокировки заказа; тем временем апдейт того же
    пользователя («Отменить» или старая «Оплатить») — upsert, затем заказ.

    Порядок у всех пользователь → заказ: апдейт ждёт пользователя, возврат завершается,
    апдейт получает отказ по статусу. Взаимной блокировки нет.
    """
    order_id, user_id, tg_id = await _failed(sessions, 904 if handler == "cancel" else 905)
    order_locked = asyncio.Event()
    real_lock_order = billing_service.lock_order

    async def lock_and_wait(session: AsyncSession, locked_order_id: int) -> Order:
        order = await real_lock_order(session, locked_order_id)
        order_locked.set()
        await asyncio.sleep(0.5)  # окно для апдейта
        return order

    monkeypatch.setattr(billing_service, "lock_order", lock_and_wait)

    async def update() -> AsyncMock:
        await asyncio.wait_for(order_locked.wait(), 10)
        async with sessions() as session:
            user = await _upsert(session, tg_id)
            callback = _callback(tg_id)
            if handler == "cancel":
                data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id)
                await cancel_order(callback, data, session, user, AsyncMock())
            else:
                await pay_order_button(
                    callback, PayOrderCallback(order_id=order_id), session, user, AsyncMock()
                )
            await session.commit()
            return callback

    kind, callback = await asyncio.wait_for(
        asyncio.gather(_refund(sessions, order_id), update()), 30
    )

    assert kind is K.REFUNDED
    # Апдейт дождался возврата: отказ (заказ уже отменён), без второго списания.
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    assert await _status(sessions, order_id) is S.CANCELLED
    assert await _state(sessions, user_id) == REFUNDED


async def test_refund_vs_topup_keeps_ledger(sessions: Sessionmaker) -> None:
    order_id, user_id, tg_id = await _failed(sessions, 906)
    async with sessions() as session:
        payment = await create_topup(session, user_id, PACKAGE)
    notification = PaymentNotification(
        provider_payment_id=f"charge-{payment.id}",
        status=PaymentStatus.SUCCEEDED,
        amount=payment.amount,
        currency=payment.currency,
        invoice_payload=topup_payload(payment.id),
        payer_tg_id=tg_id,
    )
    barrier = asyncio.Barrier(2)

    async def topup() -> TopupOutcomeKind:
        await asyncio.wait_for(barrier.wait(), 10)
        async with sessions() as session:
            return (await confirm_topup(session, notification)).kind

    async def refund() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _refund(sessions, order_id)

    topup_kind, refund_kind = await asyncio.wait_for(asyncio.gather(topup(), refund()), 30)

    assert (topup_kind, refund_kind) == (TopupOutcomeKind.CREDITED, K.REFUNDED)
    balance, ledger, reasons = await _state(sessions, user_id)
    assert balance == ledger == PRICE + PACKAGE.credits
    assert sorted(reasons) == ["bonus", "order_charge", "order_refund", "topup"]
    async with sessions() as session:
        stored = await session.get(Payment, payment.id)
        assert stored is not None and stored.status is PaymentStatus.SUCCEEDED


async def test_unique_constraint_blocks_second_refund(sessions: Sessionmaker) -> None:
    order_id, user_id, _ = await _failed(sessions, 907)
    assert await _refund(sessions, order_id) is K.REFUNDED

    async with sessions() as session:
        session.add(
            BalanceTransaction(
                user_id=user_id,
                amount=PRICE,
                reason=TransactionReason.ORDER_REFUND,
                order_id=order_id,
            )
        )
        with pytest.raises(IntegrityError, match="uq_balance_transactions_order_id_reason"):
            await session.commit()
        await session.rollback()

    assert await _state(sessions, user_id) == REFUNDED
