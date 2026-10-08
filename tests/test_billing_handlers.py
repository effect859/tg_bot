"""Бот-слой кредитов: «Баланс», счёт Stars, pre_checkout, successful_payment, «Оплатить».

Message, CallbackQuery, PreCheckoutQuery и Bot — моки или записывающая сессия Bot:
реальный Telegram Bot API не вызывается. БД — настоящая PostgreSQL.
"""

from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerPreCheckoutQuery, SendInvoice, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    StarTransaction,
    StarTransactions,
    SuccessfulPayment,
    TransactionPartnerFragment,
    TransactionPartnerUser,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import setup, texts
from app.bot.handlers import menu, plan
from app.bot.handlers.menu import buy_credits, pre_checkout, show_balance, successful_payment
from app.bot.handlers.menu import topup_menu as topup_menu_handler
from app.bot.handlers.plan import pay_order_button
from app.bot.keyboards import (
    BillingAction,
    BillingCallback,
    OrderCallback,
    PayOrderCallback,
)
from app.bot.reminders import active_order_reminder
from app.bot.setup import create_dispatcher
from app.bot.stars import fetch_star_transactions, send_star_invoice
from app.db.models import Order, Payment, User
from app.domain.billing import CREDIT_PACKAGES, order_price
from app.domain.enums import DocType, OrderStatus, PaymentStatus
from app.services import orders
from app.services.billing import create_topup, ledger_balance, topup_payload
from app.services.payment_reconciliation import (
    RECONCILIATION_MIN_AGE,
    reconcile_star_payments,
)
from app.services.payments.telegram_stars import StarTransactionRecord
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from billing_samples import grant_credits
from dialog_samples import fresh_order
from report_samples import payment_pending_order
from test_plan_handlers import RecordingSession
from test_report_delivery_handlers import _detach_routers

S = OrderStatus
TG_ID = 777_555_333
PACKAGE = CREDIT_PACKAGES[1]
PRICE = order_price(DocType.PRACTICE_REPORT)


async def _user(session: AsyncSession, tg_id: int = TG_ID) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


def _callback(tg_id: int = TG_ID) -> AsyncMock:
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.message.chat.id = tg_id
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


def _alert(callback: AsyncMock) -> str:
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    return str(callback.answer.await_args.args[0])


def _actions(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    actions = []
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("pay:"):
                actions.append(f"pay:{PayOrderCallback.unpack(data).order_id}")
            elif data.startswith("bill:"):
                parsed = BillingCallback.unpack(data)
                actions.append(f"{parsed.action.value}:{parsed.package}".rstrip(":"))
            else:
                actions.append(OrderCallback.unpack(data).action.value)
    return actions


def _payment_message(
    tg_id: int, payment: Payment, *, charge: str = "charge-1", amount: int | None = None
) -> AsyncMock:
    message = AsyncMock()
    message.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    message.successful_payment = SuccessfulPayment(
        currency="XTR",
        total_amount=payment.amount if amount is None else amount,
        invoice_payload=topup_payload(payment.id),
        telegram_payment_charge_id=charge,
        provider_payment_charge_id="",
    )
    return message


def _pre_checkout(
    tg_id: int, payload: str, amount: int = PACKAGE.stars, currency: str = "XTR"
) -> AsyncMock:
    query = AsyncMock()
    query.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    query.invoice_payload = payload
    query.total_amount = amount
    query.currency = currency
    return query


async def _invoice(session: AsyncSession, user_id: int) -> Payment:
    payment = await create_topup(session, user_id, PACKAGE)
    session.expunge(payment)
    return payment


async def _payment(session: AsyncSession, payment_id: int) -> Payment:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert payment is not None
    return payment


async def _payments(session: AsyncSession, user_id: int) -> list[Payment]:
    stmt = select(Payment).where(Payment.user_id == user_id).order_by(Payment.id)
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _order_user_id(session: AsyncSession, order_id: int) -> int:
    user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
    assert user_id is not None
    return user_id


# --- «Баланс» и счёт ------------------------------------------------------------------


async def test_balance_shows_credits_and_packages(session: AsyncSession) -> None:
    user = await _user(session)
    message = AsyncMock()

    await show_balance(message, user)

    assert message.answer.await_args.args[0] == texts.balance_with_packages(0)
    assert _actions(message.answer.await_args.kwargs["reply_markup"]) == [
        f"buy:{package.id}" for package in CREDIT_PACKAGES
    ]


async def test_topup_menu_from_payment_screen(session: AsyncSession) -> None:
    user = await _user(session)
    await grant_credits(session, user.id, 7)
    callback, bot = _callback(), AsyncMock()

    await topup_menu_handler(callback, session, user, bot)

    callback.answer.assert_awaited_once_with()
    chat_id, text = bot.send_message.await_args.args
    assert (chat_id, text) == (TG_ID, texts.balance_with_packages(7))


async def test_buy_credits_creates_invoice_and_sends_it(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    callback, bot = _callback(), AsyncMock()

    await buy_credits(
        callback,
        BillingCallback(action=BillingAction.BUY, package=PACKAGE.id),
        session,
        user,
        bot,
    )

    callback.answer.assert_awaited_once_with()
    [payment] = await _payments(session, user_id)
    assert (payment.status, payment.amount, payment.credits) == (
        PaymentStatus.PENDING,
        PACKAGE.stars,
        PACKAGE.credits,
    )
    kwargs = bot.send_invoice.await_args.kwargs
    assert kwargs["chat_id"] == TG_ID
    assert kwargs["payload"] == topup_payload(payment.id)
    assert kwargs["currency"] == "XTR"
    assert kwargs["prices"] == [
        LabeledPrice(label=texts.invoice_title(PACKAGE), amount=PACKAGE.stars)
    ]
    assert "provider_token" not in kwargs  # Stars: токен провайдера не нужен
    assert await ledger_balance(session, user_id) == 0  # кредиты — только после оплаты


async def test_buy_unknown_package(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    callback, bot = _callback(), AsyncMock()

    await buy_credits(
        callback, BillingCallback(action=BillingAction.BUY, package="nope"), session, user, bot
    )

    assert _alert(callback) == texts.PACKAGE_UNKNOWN
    bot.send_invoice.assert_not_awaited()
    assert await _payments(session, user_id) == []


async def test_invoice_sending_failure_leaves_harmless_pending(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    callback, bot = _callback(), AsyncMock()
    bot.send_invoice.side_effect = TelegramBadRequest(
        SendInvoice(
            chat_id=1,
            title="t",
            description="d",
            payload="p",
            currency="XTR",
            prices=[LabeledPrice(label="t", amount=1)],
        ),
        "bad",
    )

    await buy_credits(
        callback,
        BillingCallback(action=BillingAction.BUY, package=PACKAGE.id),
        session,
        user,
        bot,
    )

    assert _alert(callback) == texts.TOPUP_FAILED
    [payment] = await _payments(session, user_id)
    assert payment.status is PaymentStatus.PENDING
    assert await ledger_balance(session, user_id) == 0


async def test_buy_credits_database_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    callback, bot = _callback(), AsyncMock()

    async def broken(*args: object) -> Payment:
        raise OperationalError("INSERT secret", {}, Exception("down"))

    monkeypatch.setattr(menu, "create_topup", broken)

    await buy_credits(
        callback,
        BillingCallback(action=BillingAction.BUY, package=PACKAGE.id),
        session,
        user,
        bot,
    )

    assert _alert(callback) == texts.TOPUP_FAILED
    bot.send_invoice.assert_not_awaited()
    assert "error=OperationalError" in caplog.text and "secret" not in caplog.text


# --- pre_checkout ---------------------------------------------------------------------


async def test_pre_checkout_ok(session: AsyncSession) -> None:
    user = await _user(session)
    payment = await _invoice(session, user.id)
    query = _pre_checkout(TG_ID, topup_payload(payment.id))

    await pre_checkout(query, session)

    query.answer.assert_awaited_once_with(ok=True)


@pytest.mark.parametrize(
    ("tg_id", "amount", "code"),
    [
        (TG_ID + 1, PACKAGE.stars, "foreign_invoice"),
        (TG_ID, PACKAGE.stars + 1, "amount_mismatch"),
    ],
)
async def test_pre_checkout_refused(
    session: AsyncSession, tg_id: int, amount: int, code: str
) -> None:
    user = await _user(session)
    payment = await _invoice(session, user.id)
    query = _pre_checkout(tg_id, topup_payload(payment.id), amount=amount)

    await pre_checkout(query, session)

    query.answer.assert_awaited_once_with(ok=False, error_message=texts.pre_checkout_refusal(code))


async def test_pre_checkout_database_error_refuses(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(menu, "check_pre_checkout", broken)
    query = _pre_checkout(TG_ID, "topup:1")

    await pre_checkout(query, session)

    query.answer.assert_awaited_once_with(
        ok=False, error_message=texts.PAYMENT_TEMPORARILY_UNAVAILABLE
    )


# --- successful_payment ---------------------------------------------------------------


async def test_successful_payment_credits_and_reports(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    payment = await _invoice(session, user_id)
    message = _payment_message(TG_ID, payment)

    await successful_payment(message, session, user, AsyncMock())

    message.answer.assert_awaited_once_with(texts.topup_credited(PACKAGE.credits, PACKAGE.credits))
    stored = await _payment(session, payment.id)
    # Идентификатор Telegram сохранён для возврата (refundStarPayment).
    assert (stored.status, stored.provider_payment_id) == (PaymentStatus.SUCCEEDED, "charge-1")
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_duplicate_successful_payment_update(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    payment = await _invoice(session, user_id)
    await successful_payment(
        _payment_message(TG_ID, payment), session, await _user(session), AsyncMock()
    )
    again = _payment_message(TG_ID, payment)

    await successful_payment(again, session, await _user(session), AsyncMock())

    again.answer.assert_not_awaited()
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_successful_payment_offers_waiting_order(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user = await _user(session)
    payment = await _invoice(session, user.id)
    message = _payment_message(TG_ID, payment)

    await successful_payment(message, session, user, AsyncMock())

    first, second = message.answer.await_args_list
    assert first.args[0] == texts.topup_credited(PACKAGE.credits, PACKAGE.credits)
    assert second.args[0] == texts.payment_pending(order_id, PRICE, PACKAGE.credits)
    assert _actions(second.kwargs["reply_markup"]) == [f"pay:{order_id}", "cancel"]
    # Кредиты сами не списываются (OD-5).
    assert (await fresh_order(session, order_id)).status is S.PAYMENT_PENDING


async def test_rejected_payment_shows_charge_id(session: AsyncSession) -> None:
    user = await _user(session)
    user_id = user.id
    payment = await _invoice(session, user_id)
    message = _payment_message(TG_ID, payment, amount=PACKAGE.stars - 1)

    await successful_payment(message, session, user, AsyncMock())

    message.answer.assert_awaited_once_with(texts.payment_rejected("charge-1"))
    assert await ledger_balance(session, user_id) == 0


async def test_lost_confirmation_recovered_by_reconciliation(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OD-6: БД недоступна при successful_payment — сверка зачисляет позже, один раз."""
    user = await _user(session)
    user_id = user.id
    payment = await _invoice(session, user_id)
    real_confirm = menu.confirm_topup

    async def broken(*args: object) -> object:
        raise OperationalError("UPDATE", {}, Exception("down"))

    monkeypatch.setattr(menu, "confirm_topup", broken)
    message = _payment_message(TG_ID, payment)
    await successful_payment(message, session, user, AsyncMock())
    message.answer.assert_awaited_once_with(texts.PAYMENT_PROCESSING_DELAYED)
    assert (await _payment(session, payment.id)).status is PaymentStatus.PENDING
    monkeypatch.setattr(menu, "confirm_topup", real_confirm)

    created = (await _payment(session, payment.id)).created_at

    async def fetch(offset: int, limit: int) -> list[StarTransactionRecord]:
        record = StarTransactionRecord(
            id="charge-1",
            amount=PACKAGE.stars,
            date=created,
            transaction_type="invoice_payment",
            invoice_payload=topup_payload(payment.id),
            payer_tg_id=TG_ID,
        )
        return [record][offset : offset + limit]

    await reconcile_star_payments(sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE)
    await reconcile_star_payments(sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE)

    stored = await _payment(session, payment.id)
    assert (stored.status, stored.provider_payment_id) == (PaymentStatus.SUCCEEDED, "charge-1")
    assert await ledger_balance(session, user_id) == PACKAGE.credits


# --- Оплата заказа --------------------------------------------------------------------


async def test_payment_screen_offers_pay_when_enough_credits(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    await grant_credits(session, await _order_user_id(session, order_id), PRICE)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text == texts.payment_pending(order_id, PRICE, PRICE)
    assert _actions(markup) == [f"pay:{order_id}", "cancel"]


async def test_pay_button_charges_and_starts_generation(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _order_user_id(session, order_id)
    await grant_credits(session, user_id, PRICE + 5)
    await session.commit()
    callback, bot = _callback(), AsyncMock()

    await pay_order_button(
        callback, PayOrderCallback(order_id=order_id), session, await _user(session), bot
    )

    callback.answer.assert_awaited_once_with()
    assert bot.send_message.await_args.args[1] == texts.order_paid(order_id, PRICE, 5)
    assert (await fresh_order(session, order_id)).status is S.GENERATING
    assert await ledger_balance(session, user_id) == 5


async def test_pay_button_insufficient_credits(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    callback, bot = _callback(), AsyncMock()

    await pay_order_button(
        callback, PayOrderCallback(order_id=order_id), session, await _user(session), bot
    )

    assert _alert(callback) == texts.insufficient_credits(PRICE, 0)
    bot.send_message.assert_not_awaited()
    assert (await fresh_order(session, order_id)).status is S.PAYMENT_PENDING


async def test_pay_button_twice(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _order_user_id(session, order_id)
    await grant_credits(session, user_id, PRICE * 2)
    await session.commit()
    await pay_order_button(
        _callback(), PayOrderCallback(order_id=order_id), session, await _user(session), AsyncMock()
    )
    callback = _callback()

    await pay_order_button(
        callback, PayOrderCallback(order_id=order_id), session, await _user(session), AsyncMock()
    )

    assert _alert(callback) == texts.ORDER_ALREADY_PAID
    assert await ledger_balance(session, user_id) == PRICE


async def test_pay_button_foreign_or_missing_order(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)

    for target in (order_id, order_id + 10_000):
        stranger = await _user(session, tg_id=TG_ID + 1)
        callback = _callback(TG_ID + 1)
        await pay_order_button(
            callback, PayOrderCallback(order_id=target), session, stranger, AsyncMock()
        )
        assert _alert(callback) == texts.ORDER_NOT_FOUND


async def test_pay_button_cancelled_order(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    await orders.change_status(session, order_id, S.CANCELLED)
    await session.commit()
    callback = _callback()

    await pay_order_button(
        callback, PayOrderCallback(order_id=order_id), session, await _user(session), AsyncMock()
    )

    assert _alert(callback) == texts.ORDER_NOT_PAYABLE


async def test_pay_button_database_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)

    async def broken(*args: object) -> object:
        raise OperationalError("UPDATE users SET balance secret", {}, Exception("down"))

    monkeypatch.setattr(plan, "pay_order", broken)
    callback = _callback()

    await pay_order_button(
        callback, PayOrderCallback(order_id=order_id), session, await _user(session), AsyncMock()
    )

    assert _alert(callback) == texts.PAYMENT_ACTION_FAILED
    assert "error=OperationalError" in caplog.text and "secret" not in caplog.text
    assert (await fresh_order(session, order_id)).status is S.PAYMENT_PENDING


# --- Маршрутизация (настоящий Dispatcher) ---------------------------------------------


def _dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Any:
    _detach_routers(monkeypatch)
    return create_dispatcher(sessionmaker, MemoryStorage(), LocalFileStorage(tmp_path))


async def test_stars_updates_reach_handlers(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _user(session)
    user_id = user.id
    payment = await _invoice(session, user_id)
    dispatcher = _dispatcher(sessionmaker, tmp_path, monkeypatch)
    bot = Bot(token="42:TEST", session=RecordingSession())
    payer = TgUser(id=TG_ID, is_bot=False, first_name="U")
    query = PreCheckoutQuery(
        id="q",
        from_user=payer,
        currency="XTR",
        total_amount=PACKAGE.stars,
        invoice_payload=topup_payload(payment.id),
    )
    paid = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=TG_ID, type="private"),
        from_user=payer,
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=PACKAGE.stars,
            invoice_payload=topup_payload(payment.id),
            telegram_payment_charge_id="charge-77",
            provider_payment_charge_id="",
        ),
    )

    await dispatcher.feed_update(bot, Update(update_id=1, pre_checkout_query=query))
    await dispatcher.feed_update(bot, Update(update_id=2, message=paid))

    recorded = bot.session
    assert isinstance(recorded, RecordingSession)
    [answer] = [r for r in recorded.requests if isinstance(r, AnswerPreCheckoutQuery)]
    assert answer.ok is True
    sent = [r.text for r in recorded.requests if isinstance(r, SendMessage)]
    assert sent == [texts.topup_credited(PACKAGE.credits, PACKAGE.credits)]
    assert await ledger_balance(session, user_id) == PACKAGE.credits
    assert (await _payment(session, payment.id)).provider_payment_id == "charge-77"
    # Методы Stars нужны боту: позволяют получать pre_checkout_query.
    assert "pre_checkout_query" in dispatcher.resolve_used_update_types()


async def test_pay_callback_reaches_plan_handler(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    await grant_credits(session, await _order_user_id(session, order_id), PRICE)
    await session.commit()
    dispatcher = _dispatcher(sessionmaker, tmp_path, monkeypatch)
    bot = Bot(token="42:TEST", session=RecordingSession())
    update = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb",
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=PayOrderCallback(order_id=order_id).pack(),
        ),
    )

    await dispatcher.feed_update(bot, update)

    recorded = bot.session
    assert isinstance(recorded, RecordingSession)
    sent = [r.text for r in recorded.requests if isinstance(r, SendMessage)]
    assert sent == [texts.order_paid(order_id, PRICE, 0)]
    assert (await fresh_order(session, order_id)).status is S.GENERATING


async def test_reconciliation_registered_without_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[object, ...]] = []

    class Fake:
        def __init__(self, *args: object) -> None:
            created.append(args)

        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    for name in (
        "ReportCompletionRecovery",
        "ReportDeliveryRecovery",
        "RefundRecovery",
        "PaymentReconciliation",
    ):
        monkeypatch.setattr(setup, name, Fake)
    dispatcher = _dispatcher(sessionmaker, tmp_path, monkeypatch)
    bot = AsyncMock()

    for handler in dispatcher.startup.handlers:
        await handler.callback(bot)

    *_, (sessions, fetch) = created
    assert sessions is sessionmaker
    assert isinstance(fetch, partial)
    assert (fetch.func, fetch.args) == (fetch_star_transactions, (bot,))


# --- Адаптеры Stars (aiogram) ---------------------------------------------------------


async def test_send_star_invoice() -> None:
    bot = AsyncMock()

    await send_star_invoice(bot, 5, "topup:1", "500 кредитов", "описание", 500)

    bot.send_invoice.assert_awaited_once_with(
        chat_id=5,
        title="500 кредитов",
        description="описание",
        payload="topup:1",
        currency="XTR",
        prices=[LabeledPrice(label="500 кредитов", amount=500)],
    )


async def test_fetch_star_transactions_normalizes() -> None:
    date = datetime(2026, 10, 7, 12, tzinfo=UTC)
    bot = AsyncMock()
    bot.get_star_transactions.return_value = StarTransactions(
        transactions=[
            StarTransaction(
                id="charge-1",
                amount=500,
                date=date,
                source=TransactionPartnerUser(
                    transaction_type="invoice_payment",
                    user=TgUser(id=42, is_bot=False, first_name="U"),
                    invoice_payload="topup:1",
                ),
            ),
            StarTransaction(
                id="out-1", amount=100, date=date, receiver=TransactionPartnerFragment()
            ),
        ]
    )

    records = await fetch_star_transactions(bot, 10, 2)

    bot.get_star_transactions.assert_awaited_once_with(offset=10, limit=2)
    assert records == [
        StarTransactionRecord(
            id="charge-1",
            amount=500,
            date=date,
            transaction_type="invoice_payment",
            invoice_payload="topup:1",
            payer_tg_id=42,
        ),
        StarTransactionRecord(id="out-1", amount=100, date=date),
    ]
