"""Бот-слой возврата: напоминания, уведомление, регистрация прохода, старые кнопки.

Message, CallbackQuery и Bot — моки: реальный Telegram Bot API не вызывается. БД —
настоящая PostgreSQL; сбой генерации — настоящий (постоянная ошибка модели).
"""

from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import setup, texts
from app.bot.handlers.menu import my_orders
from app.bot.handlers.order_intake import cancel_order, new_report
from app.bot.keyboards import OrderAction, OrderCallback
from app.bot.reminders import active_order_reminder
from app.bot.setup import create_dispatcher, notify_refund_outcome
from app.db.models import Order, User
from app.domain.billing import order_price
from app.domain.enums import DocType, OrderStatus
from app.services.billing import (
    RefundOutcome,
    RefundOutcomeKind,
    ledger_balance,
    refund_failed_order,
)
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from dialog_samples import fresh_order
from report_samples import generating_order
from test_order_refunds import _generation_failed
from test_report_delivery_handlers import _detach_routers

S = OrderStatus
PRICE = order_price(DocType.PRACTICE_REPORT)
TG_ID = 777_555_444


async def _user(session: AsyncSession, tg_id: int = TG_ID) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


async def test_generating_reminder(session: AsyncSession) -> None:
    order_id = await generating_order(session, tg_id=TG_ID)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert (text, markup) == (texts.generation_in_progress(order_id), None)


async def test_failed_generation_reminder_promises_refund(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert (text, markup) == (texts.generation_failed(order_id), None)
    assert "вернутся на баланс" in text


async def test_notify_refund_outcome() -> None:
    bot = AsyncMock()
    outcome = RefundOutcome(RefundOutcomeKind.REFUNDED, 7, 500, balance=650, chat_id=42)

    await notify_refund_outcome(bot, 42, outcome)

    bot.send_message.assert_awaited_once_with(42, texts.order_refunded(7, 500, 650))
    assert "500 кредитов" in texts.order_refunded(7, 500, 650)


async def test_stale_cancel_button_after_generation_failure(session: AsyncSession) -> None:
    """Старая кнопка «Отменить»: без возврата отмены нет — обычный отказ, без исключения."""
    order_id = await _generation_failed(session)
    user = await _user(session)
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.from_user = TgUser(id=TG_ID, is_bot=False, first_name="U")

    await cancel_order(
        callback,
        OrderCallback(action=OrderAction.CANCEL, order_id=order_id),
        session,
        user,
        AsyncMock(),
    )

    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    assert callback.answer.await_args.args[0].startswith("Действие уже неактуально")
    await session.rollback()
    assert (await fresh_order(session, order_id)).status is S.FAILED


async def test_after_refund_new_report_starts_new_order(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    await refund_failed_order(session, order_id)
    user = await _user(session)
    user_id = user.id
    message = AsyncMock()

    await new_report(message, session, user)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == texts.CONSENT_REQUEST
    orders_count = len(
        list(await session.scalars(select(Order.id).where(Order.user_id == user_id)))
    )
    assert orders_count == 2
    assert await ledger_balance(session, user_id) == PRICE


async def test_my_orders_after_refund(session: AsyncSession) -> None:
    order_id = await _generation_failed(session)
    await refund_failed_order(session, order_id)
    message = AsyncMock()

    await my_orders(message, session, await _user(session))

    text = message.answer.await_args.args[0]
    assert f"#{order_id}" in text and "отменён" in text
    # Отменённый заказ не скачивается.
    assert message.answer.await_args.kwargs == {}


async def test_refund_recovery_registered_without_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _detach_routers(monkeypatch)
    created: list[tuple[str, tuple[object, ...]]] = []

    def fake(name: str) -> type:
        class Fake:
            def __init__(self, *args: object) -> None:
                created.append((name, args))

            def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

        return Fake

    for name in (
        "ReportCompletionRecovery",
        "ReportDeliveryRecovery",
        "RefundRecovery",
        "PaymentReconciliation",
    ):
        monkeypatch.setattr(setup, name, fake(name))
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), LocalFileStorage(tmp_path))
    bot = AsyncMock()

    for handler in dispatcher.startup.handlers:
        await handler.callback(bot)

    [(sessions, notify)] = [args for name, args in created if name == "RefundRecovery"]
    assert sessions is sessionmaker
    assert isinstance(notify, partial)
    assert (notify.func, notify.args) == (notify_refund_outcome, (bot,))
