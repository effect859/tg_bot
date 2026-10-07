"""Гонки биллинга между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировки строк заказа, платежа и пользователя действуют так же, как между двумя
обработчиками или двумя экземплярами бота.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.types import SuccessfulPayment
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_report_completion_concurrency as completion_races
from app.bot import texts
from app.bot.handlers.menu import successful_payment
from app.bot.handlers.order_intake import cancel_order
from app.bot.handlers.plan import pay_order_button
from app.bot.keyboards import OrderAction, OrderCallback, PayOrderCallback
from app.db.models import BalanceTransaction, Order, Payment, User
from app.domain.billing import CREDIT_PACKAGES, order_price
from app.domain.enums import DocType, OrderStatus, PaymentStatus, TransactionReason
from app.domain.errors import (
    ActiveOrderExistsError,
    InvalidStatusTransitionError,
    OrderDomainError,
)
from app.services import billing as billing_service
from app.services import orders
from app.services.billing import (
    ChargeOutcomeKind,
    TopupOutcomeKind,
    confirm_topup,
    create_topup,
    ledger_balance,
    pay_order,
    topup_payload,
)
from app.services.order_materials import finish_materials
from app.services.payments.base import PaymentNotification
from app.services.plan_confirmation import confirm_plan
from app.services.plan_generation import restart_plan_generation
from app.services.report_generation import find_orders_for_generation
from app.services.requirements_analysis import restart_analysis
from app.services.requirements_dialog import confirm_requirements, start_edit
from app.services.users import register_or_update_user
from billing_samples import grant_credits
from conftest import COMMITTED_TG_ID_BASE
from report_samples import payment_pending_order

S = OrderStatus
K = ChargeOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]
PRICE = order_price(DocType.PRACTICE_REPORT)
PACKAGE = CREDIT_PACKAGES[0]

# Та же фикстура, что у гонок завершения: удаляет разделы, файлы и версии тестовых
# заказов; журнал и платежи удаляет committed_sessionmaker (conftest).
sessions = completion_races.sessions


async def _pending(sessions: Sessionmaker, offset: int) -> tuple[int, int]:
    """(order_id, user_id) заказа в PAYMENT_PENDING (зафиксировано)."""
    async with sessions() as session:
        order_id = await payment_pending_order(session, tg_id=COMMITTED_TG_ID_BASE + offset)
        user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
        assert user_id is not None
        return order_id, user_id


async def _grant(sessions: Sessionmaker, user_id: int, credits: int) -> None:
    async with sessions() as session:
        await grant_credits(session, user_id, credits)
        await session.commit()


async def _pay(sessions: Sessionmaker, user_id: int, order_id: int) -> K:
    async with sessions() as session:
        return (await pay_order(session, user_id, order_id)).kind


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


async def _invoice(sessions: Sessionmaker, user_id: int) -> Payment:
    async with sessions() as session:
        return await create_topup(session, user_id, PACKAGE)


def _notification(payment: Payment, tg_id: int, charge: str = "charge-1") -> PaymentNotification:
    return PaymentNotification(
        provider_payment_id=f"{charge}-{payment.id}",
        status=PaymentStatus.SUCCEEDED,
        amount=payment.amount,
        currency=payment.currency,
        invoice_payload=topup_payload(payment.id),
        payer_tg_id=tg_id,
    )


async def _confirm(sessions: Sessionmaker, notification: PaymentNotification) -> TopupOutcomeKind:
    async with sessions() as session:
        return (await confirm_topup(session, notification)).kind


# --- Пополнение -----------------------------------------------------------------------


async def test_concurrent_successful_payments_credit_once(sessions: Sessionmaker) -> None:
    """Апдейт и сверка (или повтор апдейта) одновременно: одно зачисление."""
    _, user_id = await _pending(sessions, 801)
    payment = await _invoice(sessions, user_id)
    notification = _notification(payment, COMMITTED_TG_ID_BASE + 801)
    barrier = asyncio.Barrier(3)

    async def run() -> TopupOutcomeKind:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _confirm(sessions, notification)

    kinds = await asyncio.gather(run(), run(), run())

    assert sorted(kinds) == sorted(
        [
            TopupOutcomeKind.CREDITED,
            TopupOutcomeKind.ALREADY_CREDITED,
            TopupOutcomeKind.ALREADY_CREDITED,
        ]
    )
    assert await _state(sessions, user_id) == (PACKAGE.credits, PACKAGE.credits, ["topup"])


async def test_confirmation_waits_for_payment_lock(sessions: Sessionmaker) -> None:
    _, user_id = await _pending(sessions, 802)
    payment = await _invoice(sessions, user_id)
    notification = _notification(payment, COMMITTED_TG_ID_BASE + 802)

    async with sessions() as holder:
        await holder.execute(select(Payment).where(Payment.id == payment.id).with_for_update())
        task = asyncio.create_task(_confirm(sessions, notification))
        await asyncio.sleep(0.5)
        assert not task.done()  # ждёт блокировку строки платежа
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is TopupOutcomeKind.CREDITED


async def test_unique_payment_id_blocks_second_topup(sessions: Sessionmaker) -> None:
    _, user_id = await _pending(sessions, 803)
    payment = await _invoice(sessions, user_id)
    await _confirm(sessions, _notification(payment, COMMITTED_TG_ID_BASE + 803))

    async with sessions() as session:
        session.add(
            BalanceTransaction(
                user_id=user_id,
                amount=payment.credits,
                reason=TransactionReason.TOPUP,
                payment_id=payment.id,
            )
        )
        with pytest.raises(IntegrityError, match="uq_balance_transactions_payment_id"):
            await session.commit()
        await session.rollback()

    assert await _state(sessions, user_id) == (PACKAGE.credits, PACKAGE.credits, ["topup"])


# --- Оплата заказа --------------------------------------------------------------------


async def test_concurrent_pay_order_charges_once(sessions: Sessionmaker) -> None:
    order_id, user_id = await _pending(sessions, 811)
    await _grant(sessions, user_id, PRICE * 3)
    barrier = asyncio.Barrier(3)

    async def run() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _pay(sessions, user_id, order_id)

    kinds = await asyncio.gather(run(), run(), run())

    assert sorted(kinds) == sorted([K.PAID, K.ALREADY_PAID, K.ALREADY_PAID])
    assert await _state(sessions, user_id) == (PRICE * 2, PRICE * 2, ["bonus", "order_charge"])
    assert await _status(sessions, order_id) is S.GENERATING
    # Генерация одна: заказ один раз в выборке прохода.
    async with sessions() as session:
        found = await find_orders_for_generation(session, limit=10)
    assert found.count(order_id) == 1


async def test_pay_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id, user_id = await _pending(sessions, 812)
    await _grant(sessions, user_id, PRICE)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        task = asyncio.create_task(_pay(sessions, user_id, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.PAID


async def test_pay_sees_topup_committed_while_waiting(sessions: Sessionmaker) -> None:
    """Пополнение держит строку пользователя: оплата ждёт и видит новый баланс."""
    order_id, user_id = await _pending(sessions, 813)

    async with sessions() as holder:
        await holder.execute(select(User).where(User.id == user_id).with_for_update())
        task = asyncio.create_task(_pay(sessions, user_id, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # заказ взят, ждёт блокировку пользователя
        await grant_credits(holder, user_id, PRICE)
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.PAID
    assert await _state(sessions, user_id) == (0, 0, ["bonus", "order_charge"])


async def test_insufficient_after_balance_drop_while_waiting(sessions: Sessionmaker) -> None:
    """Баланс уменьшился, пока оплата ждала пользователя: INSUFFICIENT, без минуса."""
    order_id, user_id = await _pending(sessions, 814)
    await _grant(sessions, user_id, PRICE)

    async with sessions() as holder:
        await holder.execute(select(User).where(User.id == user_id).with_for_update())
        task = asyncio.create_task(_pay(sessions, user_id, order_id))
        await asyncio.sleep(0.5)
        holder.add(
            BalanceTransaction(user_id=user_id, amount=-1, reason=TransactionReason.ADMIN_ADJUST)
        )
        user = await holder.get(User, user_id)
        assert user is not None
        user.balance -= 1
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.INSUFFICIENT
    assert await _status(sessions, order_id) is S.PAYMENT_PENDING
    assert await _state(sessions, user_id) == (PRICE - 1, PRICE - 1, ["bonus", "admin_adjust"])


async def test_topup_and_pay_concurrently_keep_ledger(sessions: Sessionmaker) -> None:
    order_id, user_id = await _pending(sessions, 815)
    await _grant(sessions, user_id, PRICE)
    payment = await _invoice(sessions, user_id)
    barrier = asyncio.Barrier(2)

    async def topup() -> TopupOutcomeKind:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _confirm(sessions, _notification(payment, COMMITTED_TG_ID_BASE + 815))

    async def pay() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _pay(sessions, user_id, order_id)

    topup_kind, pay_kind = await asyncio.gather(topup(), pay())

    assert (topup_kind, pay_kind) == (TopupOutcomeKind.CREDITED, K.PAID)
    balance, ledger, reasons = await _state(sessions, user_id)
    assert balance == ledger == PACKAGE.credits
    assert sorted(reasons) == ["bonus", "order_charge", "topup"]


async def test_pay_and_cancel_race_has_single_winner(sessions: Sessionmaker) -> None:
    order_id, user_id = await _pending(sessions, 816)
    await _grant(sessions, user_id, PRICE)
    barrier = asyncio.Barrier(2)

    async def cancel() -> str:
        await asyncio.wait_for(barrier.wait(), 10)
        async with sessions() as session:
            try:
                await orders.change_status(session, order_id, S.CANCELLED)
                await session.commit()
            except InvalidStatusTransitionError:
                return "refused"
        return "cancelled"

    async def pay() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _pay(sessions, user_id, order_id)

    cancelled, paid = await asyncio.gather(cancel(), pay())

    balance, ledger, reasons = await _state(sessions, user_id)
    assert balance == ledger
    if paid is K.PAID:
        assert cancelled == "refused"
        assert await _status(sessions, order_id) is S.GENERATING
        assert reasons == ["bonus", "order_charge"]
    else:
        assert (cancelled, paid) == ("cancelled", K.NOT_PAYABLE)
        assert await _status(sessions, order_id) is S.CANCELLED
        assert (balance, reasons) == (PRICE, ["bonus"])


async def test_one_balance_cannot_pay_two_orders(sessions: Sessionmaker) -> None:
    """Второй активный заказ невозможен (uq_orders_user_id_active): баланс не делится."""
    _, user_id = await _pending(sessions, 817)

    async with sessions() as session:
        with pytest.raises(ActiveOrderExistsError):
            await orders.create_order(session, user_id=user_id)
        await session.rollback()


async def test_unique_order_charge_blocks_second_charge(sessions: Sessionmaker) -> None:
    order_id, user_id = await _pending(sessions, 818)
    await _grant(sessions, user_id, PRICE * 2)
    assert await _pay(sessions, user_id, order_id) is K.PAID

    async with sessions() as session:
        session.add(
            BalanceTransaction(
                user_id=user_id,
                amount=-PRICE,
                reason=TransactionReason.ORDER_CHARGE,
                order_id=order_id,
            )
        )
        with pytest.raises(IntegrityError, match="uq_balance_transactions_order_id_reason"):
            await session.commit()
        await session.rollback()

    assert await _state(sessions, user_id) == (PRICE, PRICE, ["bonus", "order_charge"])


# --- Хендлеры и UserMiddleware --------------------------------------------------------


async def test_successful_payment_handler_and_reconciliation_do_not_deadlock(
    sessions: Sessionmaker,
) -> None:
    """UserMiddleware блокирует строку пользователя (upsert) до конца апдейта.

    Хендлер фиксирует её до подтверждения: сверка, держащая платёж, получает
    пользователя без ожидания, порядок блокировок един — платёж → пользователь.
    """
    _, user_id = await _pending(sessions, 821)
    tg_id = COMMITTED_TG_ID_BASE + 821
    payment = await _invoice(sessions, user_id)
    message = AsyncMock()
    message.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    message.successful_payment = SuccessfulPayment(
        currency="XTR",
        total_amount=payment.amount,
        invoice_payload=topup_payload(payment.id),
        telegram_payment_charge_id="charge-821",
        provider_payment_charge_id="",
    )

    async with sessions() as reconciliation:
        await reconciliation.execute(
            select(Payment).where(Payment.id == payment.id).with_for_update()
        )
        async with sessions() as update_session:
            # Как UserMiddleware: upsert пользователя в транзакции апдейта.
            user = await register_or_update_user(
                update_session, tg_id=tg_id, username=None, first_name=None, language_code=None
            )
            task = asyncio.create_task(
                successful_payment(message, update_session, user, AsyncMock())
            )
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт блокировку платежа
            # Сверка берёт пользователя: хендлер его уже не держит.
            await asyncio.wait_for(
                reconciliation.execute(select(User).where(User.id == user_id).with_for_update()),
                5,
            )
            await reconciliation.commit()
            await asyncio.wait_for(task, 10)

    assert await _state(sessions, user_id) == (PACKAGE.credits, PACKAGE.credits, ["topup"])


def _update_callback(tg_id: int) -> AsyncMock:
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


async def _cancel_handler(session: AsyncSession, user: User, order_id: int) -> str:
    """Хендлер «Отменить» целиком: текст alert или «ok»."""
    callback = _update_callback(user.tg_id)
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id)
    await cancel_order(callback, data, session, user, AsyncMock())
    if callback.answer.await_args.kwargs.get("show_alert"):
        return str(callback.answer.await_args.args[0])
    return "ok"


async def _service_action(name: str, session: AsyncSession, order_id: int) -> str:
    """Сервисы, которые вызывают хендлеры заказа: блокируют заказ до проверки статуса."""
    try:
        match name:
            case "confirm_plan":  # устаревшая кнопка «Подтвердить план»
                await confirm_plan(session, order_id, "stale-digest")
            case "restart_plan":  # «Повторить составление плана»
                await restart_plan_generation(session, order_id)
            case "requirements_edit":  # «Изменить» ТЗ
                await start_edit(session, order_id, "topic")
            case "requirements_confirm":  # «Подтвердить ТЗ»
                await confirm_requirements(session, order_id, "stale-digest")
            case "restart_analysis":  # «Повторить анализ»
                await restart_analysis(session, order_id)
            case "finish_materials":  # «Материалы загружены»
                await finish_materials(session, order_id)
    except OrderDomainError:
        await session.rollback()
        return "refused"
    await session.commit()
    return "done"


async def _pay_update(sessions: Sessionmaker, tg_id: int, order_id: int) -> AsyncMock:
    """Апдейт «Оплатить» как в боте: UserMiddleware, хендлер, commit DbSessionMiddleware."""
    async with sessions() as session:
        user = await _upsert(session, tg_id)
        callback = _update_callback(tg_id)
        data = PayOrderCallback(order_id=order_id)
        await pay_order_button(callback, data, session, user, AsyncMock())
        await session.commit()
        return callback


ORDER_HANDLERS = (
    "cancel",
    "confirm_plan",
    "restart_plan",
    "requirements_edit",
    "requirements_confirm",
    "restart_analysis",
    "finish_materials",
)


@pytest.mark.parametrize("handler", ORDER_HANDLERS)
async def test_pay_vs_order_handler_no_deadlock(
    sessions: Sessionmaker, monkeypatch: pytest.MonkeyPatch, handler: str
) -> None:
    """F1: «Оплатить» и другой апдейт того же пользователя по тому же заказу.

    Оплата задержана сразу после блокировки заказа. При прежнем порядке (заказ →
    пользователь) второй апдейт в этот момент брал пользователя (UserMiddleware) и ждал
    заказ, а оплата ждала пользователя — взаимная блокировка. Теперь второй апдейт ждёт
    пользователя, пока оплата не завершится.
    """
    offset = 830 + ORDER_HANDLERS.index(handler)
    order_id, user_id = await _pending(sessions, offset)
    tg_id = COMMITTED_TG_ID_BASE + offset
    await _grant(sessions, user_id, PRICE)
    order_locked = asyncio.Event()
    real_lock_order = billing_service.lock_order

    async def lock_and_wait(session: AsyncSession, locked_order_id: int) -> Order:
        order = await real_lock_order(session, locked_order_id)
        order_locked.set()
        await asyncio.sleep(0.5)  # окно для второго апдейта
        return order

    monkeypatch.setattr(billing_service, "lock_order", lock_and_wait)

    async def other_update() -> str:
        await asyncio.wait_for(order_locked.wait(), 10)
        async with sessions() as session:
            user = await _upsert(session, tg_id)
            if handler == "cancel":
                result = await _cancel_handler(session, user, order_id)
                await session.commit()
                return result
            return await _service_action(handler, session, order_id)

    pay_callback, other = await asyncio.wait_for(
        asyncio.gather(_pay_update(sessions, tg_id, order_id), other_update()), 30
    )

    # Оплата прошла; второй апдейт дождался её и получил отказ по статусу.
    pay_callback.answer.assert_awaited_once_with()
    assert other not in ("done", "ok")
    assert await _status(sessions, order_id) is S.GENERATING
    assert await _state(sessions, user_id) == (0, 0, ["bonus", "order_charge"])


async def test_cancel_first_then_pay_is_not_payable(sessions: Sessionmaker) -> None:
    """Отмена взяла пользователя и заказ раньше: оплата ждёт и ничего не списывает."""
    order_id, user_id = await _pending(sessions, 840)
    tg_id = COMMITTED_TG_ID_BASE + 840
    await _grant(sessions, user_id, PRICE)
    cancelled = asyncio.Event()

    async def cancel_update() -> str:
        async with sessions() as session:
            user = await _upsert(session, tg_id)
            result = await _cancel_handler(session, user, order_id)
            cancelled.set()
            await asyncio.sleep(0.5)  # транзакция апдейта ещё открыта
            await session.commit()
            return result

    async def pay_after_cancel() -> AsyncMock:
        await asyncio.wait_for(cancelled.wait(), 10)
        return await _pay_update(sessions, tg_id, order_id)

    cancel_result, pay_callback = await asyncio.wait_for(
        asyncio.gather(cancel_update(), pay_after_cancel()), 30
    )

    assert cancel_result == "ok"
    assert pay_callback.answer.await_args.args[0] == texts.ORDER_NOT_PAYABLE
    assert await _status(sessions, order_id) is S.CANCELLED
    # У отменённого заказа нет ORDER_CHARGE.
    assert await _state(sessions, user_id) == (PRICE, PRICE, ["bonus"])


async def test_pay_vs_cancel_updates_race_repeatedly(sessions: Sessionmaker) -> None:
    """Апдейты «Оплатить» и «Отменить» одновременно, без пауз, пять раундов."""
    for round_ in range(5):
        offset = 850 + round_
        order_id, user_id = await _pending(sessions, offset)
        tg_id = COMMITTED_TG_ID_BASE + offset
        await _grant(sessions, user_id, PRICE)
        barrier = asyncio.Barrier(2)

        async def pay(
            tg_id: int = tg_id, order_id: int = order_id, barrier: asyncio.Barrier = barrier
        ) -> AsyncMock:
            await asyncio.wait_for(barrier.wait(), 10)
            return await _pay_update(sessions, tg_id, order_id)

        async def cancel(
            tg_id: int = tg_id, order_id: int = order_id, barrier: asyncio.Barrier = barrier
        ) -> str:
            await asyncio.wait_for(barrier.wait(), 10)
            async with sessions() as session:
                user = await _upsert(session, tg_id)
                result = await _cancel_handler(session, user, order_id)
                await session.commit()
                return result

        pay_callback, cancel_result = await asyncio.wait_for(asyncio.gather(pay(), cancel()), 30)

        balance, ledger, reasons = await _state(sessions, user_id)
        assert balance == ledger
        if await _status(sessions, order_id) is S.GENERATING:
            pay_callback.answer.assert_awaited_once_with()
            assert cancel_result != "ok"
            assert (balance, reasons) == (0, ["bonus", "order_charge"])
        else:
            assert await _status(sessions, order_id) is S.CANCELLED
            assert cancel_result == "ok"
            assert pay_callback.answer.await_args.args[0] == texts.ORDER_NOT_PAYABLE
            assert (balance, reasons) == (PRICE, ["bonus"])
