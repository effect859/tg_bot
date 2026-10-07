"""Хендлер «Подтвердить план», экран PAYMENT_PENDING и навигация вокруг него.

Message, CallbackQuery и Bot в тестах хендлеров — моки. Маршрутизация проверяется
настоящим Dispatcher.feed_update: Bot с сессией, которая записывает запросы вместо
отправки, — реальный Telegram Bot API не вызывается. БД — настоящая PostgreSQL.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.handlers.order_intake import cancel_order, new_report
from app.bot.handlers.plan import confirm_plan_button
from app.bot.handlers.requirements_dialog import answer_requirement
from app.bot.handlers.start import cmd_start
from app.bot.keyboards import (
    BillingCallback,
    OrderAction,
    OrderCallback,
    PlanAction,
    PlanCallback,
)
from app.bot.reminders import active_order_reminder
from app.bot.setup import create_dispatcher
from app.db.models import Order, OrderPlanVersion, User
from app.domain.billing import order_price
from app.domain.enums import DocType, OrderStatus, VersionStatus
from app.domain.requirements_dialog import content_digest
from app.services import orders
from app.services.plan_generation import generate_plan
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from dialog_samples import fresh_order
from fake_llm import FakeLLMClient
from plan_samples import GOOD_PLAN, preparing_order

S = OrderStatus
SECRET = "Секретов Секрет Секретович"


async def _user(session: AsyncSession, tg_id: int = 1) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


def _callback(tg_id: int = 1) -> AsyncMock:
    callback = AsyncMock()
    callback.message = None
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


def _message(text: str = "") -> AsyncMock:
    message = AsyncMock()
    message.text = text
    return message


def _sent(bot: AsyncMock) -> list[str]:
    return [call.args[1] for call in bot.send_message.await_args_list]


def _answers(message: AsyncMock) -> list[str]:
    return [call.args[0] for call in message.answer.await_args_list]


def _buttons(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


def _actions(markup: Any) -> list[str]:
    actions = []
    for data in _buttons(markup):
        if data.startswith("pay:"):
            actions.append("pay")
            continue
        callback_type: Any = OrderCallback
        if data.startswith("plan:"):
            callback_type = PlanCallback
        elif data.startswith("bill:"):
            callback_type = BillingCallback
        actions.append(callback_type.unpack(data).action.value)
    return actions


# Цена фиксируется при подтверждении плана; баланс в этих тестах — 0.
PRICE = order_price(DocType.PRACTICE_REPORT)


def _pending_text(order_id: int) -> str:
    return texts.payment_pending(order_id, PRICE, 0)


def _confirmed_text(order_id: int) -> str:
    return f"{texts.plan_confirmed(order_id)}\n\n{_pending_text(order_id)}"


def _alert(callback: AsyncMock) -> str:
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    return str(callback.answer.await_args.args[0])


async def _planned_order(session: AsyncSession, tg_id: int = 1) -> int:
    order_id = await preparing_order(session, tg_id=tg_id)
    await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)
    return order_id


async def _plan_statuses(session: AsyncSession, order_id: int) -> list[VersionStatus]:
    stmt = (
        select(OrderPlanVersion.status)
        .where(OrderPlanVersion.order_id == order_id)
        .order_by(OrderPlanVersion.version)
    )
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _digest(session: AsyncSession, order_id: int) -> str:
    plan_content = await session.scalar(
        select(OrderPlanVersion.content).where(OrderPlanVersion.order_id == order_id)
    )
    assert plan_content is not None
    return content_digest(plan_content)


async def _press(
    session: AsyncSession,
    user: User,
    order_id: int,
    digest: str,
    bot: AsyncMock | None = None,
    callback: AsyncMock | None = None,
) -> AsyncMock:
    callback = callback or _callback()
    data = PlanCallback(action=PlanAction.CONFIRM, order_id=order_id, digest=digest)
    await confirm_plan_button(callback, data, session, user, bot or AsyncMock())
    return callback


async def _state(session: AsyncSession, order_id: int) -> tuple[S, list[VersionStatus]]:
    return (await fresh_order(session, order_id)).status, await _plan_statuses(session, order_id)


# --- Кнопка ---------------------------------------------------------------------------


async def test_plan_screen_has_confirm_button_with_stored_digest(session: AsyncSession) -> None:
    order_id = await _planned_order(session)

    _, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert _actions(markup) == ["confirm", "cancel"]
    confirm = PlanCallback.unpack(_buttons(markup)[0])
    assert (confirm.order_id, confirm.digest) == (order_id, await _digest(session, order_id))
    assert markup.inline_keyboard[0][0].text == texts.BTN_CONFIRM_PLAN
    # Лимит Telegram на callback_data — 64 байта.
    assert len(_buttons(markup)[0].encode()) <= 64


def test_callback_data_fits_telegram_limit() -> None:
    data = PlanCallback(action=PlanAction.CONFIRM, order_id=2**63 - 1, digest="f" * 12).pack()

    assert len(data.encode()) <= 64


# --- Подтверждение --------------------------------------------------------------------


async def test_confirm_moves_to_payment_pending(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _planned_order(session)
    bot = AsyncMock()

    callback = await _press(session, user, order_id, await _digest(session, order_id), bot)

    callback.answer.assert_awaited_once_with()
    assert _sent(bot) == [_confirmed_text(order_id)]
    # Кредитов нет: «Пополнить баланс» вместо «Оплатить».
    assert _actions(bot.send_message.await_args.kwargs["reply_markup"]) == ["menu", "cancel"]
    assert (await fresh_order(session, order_id)).price == PRICE
    assert await _state(session, order_id) == (S.PAYMENT_PENDING, [VersionStatus.CONFIRMED])


async def test_repeated_press_says_already_confirmed(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    await _press(session, user, order_id, digest)
    bot = AsyncMock()

    callback = await _press(session, user, order_id, digest, bot)

    assert _alert(callback) == texts.PLAN_ALREADY_CONFIRMED
    assert _sent(bot) == []
    assert await _state(session, order_id) == (S.PAYMENT_PENDING, [VersionStatus.CONFIRMED])


async def test_press_after_cancel_is_outdated(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    await orders.change_status(session, order_id, S.CANCELLED)
    await session.commit()

    callback = await _press(session, user, order_id, digest)

    assert _alert(callback) == f"Действие уже неактуально: заказ #{order_id} — «отменён»."
    assert await _state(session, order_id) == (S.CANCELLED, [VersionStatus.DRAFT])


async def test_stale_digest_shows_current_plan(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _planned_order(session)
    bot = AsyncMock()

    callback = await _press(session, user, order_id, "000000000000", bot)

    assert _alert(callback) == texts.PLAN_CHANGED
    [current] = _sent(bot)
    assert current.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert PlanCallback.unpack(_buttons(markup)[0]).digest == await _digest(session, order_id)
    assert await _state(session, order_id) == (
        S.WAITING_FOR_PLAN_CONFIRMATION,
        [VersionStatus.DRAFT],
    )


async def test_foreign_order_is_not_found(session: AsyncSession) -> None:
    order_id = await _planned_order(session, tg_id=1)
    stranger = await _user(session, tg_id=2)

    callback = await _press(session, stranger, order_id, await _digest(session, order_id))

    assert _alert(callback) == texts.ORDER_NOT_FOUND
    assert await _state(session, order_id) == (
        S.WAITING_FOR_PLAN_CONFIRMATION,
        [VersionStatus.DRAFT],
    )


async def test_missing_draft_is_technical_error(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    order_id = await preparing_order(session)
    await session.execute(
        update(Order).where(Order.id == order_id).values(status=S.WAITING_FOR_PLAN_CONFIRMATION)
    )
    await session.commit()

    callback = await _press(session, user, order_id, "000000000000")

    assert _alert(callback) == texts.PLAN_CONFIRM_FAILED
    assert (
        f"Plan confirmation refused: order_id={order_id} error=VersionNotFoundError" in caplog.text
    )


def _db_error() -> OperationalError:
    return OperationalError(
        "UPDATE order_plan_versions ...", {"content": SECRET}, Exception("db down")
    )


async def test_db_error_is_logged_safely_and_not_reported_as_success(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await _user(session)
    order_id = await _planned_order(session)
    monkeypatch.setattr(plan, "confirm_plan", AsyncMock(side_effect=_db_error()))
    bot = AsyncMock()

    callback = await _press(session, user, order_id, await _digest(session, order_id), bot)

    assert _alert(callback) == texts.PLAN_ACTION_FAILED
    assert _sent(bot) == []
    assert f"Plan confirmation failed: order_id={order_id} error=OperationalError" in caplog.text
    assert SECRET not in caplog.text and "UPDATE" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_commit_failure_is_not_reported_as_success(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой commit: rollback, ни плана CONFIRMED, ни сообщения об успехе."""
    user = await _user(session)
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    monkeypatch.setattr(session, "commit", AsyncMock(side_effect=_db_error()))
    bot = AsyncMock()

    callback = await _press(session, user, order_id, digest, bot)

    assert _alert(callback) == texts.PLAN_ACTION_FAILED
    assert _sent(bot) == []
    monkeypatch.undo()
    assert await _state(session, order_id) == (
        S.WAITING_FOR_PLAN_CONFIRMATION,
        [VersionStatus.DRAFT],
    )


async def test_refusal_rolls_back_before_answer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка заказа не держится во время ответа в Telegram."""
    user = await _user(session)
    order_id = await _planned_order(session)
    calls: list[str] = []
    original_rollback = session.rollback

    async def rollback() -> None:
        calls.append("rollback")
        await original_rollback()

    monkeypatch.setattr(session, "rollback", rollback)
    callback = _callback()
    callback.answer.side_effect = lambda *args, **kwargs: calls.append("answer")

    await _press(session, user, order_id, "000000000000", callback=callback)

    assert calls[:2] == ["rollback", "answer"]


async def test_success_answers_after_commit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    calls: list[str] = []
    original_commit = session.commit

    async def commit() -> None:
        calls.append("commit")
        await original_commit()

    monkeypatch.setattr(session, "commit", commit)
    callback = _callback()
    callback.answer.side_effect = lambda *args, **kwargs: calls.append("answer")
    bot = AsyncMock()
    bot.send_message.side_effect = lambda *args, **kwargs: calls.append("send")

    await _press(session, user, order_id, digest, bot, callback)

    assert calls == ["commit", "answer", "send"]


# --- PAYMENT_PENDING и навигация ------------------------------------------------------


async def _payment_pending_order(session: AsyncSession) -> int:
    user = await _user(session)
    order_id = await _planned_order(session)
    await _press(session, user, order_id, await _digest(session, order_id))
    return order_id


async def test_payment_pending_reminder(session: AsyncSession) -> None:
    order_id = await _payment_pending_order(session)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text == _pending_text(order_id)
    assert _actions(markup) == ["menu", "cancel"]


async def test_start_in_payment_pending(session: AsyncSession) -> None:
    order_id = await _payment_pending_order(session)
    message = _message()

    await cmd_start(message, AsyncMock(), await _user(session), session)

    greeting, reminder = _answers(message)
    assert reminder == _pending_text(order_id)


async def test_start_waiting_for_plan_confirmation_offers_confirm(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    message = _message()

    await cmd_start(message, AsyncMock(), await _user(session), session)

    assert _answers(message)[1].startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    markup = message.answer.await_args_list[1].kwargs["reply_markup"]
    assert _actions(markup) == ["confirm", "cancel"]


@pytest.mark.parametrize("pending", [False, True])
async def test_new_report_does_not_create_order_over_active_one(
    session: AsyncSession, pending: bool
) -> None:
    order_id = await (_payment_pending_order(session) if pending else _planned_order(session))
    user = await _user(session)
    message = _message(texts.BTN_NEW_REPORT)

    await new_report(message, session, user)

    expected = _pending_text(order_id) if pending else "🗂 <b>План отчёта"
    assert _answers(message)[0].startswith(expected)
    count = await session.scalar(select(func.count()).where(Order.user_id == user.id))
    assert count == 1


async def test_text_in_payment_pending_reminds_without_changes(session: AsyncSession) -> None:
    order_id = await _payment_pending_order(session)
    message = _message("Когда оплата?")

    await answer_requirement(message, session, await _user(session))

    assert _answers(message) == [_pending_text(order_id)]
    assert await _state(session, order_id) == (S.PAYMENT_PENDING, [VersionStatus.CONFIRMED])


async def test_cancel_payment_pending_then_new_report(session: AsyncSession) -> None:
    order_id = await _payment_pending_order(session)
    user = await _user(session)
    bot = AsyncMock()
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id)

    await cancel_order(_callback(), data, session, user, bot)

    assert _sent(bot) == [f"Заказ #{order_id} отменён."]
    assert await _state(session, order_id) == (S.CANCELLED, [VersionStatus.CONFIRMED])
    message = _message(texts.BTN_NEW_REPORT)
    await new_report(message, session, user)
    assert _answers(message) == [texts.CONSENT_REQUEST]


def test_help_does_not_promise_ready_docx() -> None:
    assert "После оплаты вы получаете готовый DOCX" not in texts.HELP
    assert "пока в разработке" in texts.HELP


# --- Маршрутизация --------------------------------------------------------------------

TG_ID = 777


class RecordingSession(BaseSession):
    """Сессия Bot без сети: запросы к Bot API только записываются."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура BaseSession
    ) -> Any:
        self.requests.append(method)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def test_plan_callback_reaches_confirm_handler(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id = await _planned_order(session, tg_id=TG_ID)
    for router in (
        start.router,
        menu.router,
        order_intake.router,
        plan.router,
        requirements_dialog.router,
    ):
        monkeypatch.setattr(router, "_parent_router", None)
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), LocalFileStorage(tmp_path))
    bot = Bot(token="42:TEST", session=RecordingSession())
    data = PlanCallback(
        action=PlanAction.CONFIRM, order_id=order_id, digest=await _digest(session, order_id)
    ).pack()
    update_ = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb",
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=data,
        ),
    )

    await dispatcher.feed_update(bot, update_)

    recorded = bot.session
    assert isinstance(recorded, RecordingSession)
    assert any(isinstance(r, AnswerCallbackQuery) for r in recorded.requests)
    sent = [r.text for r in recorded.requests if isinstance(r, SendMessage)]
    assert sent == [_confirmed_text(order_id)]
    assert (await fresh_order(session, order_id)).status is S.PAYMENT_PENDING
