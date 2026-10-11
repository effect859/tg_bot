"""Хендлеры плана: запуск после подтверждения ТЗ, повтор, показ плана, напоминания.

Message, CallbackQuery и Bot в тестах хендлеров — моки. Маршрутизация проверяется
настоящим Dispatcher.feed_update: Bot с сессией, которая записывает запросы вместо
отправки, — реальный Telegram Bot API не вызывается. БД — настоящая PostgreSQL,
LLM — FakeLLMClient.
"""

import logging
import re
from html import escape
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, TelegramMethod
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.handlers.order_intake import cancel_order, new_report
from app.bot.handlers.plan import plan_reply, retry_plan
from app.bot.handlers.requirements_dialog import answer_requirement, confirm_requirements_button
from app.bot.handlers.start import cmd_start
from app.bot.keyboards import (
    OrderAction,
    OrderCallback,
    PlanCallback,
    RequirementsAction,
    RequirementsCallback,
)
from app.bot.reminders import active_order_reminder
from app.bot.setup import create_dispatcher, notify_plan_outcome
from app.db.models import LLMCall, Order, OrderPlanVersion, User
from app.domain.enums import LLMCallStatus, OrderStatus
from app.domain.plan import (
    MAX_PLAN_LLM_CALLS,
    MAX_SECTION_PAGES,
    PlanOutcome,
    PlanOutcomeKind,
    validate_plan,
)
from app.domain.requirements_dialog import content_digest
from app.llm.base import LLMPermanentError, LLMTemporaryError
from app.services import orders
from app.services import plan_generation as service
from app.services.plan_generation import generate_plan
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from dialog_samples import FULL, fresh_order, order_with_draft
from fake_llm import FakeLLMClient
from plan_samples import (
    GOOD_PLAN,
    PERSONAL_VALUES,
    REQUIREMENTS,
    plan_payload,
    preparing_order,
    section,
)

S = OrderStatus
K = PlanOutcomeKind
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


def _last_markup(bot: AsyncMock) -> Any:
    return bot.send_message.await_args.kwargs["reply_markup"]


def _answers(message: AsyncMock) -> list[str]:
    return [call.args[0] for call in message.answer.await_args_list]


def _actions(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    actions = []
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data
            assert data is not None
            callback_type = PlanCallback if data.startswith("plan:") else OrderCallback
            actions.append(callback_type.unpack(data).action.value)
    return actions


def _alert(callback: AsyncMock) -> str:
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    return str(callback.answer.await_args.args[0])


async def _plans(session: AsyncSession, order_id: int) -> int:
    stmt = select(func.count()).where(OrderPlanVersion.order_id == order_id)
    return int(await session.scalar(stmt) or 0)


async def _confirm(
    session: AsyncSession, user: User, order_id: int, bot: AsyncMock, llm: Any = None
) -> AsyncMock:
    callback = _callback()
    data = RequirementsCallback(
        action=RequirementsAction.CONFIRM, order_id=order_id, digest=content_digest(FULL)
    )
    await confirm_requirements_button(callback, data, session, user, bot, llm)
    return callback


async def _retry(
    session: AsyncSession, user: User, order_id: int, bot: AsyncMock, llm: Any = None
) -> AsyncMock:
    callback = _callback()
    data = OrderCallback(action=OrderAction.RETRY_PLAN, order_id=order_id)
    await retry_plan(callback, data, session, user, bot, llm)
    return callback


async def _planned_order(session: AsyncSession, tg_id: int = 1) -> int:
    """Заказ в WAITING_FOR_PLAN_CONFIRMATION с черновиком плана."""
    order_id = await preparing_order(session, tg_id=tg_id)
    assert (await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)).kind is K.COMPLETED
    return order_id


async def _failed_order(session: AsyncSession, tg_id: int = 1) -> int:
    order_id = await preparing_order(session, tg_id=tg_id)
    llm = FakeLLMClient(LLMPermanentError("http_400"))
    assert (await generate_plan(session, llm, order_id)).kind is K.FAILED
    return order_id


def _db_error() -> OperationalError:
    return OperationalError(
        "INSERT INTO order_plan_versions ...", {"content": SECRET}, Exception("db down")
    )


# --- Подтверждение ТЗ запускает план --------------------------------------------------


async def test_confirm_requirements_generates_and_shows_plan(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot, llm = AsyncMock(), FakeLLMClient(GOOD_PLAN)

    callback = await _confirm(session, user, order_id, bot, llm)

    callback.answer.assert_awaited_once_with()
    confirmed, started, shown = _sent(bot)
    assert (confirmed, started) == (texts.REQUIREMENTS_CONFIRMED, texts.PLAN_STARTED)
    assert shown.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    assert "Введение" in shown and texts.PLAN_NEXT_STEP in shown
    assert _actions(_last_markup(bot)) == ["confirm", "cancel"]
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_PLAN_CONFIRMATION
    assert await _plans(session, order_id) == 1
    assert len(llm.calls) == 1


async def test_confirm_without_llm_leaves_order_for_recovery(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    await _confirm(session, user, order_id, bot)

    assert _sent(bot) == [texts.REQUIREMENTS_CONFIRMED]
    assert _actions(_last_markup(bot)) == ["cancel"]
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    assert await _plans(session, order_id) == 0


async def test_confirm_with_temporary_error_promises_retry(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    await _confirm(session, user, order_id, bot, FakeLLMClient(LLMTemporaryError("http_529")))

    assert _sent(bot)[-1] == texts.PLAN_DELAYED
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN


async def test_confirm_with_permanent_error_offers_retry(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    await _confirm(session, user, order_id, bot, FakeLLMClient(LLMPermanentError("http_403")))

    assert _sent(bot)[-1] == texts.plan_failed(order_id, can_retry=True)
    assert "http_403" not in _sent(bot)[-1]
    assert _actions(_last_markup(bot)) == ["retry_plan", "cancel"]
    order = await fresh_order(session, order_id)
    assert (order.status, order.failed_from_status) == (S.FAILED, S.PREPARING_PLAN)


async def test_confirm_double_click_generates_once(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    llm = FakeLLMClient(GOOD_PLAN)

    await _confirm(session, user, order_id, AsyncMock(), llm)
    second_bot = AsyncMock()
    second = await _confirm(session, user, order_id, second_bot, llm)

    assert _alert(second) == texts.action_outdated(await fresh_order(session, order_id))
    assert _sent(second_bot) == []
    assert len(llm.calls) == 1
    assert await _plans(session, order_id) == 1


async def test_generation_db_error_is_logged_safely(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    failing = AsyncMock(side_effect=_db_error())
    monkeypatch.setattr(plan, "generate_plan", failing)
    bot = AsyncMock()

    await _confirm(session, user, order_id, bot, FakeLLMClient(GOOD_PLAN))

    assert _sent(bot)[-1] == texts.PLAN_DELAYED
    assert f"Plan generation failed: order_id={order_id} error=OperationalError" in caplog.text
    assert SECRET not in caplog.text and "INSERT" not in caplog.text
    assert "parameters" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_plan_and_personal_data_are_not_logged(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    await _confirm(session, user, order_id, AsyncMock(), FakeLLMClient(GOOD_PLAN))

    for marker in ("Введение", "Характеристика организации", *PERSONAL_VALUES[:2]):
        assert marker not in caplog.text


# --- Повтор ---------------------------------------------------------------------------


async def test_retry_restarts_and_shows_plan(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_order(session)
    bot, llm = AsyncMock(), FakeLLMClient(GOOD_PLAN)

    callback = await _retry(session, user, order_id, bot, llm)

    callback.answer.assert_awaited_once_with()
    started, shown = _sent(bot)
    assert started == texts.PLAN_STARTED
    assert shown.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    order = await fresh_order(session, order_id)
    assert (order.status, order.error) == (S.WAITING_FOR_PLAN_CONFIRMATION, None)


async def test_retry_of_foreign_order_is_refused(session: AsyncSession) -> None:
    order_id = await _failed_order(session, tg_id=1)
    stranger = await _user(session, tg_id=2)
    llm = FakeLLMClient(GOOD_PLAN)

    callback = await _retry(session, stranger, order_id, AsyncMock(), llm)

    assert _alert(callback) == texts.ORDER_NOT_FOUND
    assert llm.calls == []
    assert (await fresh_order(session, order_id)).status is S.FAILED


async def test_retry_while_generating_is_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await preparing_order(session)
    llm = FakeLLMClient(GOOD_PLAN)

    callback = await _retry(session, user, order_id, AsyncMock(), llm)

    assert _alert(callback) == f"План отчёта по заказу #{order_id} уже составляется."
    assert llm.calls == []


async def test_stale_retry_button_is_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_order(session)
    await orders.change_status(session, order_id, S.CANCELLED)
    await session.commit()

    callback = await _retry(session, user, order_id, AsyncMock(), FakeLLMClient(GOOD_PLAN))

    assert _alert(callback) == f"Действие уже неактуально: заказ #{order_id} — «отменён»."


async def test_retry_double_click_restarts_once(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_order(session)
    llm = FakeLLMClient(GOOD_PLAN)

    await _retry(session, user, order_id, AsyncMock(), llm)
    second_bot = AsyncMock()
    second = await _retry(session, user, order_id, second_bot, llm)

    assert "Действие уже неактуально" in _alert(second)
    assert _sent(second_bot) == []
    assert len(llm.calls) == 1


async def test_retry_after_limit_is_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_order(session)
    session.add_all(
        LLMCall(
            order_id=order_id,
            step=service.LLM_STEP,
            provider="fake",
            model="m",
            status=LLMCallStatus.ERROR,
            error="http_500",
        )
        for _ in range(MAX_PLAN_LLM_CALLS - 1)  # плюс один уже сделанный — лимит
    )
    await session.commit()
    llm = FakeLLMClient(GOOD_PLAN)

    callback = await _retry(session, user, order_id, AsyncMock(), llm)

    assert _alert(callback) == texts.PLAN_LIMIT_REACHED
    assert llm.calls == []
    assert (await fresh_order(session, order_id)).status is S.FAILED


async def test_retry_without_llm_is_unavailable(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_order(session)

    callback = await _retry(session, user, order_id, AsyncMock())

    assert _alert(callback) == texts.PLAN_UNAVAILABLE
    assert (await fresh_order(session, order_id)).status is S.FAILED


async def test_retry_refusal_rolls_back_before_answer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка заказа не держится во время ответа в Telegram."""
    user = await _user(session)
    order_id = await preparing_order(session)
    calls: list[str] = []
    original_rollback = session.rollback

    async def rollback() -> None:
        calls.append("rollback")
        await original_rollback()

    monkeypatch.setattr(session, "rollback", rollback)
    callback = _callback()
    callback.answer.side_effect = lambda *args, **kwargs: calls.append("answer")

    data = OrderCallback(action=OrderAction.RETRY_PLAN, order_id=order_id)
    await retry_plan(callback, data, session, user, AsyncMock(), FakeLLMClient(GOOD_PLAN))

    assert calls == ["rollback", "answer"]


async def test_retry_db_error_is_logged_safely(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user = await _user(session)
    order_id = await _failed_order(session)
    monkeypatch.setattr(plan, "restart_plan_generation", AsyncMock(side_effect=_db_error()))

    callback = await _retry(session, user, order_id, AsyncMock(), FakeLLMClient(GOOD_PLAN))

    assert _alert(callback) == texts.PLAN_ACTION_FAILED
    assert f"Plan retry failed: order_id={order_id} error=OperationalError" in caplog.text
    assert SECRET not in caplog.text and "INSERT" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


# --- Показ плана ----------------------------------------------------------------------


def _tags_removed(text: str) -> str:
    return re.sub(r"</?(b|i)>", "", text)


def test_plan_view_escapes_model_text() -> None:
    payload = plan_payload(
        section("<b>Введение</b> & цели", description="<script>alert(1)</script>"),
        section("Раздел <i>2</i>", requirements=["a < b > c"]),
        section("Итог"),
    )
    plan_content = validate_plan(payload, None)

    text = texts.plan_view(7, plan_content)

    assert escape("<b>Введение</b> & цели", quote=False) in text
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "<" not in _tags_removed(text)  # только наша разметка
    assert text.count("<b>") == text.count("</b>")


def _worst_case_plan() -> dict[str, Any]:
    """Максимальный допустимый план, где каждый символ при экранировании растёт до 5."""
    payload = {
        "sections": [
            {
                "title": f"{i}".ljust(200, "&"),
                "description": "<" * 1000,
                "requirements": [f"{j}" + ">" * 299 for j in range(10)],
                "target_pages": MAX_SECTION_PAGES,
                "subsections": [
                    {"title": f"{j}" + "&" * 199, "description": "&" * 1000, "target_pages": 1}
                    for j in range(10)
                ],
            }
            for i in range(15)
        ]
    }
    return validate_plan(payload, None)


def test_plan_view_fits_telegram_limit_after_escaping() -> None:
    text = texts.plan_view(999_999, _worst_case_plan())

    assert len(text) <= texts.MAX_MESSAGE_CHARS == 4096
    assert texts.PLAN_SHORTENED in text
    assert texts.PLAN_NEXT_STEP in text
    assert "<" not in _tags_removed(text)
    # Структура не теряется: все 15 разделов на месте.
    assert all(f"<b>{n}. " in text for n in range(1, 16))
    # Сокращение не режет HTML-сущности.
    assert not re.search(r"&(?!amp;|lt;|gt;)", text)


def test_regular_plan_is_shown_in_full() -> None:
    plan_content = validate_plan(plan_payload(), 25)

    text = texts.plan_view(1, plan_content)

    assert texts.PLAN_SHORTENED not in text
    assert "Объём: 25 стр." in text
    # Нумерация как в DOCX: «Введение» без номера, основные разделы с 1 (Stage 3.11).
    assert "<b>Введение</b>" in text
    assert "1.1. Общие сведения" in text
    assert "• Описать структуру отдела продаж" in text


def test_status_labels() -> None:
    assert texts.status_label(S.PREPARING_PLAN) == "составляется план"
    assert texts.status_label(S.WAITING_FOR_PLAN_CONFIRMATION) == "план готов"


def test_plan_reply_for_silent_outcomes() -> None:
    for kind in (K.BUSY, K.SKIPPED, K.DISCARDED):
        assert plan_reply(PlanOutcome(kind, 1)) is None


async def test_notify_plan_outcome_sends_plan() -> None:
    bot = AsyncMock()
    outcome = PlanOutcome(K.COMPLETED, 5, plan=validate_plan(plan_payload(), None))

    await notify_plan_outcome(bot, 42, outcome)

    chat_id, text = bot.send_message.await_args.args
    assert chat_id == 42 and text.startswith("🗂 <b>План отчёта — заказ #5</b>")
    assert _actions(bot.send_message.await_args.kwargs["reply_markup"]) == ["confirm", "cancel"]


# --- Напоминания, /start, «Новый отчёт», текст, отмена ----------------------------------


async def test_reminder_while_preparing_plan(session: AsyncSession) -> None:
    order_id = await preparing_order(session)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text == texts.plan_in_progress(order_id)
    assert _actions(markup) == ["cancel"]


async def test_reminder_shows_ready_plan(session: AsyncSession) -> None:
    order_id = await _planned_order(session)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    assert _actions(markup) == ["confirm", "cancel"]


async def test_reminder_without_plan_draft(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    order_id = await _planned_order(session)
    order = await fresh_order(session, order_id)
    order_without_plan = Order(id=-1, user_id=order.user_id, status=order.status)

    text, markup = await active_order_reminder(session, order_without_plan)

    assert text == texts.PLAN_SHOW_FAILED
    assert "Plan draft missing: order_id=-1" in caplog.text


@pytest.mark.parametrize(("extra_calls", "can_retry"), [(0, True), (MAX_PLAN_LLM_CALLS, False)])
async def test_reminder_after_plan_failure(
    session: AsyncSession, extra_calls: int, can_retry: bool
) -> None:
    order_id = await _failed_order(session)
    session.add_all(
        LLMCall(
            order_id=order_id,
            step=service.LLM_STEP,
            provider="fake",
            model="m",
            status=LLMCallStatus.ERROR,
            error="x",
        )
        for _ in range(extra_calls)
    )
    await session.commit()

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text == texts.plan_failed(order_id, can_retry=can_retry)
    assert _actions(markup) == (["retry_plan", "cancel"] if can_retry else ["cancel"])


async def test_start_shows_ready_plan(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    user = await _user(session)
    message = _message()

    await cmd_start(message, AsyncMock(), user, session)

    greeting, reminder = _answers(message)
    assert reminder.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")


async def test_start_while_preparing_plan(session: AsyncSession) -> None:
    order_id = await preparing_order(session)
    user = await _user(session)
    message = _message()

    await cmd_start(message, AsyncMock(), user, session)

    assert _answers(message)[1] == texts.plan_in_progress(order_id)


async def test_new_report_with_ready_plan_reminds(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    user = await _user(session)
    message = _message(texts.BTN_NEW_REPORT)

    await new_report(message, session, user)

    [reminder] = _answers(message)
    assert reminder.startswith(f"🗂 <b>План отчёта — заказ #{order_id}</b>")
    count = await session.scalar(select(func.count()).where(Order.user_id == user.id))
    assert count == 1


async def test_text_with_ready_plan_reminds_without_changes(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    user = await _user(session)
    message = _message("Поменяйте план")

    await answer_requirement(message, session, user)

    assert _answers(message)[0].startswith("🗂 <b>План отчёта")
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_PLAN_CONFIRMATION
    assert await _plans(session, order_id) == 1


async def test_cancel_with_ready_plan_then_new_report(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    user = await _user(session)
    bot = AsyncMock()
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id)

    await cancel_order(_callback(), data, session, user, bot)

    assert _sent(bot) == [f"Заказ #{order_id} отменён."]
    assert (await fresh_order(session, order_id)).status is S.CANCELLED
    message = _message(texts.BTN_NEW_REPORT)
    await new_report(message, session, user)
    assert _answers(message) == [texts.CONSENT_REQUEST]


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


async def test_retry_plan_callback_reaches_plan_handler(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id = await _failed_order(session, tg_id=TG_ID)
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
    data = OrderCallback(action=OrderAction.RETRY_PLAN, order_id=order_id).pack()
    update = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb",
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=data,
        ),
    )

    await dispatcher.feed_update(bot, update)

    recorded = bot.session
    assert isinstance(recorded, RecordingSession)
    [answer] = [r for r in recorded.requests if isinstance(r, AnswerCallbackQuery)]
    # Диспетчер без LLM-клиента: хендлер плана отвечает, что составление недоступно.
    assert (answer.text, answer.show_alert) == (texts.PLAN_UNAVAILABLE, True)


# --- Объём ТЗ вне 3..120 (Stage 3.10) -------------------------------------------------


async def _unsupported_target_order(session: AsyncSession, *, failed: bool = True) -> int:
    """ТЗ подтверждено до проверки объёма: target_pages = 150."""
    order_id = await preparing_order(session, content={**REQUIREMENTS, "target_pages": 150})
    if failed:
        outcome = await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)
        assert outcome.error_code == service.TARGET_PAGES_UNSUPPORTED
    return order_id


def test_unsupported_target_reply_has_no_retry_button() -> None:
    outcome = PlanOutcome(K.FAILED, 7, can_retry=False, error_code=service.TARGET_PAGES_UNSUPPORTED)

    reply = plan_reply(outcome)

    assert reply is not None
    text, markup = reply
    assert text == texts.plan_target_pages_unsupported(7)
    assert "от 3 до 120 стр." in text
    assert _actions(markup) == ["cancel"]


async def test_run_plan_with_unsupported_target_explains_without_llm_call(
    session: AsyncSession,
) -> None:
    order_id = await _unsupported_target_order(session, failed=False)
    bot, llm = AsyncMock(), FakeLLMClient(GOOD_PLAN)

    await plan.run_plan(_callback(), bot, session, llm, order_id)

    assert _sent(bot) == [texts.PLAN_STARTED, texts.plan_target_pages_unsupported(order_id)]
    assert _actions(_last_markup(bot)) == ["cancel"]
    assert llm.calls == []


async def test_retry_of_unsupported_target_is_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _unsupported_target_order(session)
    bot, llm = AsyncMock(), FakeLLMClient(GOOD_PLAN)

    callback = await _retry(session, user, order_id, bot, llm)

    assert _alert(callback) == texts.PLAN_TARGET_PAGES_UNSUPPORTED
    assert _sent(bot) == [] and llm.calls == []
    order = await fresh_order(session, order_id)
    assert (order.status, order.failed_from_status) == (S.FAILED, S.PREPARING_PLAN)


async def test_reminder_for_unsupported_target_has_no_retry(session: AsyncSession) -> None:
    order_id = await _unsupported_target_order(session)

    text, markup = await active_order_reminder(session, await fresh_order(session, order_id))

    assert text == texts.plan_target_pages_unsupported(order_id)
    assert _actions(markup) == ["cancel"]
