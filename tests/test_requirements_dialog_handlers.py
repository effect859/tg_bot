"""Хендлеры уточнения и подтверждения ТЗ, /start, напоминания и маршрутизация.

Message, CallbackQuery и Bot в тестах хендлеров — моки. Маршрутизация проверяется
настоящим Dispatcher.feed_update: Bot с сессией, которая записывает запросы вместо
отправки, — реальный Telegram Bot API не вызывается. БД — настоящая PostgreSQL.
"""

import logging
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.handlers.order_intake import analysis_reply, cancel_order, new_report
from app.bot.handlers.requirements_dialog import (
    answer_requirement,
    confirm_requirements_button,
    edit_requirement_field,
    edit_requirements,
)
from app.bot.handlers.start import cmd_start
from app.bot.keyboards import (
    OrderAction,
    OrderCallback,
    RequirementsAction,
    RequirementsCallback,
)
from app.bot.setup import create_dispatcher
from app.db.models import User
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.requirements import REQUIRED_FIELDS
from app.domain.requirements_dialog import AnswerError, content_digest
from app.services import orders
from app.services import requirements_dialog as requirements_dialog_service
from app.services.requirements_analysis import AnalysisOutcome, AnalysisOutcomeKind
from app.services.requirements_dialog import AnswerOutcome
from app.services.requirements_dialog import AnswerOutcomeKind as AnswerKind
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from dialog_samples import AFTER_ANALYSIS, FULL, fresh_order, order_with_draft, versions

S = OrderStatus
SECRET_NAME = "Секретов Секрет Секретович"


async def _user(session: AsyncSession, tg_id: int = 1) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


def _message(text: str) -> AsyncMock:
    message = AsyncMock()
    message.text = text
    return message


def _callback(tg_id: int = 1) -> AsyncMock:
    callback = AsyncMock()
    callback.message = None
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


def _answers(message: AsyncMock) -> list[str]:
    return [call.args[0] for call in message.answer.await_args_list]


def _markups(message: AsyncMock) -> list[InlineKeyboardMarkup | None]:
    return [call.kwargs.get("reply_markup") for call in message.answer.await_args_list]


def _callback_datas(markup: Any) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _req_actions(markup: Any) -> list[str]:
    actions = []
    for data in _callback_datas(markup):
        if data.startswith("req:"):
            actions.append(RequirementsCallback.unpack(data).action.value)
        else:
            actions.append(OrderCallback.unpack(data).action.value)
    return actions


def _sent(bot: AsyncMock) -> list[str]:
    return [call.args[1] for call in bot.send_message.await_args_list]


async def _content(session: AsyncSession, order_id: int) -> dict[str, Any]:
    [draft] = await versions(session, order_id)
    return draft.content


class _Order:
    """Последовательность commit/rollback и ответов в Telegram в хендлере."""

    def __init__(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name in ("commit", "rollback"):
            original = getattr(session, name)
            monkeypatch.setattr(session, name, self._wrap(name, original))

    def _wrap(self, name: str, original: Callable[[], Any]) -> Callable[[], Any]:
        async def wrapper() -> None:
            self.calls.append(name)
            await original()

        return wrapper

    def telegram(self, mock: AsyncMock, name: str) -> None:
        mock.side_effect = lambda *args, **kwargs: self.calls.append(name)


# --- Ответы на вопросы -------------------------------------------------------------


async def test_answer_is_saved_and_next_question_asked(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    message = _message("НИУ ВШЭ")

    await answer_requirement(message, session, user)

    [reply] = _answers(message)
    assert reply.startswith("✅ Записал: вуз — НИУ ВШЭ.")
    assert reply.endswith(texts.question("student_name"))
    assert _req_actions(_markups(message)[0]) == [OrderAction.CANCEL]
    assert (await _content(session, order_id))["university"] == "НИУ ВШЭ"


async def test_invalid_answer_repeats_question_and_changes_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    message = _message("42")
    log = _Order(session, monkeypatch)
    log.telegram(message.answer, "answer")

    await answer_requirement(message, session, user)

    assert log.calls == ["rollback", "answer"]
    assert message.answer.await_args.args[0] == texts.answer_rejected(
        "university", AnswerError.NO_LETTERS
    )
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


async def test_invalid_date_hint(session: AsyncSession) -> None:
    user = await _user(session)
    await order_with_draft(session, {**AFTER_ANALYSIS, "practice_end": None})
    message = _message("в конце июля")

    await answer_requirement(message, session, user)

    [reply] = _answers(message)
    assert "ДД.ММ.ГГГГ" in reply
    assert reply.endswith(texts.question("practice_end"))


async def test_conflicting_date_message(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, {**AFTER_ANALYSIS, "practice_end": None})
    message = _message("30.06.2026")  # раньше начала 01.07.2026

    await answer_requirement(message, session, user)

    [reply] = _answers(message)
    assert reply == f"{texts.DATES_RESET}\n\n{texts.question('practice_start')}"
    content = await _content(session, order_id)
    assert (content["practice_start"], content["practice_end"]) == (None, None)


async def test_last_answer_shows_summary_with_buttons(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, {**FULL, "student_name": None})
    message = _message("Петров Пётр")

    await answer_requirement(message, session, user)

    echo, summary = _answers(message)
    assert echo == "✅ Записал: ФИО студента — Петров Пётр."
    assert summary == texts.requirements_summary(order_id, await _content(session, order_id))
    markup = _markups(message)[1]
    assert _req_actions(markup) == ["confirm", "edit", "cancel"]
    digest = RequirementsCallback.unpack(_callback_datas(markup)[0]).digest
    assert digest == content_digest(await _content(session, order_id))
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_summary_is_escaped_and_has_no_technical_details(session: AsyncSession) -> None:
    user = await _user(session)
    await order_with_draft(session, {**FULL, "student_name": None})
    message = _message("<b>Иван</b> & Co")

    await answer_requirement(message, session, user)

    echo, summary = _answers(message)
    assert "&lt;b&gt;Иван&lt;/b&gt; &amp; Co" in echo
    assert "&lt;b&gt;Иван&lt;/b&gt; &amp; Co" in summary
    assert "<b>Иван</b>" not in summary
    for technical in ("practice_start", "student_name", "None", "{", "draft", "2026-07-01"):
        assert technical not in summary
    assert "01.07.2026" in summary and "28.07.2026" in summary
    assert "Бизнес-информатика" in summary and "25 стр." in summary
    assert "• Оформление по ГОСТ 7.32" in summary


def test_summary_fits_telegram_limit_for_any_content() -> None:
    worst = {
        **FULL,
        "topic": "&" * 500,
        "practice_type": "<" * 255,
        "practice_place": ">" * 255,
        "university": "&" * 255,
        "student_name": "&" * 255,
        "specialty": "&" * 255,
        "student_group": "&" * 255,
        "supervisor": "&" * 255,
        "extra_requirements": ["&" * 500] * 20,
    }

    summary = texts.requirements_summary(10**12, worst)

    assert len(summary) <= texts.MAX_MESSAGE_CHARS
    assert "…и ещё" in summary


def test_typical_summary_is_not_shortened() -> None:
    content = {**FULL, "topic": "Т" * 500, "extra_requirements": ["Требование"] * 10}

    summary = texts.requirements_summary(1, content)

    assert "Т" * 500 in summary
    assert summary.count("• Требование") == 10
    assert "…" not in summary


async def test_other_users_text_does_not_touch_foreign_order(session: AsyncSession) -> None:
    await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    stranger = await _user(session, tg_id=2)
    message = _message("НИУ ВШЭ")

    await answer_requirement(message, session, stranger)

    assert _answers(message) == [texts.NO_ACTIVE_ORDER]
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


async def test_text_without_active_order(session: AsyncSession) -> None:
    user = await _user(session)
    message = _message("Привет")

    await answer_requirement(message, session, user)

    assert _answers(message) == [texts.NO_ACTIVE_ORDER]


async def _order_in(session: AsyncSession, user: User, status: S) -> int:
    order = await orders.create_order(session, user_id=user.id)
    for step in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES, S.ANALYZING_MATERIALS):
        if order.status is status:
            break
        order = await orders.change_status(session, order.id, step)
    if status is S.FAILED:
        order = await orders.change_status(session, order.id, S.FAILED, error="x")
    order_id = order.id
    await session.commit()
    return order_id


@pytest.mark.parametrize(
    ("status", "expected_actions"),
    [
        (S.WAITING_FOR_FILES, ["finish", "cancel"]),
        (S.ANALYZING_MATERIALS, ["cancel"]),
        (S.FAILED, ["retry_analysis", "cancel"]),
    ],
)
async def test_text_in_other_steps_gets_reminder(
    session: AsyncSession, status: S, expected_actions: list[str]
) -> None:
    user = await _user(session)
    order_id = await _order_in(session, user, status)
    order = await fresh_order(session, order_id)
    message = _message("Анализ продаж")

    await answer_requirement(message, session, user)

    [reply] = _answers(message)
    expected = {
        S.WAITING_FOR_FILES: texts.waiting_for_files(order),
        S.ANALYZING_MATERIALS: texts.analysis_in_progress(order),
        S.FAILED: texts.analysis_failed(order_id, can_retry=True),
    }[status]
    assert reply == expected
    assert _req_actions(_markups(message)[0]) == expected_actions
    assert await orders.get_requirements_versions(session, order_id) == []
    assert (await fresh_order(session, order_id)).status is status


async def test_text_while_confirming_shows_summary_and_changes_nothing(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    message = _message("Новая тема")

    await answer_requirement(message, session, user)

    assert _answers(message) == [texts.requirements_summary(order_id, FULL)]
    assert _req_actions(_markups(message)[0]) == ["confirm", "edit", "cancel"]
    assert (await _content(session, order_id)) == FULL


async def test_text_after_confirmation_shows_honest_state(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm(session, user, order_id)
    message = _message("Когда будет план?")

    await answer_requirement(message, session, user)

    assert _answers(message) == [texts.plan_in_progress(order_id)]
    assert _req_actions(_markups(message)[0]) == ["cancel"]


async def test_answer_texts_are_not_logged(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    await order_with_draft(session, {**FULL, "student_name": None})
    caplog.set_level(logging.DEBUG)

    await answer_requirement(_message(SECRET_NAME + " 7"), session, user)  # отказ: цифра
    # Каждый апдейт получает своего пользователя из middleware.
    await answer_requirement(_message(SECRET_NAME), session, await _user(session))

    assert "Секретов" not in caplog.text


async def test_database_error_is_logged_without_answer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, {**FULL, "student_name": None})

    async def failing(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("UPDATE ...", {"content": SECRET_NAME}, Exception("conn"))

    monkeypatch.setattr(requirements_dialog, "submit_answer", failing)
    message = _message(SECRET_NAME)

    await answer_requirement(message, session, user)

    assert _answers(message) == [texts.ANSWER_FAILED]
    assert "Секретов" not in caplog.text
    assert "OperationalError" in caplog.text
    assert (await _content(session, order_id))["student_name"] is None


# --- Кнопки подтверждения и изменения ----------------------------------------------


def _req(action: RequirementsAction, order_id: int, **kwargs: str) -> RequirementsCallback:
    return RequirementsCallback(action=action, order_id=order_id, **kwargs)


async def _confirm(
    session: AsyncSession,
    user: User,
    order_id: int,
    digest: str | None = None,
    callback: AsyncMock | None = None,
    bot: AsyncMock | None = None,
) -> AsyncMock:
    callback = callback or _callback(user.tg_id)
    data = _req(RequirementsAction.CONFIRM, order_id, digest=digest or content_digest(FULL))
    await confirm_requirements_button(callback, data, session, user, bot or AsyncMock())
    return callback


async def test_confirm_moves_to_preparing_plan(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    callback = await _confirm(session, user, order_id, bot=bot)

    callback.answer.assert_awaited_once_with()
    assert _sent(bot) == ["ТЗ подтверждено. Следующий шаг — подготовка плана отчёта."]
    assert _req_actions(bot.send_message.await_args.kwargs["reply_markup"]) == ["cancel"]
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    [version] = await versions(session, order_id)
    assert version.status is VersionStatus.CONFIRMED


async def test_double_confirm_is_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm(session, user, order_id)
    bot = AsyncMock()

    second = await _confirm(session, user, order_id, bot=bot)

    second.answer.assert_awaited_once_with(texts.REQUIREMENTS_ALREADY_CONFIRMED, show_alert=True)
    bot.send_message.assert_not_awaited()
    assert [v.status for v in await versions(session, order_id)] == [VersionStatus.CONFIRMED]


async def test_confirm_from_outdated_summary_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    changed = {**FULL, "topic": "Новая тема"}
    order_id = await order_with_draft(session, changed, status=S.WAITING_FOR_DATA_CONFIRMATION)
    callback = _callback()
    bot = AsyncMock()
    log = _Order(session, monkeypatch)
    log.telegram(callback.answer, "alert")
    log.telegram(bot.send_message, "send")

    await _confirm(session, user, order_id, content_digest(FULL), callback, bot)

    assert log.calls[:2] == ["rollback", "alert"]
    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_CHANGED, show_alert=True)
    # Показано актуальное резюме с актуальной кнопкой.
    assert bot.send_message.await_args.args[1] == texts.requirements_summary(order_id, changed)
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert RequirementsCallback.unpack(_callback_datas(markup)[0]).digest == content_digest(changed)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_stale_confirm_while_waiting_for_data(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    callback = _callback()
    log = _Order(session, monkeypatch)
    log.telegram(callback.answer, "alert")

    await _confirm(session, user, order_id, content_digest(AFTER_ANALYSIS), callback)

    assert log.calls == ["rollback", "alert"]
    order = await fresh_order(session, order_id)
    callback.answer.assert_awaited_once_with(texts.action_outdated(order), show_alert=True)
    assert order.status is S.WAITING_FOR_MISSING_DATA


async def test_foreign_confirm_is_not_found(session: AsyncSession) -> None:
    await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    stranger = await _user(session, tg_id=2)

    callback = await _confirm(session, stranger, order_id)

    callback.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_edit_shows_required_fields_only(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    await edit_requirements(
        _callback(), _req(RequirementsAction.EDIT, order_id), session, user, bot
    )

    assert _sent(bot) == [texts.REQUIREMENTS_EDIT_CHOOSE]
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    datas = [RequirementsCallback.unpack(d) for d in _callback_datas(markup)]
    assert {d.action for d in datas} == {RequirementsAction.EDIT_FIELD}
    assert [d.field for d in datas] == [f for f in REQUIRED_FIELDS if f != "practice_end"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert texts.BTN_EDIT_DATES in labels
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_edit_field_asks_that_question(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    await edit_requirement_field(
        _callback(),
        _req(RequirementsAction.EDIT_FIELD, order_id, field="university"),
        session,
        user,
        bot,
    )

    assert _sent(bot) == [texts.question("university")]
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_MISSING_DATA
    assert (await _content(session, order_id)) == {**FULL, "university": None}

    # Следующий текст — ответ именно на этот вопрос, затем снова резюме.
    message = _message("МГУ имени М. В. Ломоносова")
    await answer_requirement(message, session, user)
    assert (await _content(session, order_id))["university"] == "МГУ имени М. В. Ломоносова"
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


@pytest.mark.parametrize("field", ["specialty", "supervisor", "x:y", ""])
async def test_forged_field_is_refused(session: AsyncSession, field: str) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    callback = _callback()

    await edit_requirement_field(
        callback,
        RequirementsCallback.model_construct(
            action=RequirementsAction.EDIT_FIELD, order_id=order_id, field=field, digest=""
        ),
        session,
        user,
        AsyncMock(),
    )

    callback.answer.assert_awaited_once_with(texts.REQUIREMENT_NOT_EDITABLE, show_alert=True)
    assert (await _content(session, order_id)) == FULL
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_stale_edit_buttons_are_refused(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm(session, user, order_id)

    callback = _callback()
    await edit_requirements(
        callback, _req(RequirementsAction.EDIT, order_id), session, user, AsyncMock()
    )
    second = _callback()
    await edit_requirement_field(
        second,
        _req(RequirementsAction.EDIT_FIELD, order_id, field="topic"),
        session,
        await _user(session),  # новый апдейт — свой пользователь из middleware
        AsyncMock(),
    )

    order = await fresh_order(session, order_id)
    callback.answer.assert_awaited_once_with(texts.action_outdated(order), show_alert=True)
    second.answer.assert_awaited_once_with(texts.action_outdated(order), show_alert=True)
    assert order.status is S.PREPARING_PLAN
    [version] = await versions(session, order_id)
    assert version.content == FULL


async def test_foreign_edit_is_not_found(session: AsyncSession) -> None:
    await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    stranger = await _user(session, tg_id=2)
    callback = _callback(2)

    await edit_requirement_field(
        callback,
        _req(RequirementsAction.EDIT_FIELD, order_id, field="topic"),
        session,
        stranger,
        AsyncMock(),
    )

    callback.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    assert (await _content(session, order_id)) == FULL


@pytest.mark.parametrize("status", [S.WAITING_FOR_MISSING_DATA, S.WAITING_FOR_DATA_CONFIRMATION])
async def test_cancel_from_dialog_steps(session: AsyncSession, status: S) -> None:
    user = await _user(session)
    content = AFTER_ANALYSIS if status is S.WAITING_FOR_MISSING_DATA else FULL
    order_id = await order_with_draft(session, content, status=status)

    await cancel_order(
        _callback(),
        OrderCallback(action=OrderAction.CANCEL, order_id=order_id),
        session,
        user,
        AsyncMock(),
    )

    assert (await fresh_order(session, order_id)).status is S.CANCELLED


async def test_cancel_after_confirmation(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm(session, user, order_id)

    await cancel_order(
        _callback(),
        OrderCallback(action=OrderAction.CANCEL, order_id=order_id),
        session,
        user,
        AsyncMock(),
    )

    assert (await fresh_order(session, order_id)).status is S.CANCELLED


# --- /start, «Новый отчёт», итог анализа -------------------------------------------


async def test_start_reminds_current_question(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    message = AsyncMock()

    await cmd_start(message, AsyncMock(), user, session)

    greeting, reminder = _answers(message)
    assert greeting == texts.start_greeting(None)
    assert reminder == texts.question_reminder(order_id, "university")


async def test_start_shows_summary_when_confirming(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    message = AsyncMock()

    await cmd_start(message, AsyncMock(), user, session)

    assert _answers(message)[1] == texts.requirements_summary(order_id, FULL)
    assert _req_actions(_markups(message)[1]) == ["confirm", "edit", "cancel"]


async def test_start_after_confirmation(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm(session, user, order_id)
    message = AsyncMock()

    await cmd_start(message, AsyncMock(), user, session)

    assert _answers(message)[1] == texts.plan_in_progress(order_id)
    assert _req_actions(_markups(message)[1]) == ["cancel"]


async def test_start_in_other_steps_only_greets(session: AsyncSession) -> None:
    user = await _user(session)
    await _order_in(session, user, S.WAITING_FOR_FILES)
    message = AsyncMock()

    await cmd_start(message, AsyncMock(), user, session)

    message.answer.assert_awaited_once()


async def test_new_report_button_reminds_question(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    message = AsyncMock()

    await new_report(message, session, user)

    assert _answers(message) == [texts.question_reminder(order_id, "university")]


def test_analysis_result_asks_first_question() -> None:
    outcome = AnalysisOutcome(
        AnalysisOutcomeKind.COMPLETED,
        7,
        status=S.WAITING_FOR_MISSING_DATA,
        found=("topic",),
        missing=("practice_type", "university", "student_name"),
    )

    reply = analysis_reply(outcome)

    assert reply is not None
    text, markup = reply
    assert text.endswith(texts.question("practice_type"))
    assert "Сообщим, когда он будет готов" not in text
    assert _req_actions(markup) == ["cancel"]


def test_my_orders_label_for_confirmed_requirements() -> None:
    # С 2.9 план после подтверждения ТЗ действительно составляется.
    assert texts.status_label(S.PREPARING_PLAN) == "составляется план"


# --- Маршрутизация через Dispatcher ------------------------------------------------


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

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        pass


TG_ID = 777


@pytest.fixture
def make_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Dispatcher]:
    def make() -> Dispatcher:
        # Роутер подключается к одному диспетчеру: отцепляем от предыдущего.
        for router in (
            start.router,
            menu.router,
            order_intake.router,
            plan.router,
            requirements_dialog.router,
        ):
            monkeypatch.setattr(router, "_parent_router", None)
        return create_dispatcher(sessionmaker, MemoryStorage(), LocalFileStorage(tmp_path))

    return make


@pytest.fixture
def bot() -> Bot:
    return Bot(token="42:TEST", session=RecordingSession())


def _texts_sent(bot: Bot) -> list[str]:
    session = bot.session
    assert isinstance(session, RecordingSession)
    return [r.text for r in session.requests if isinstance(r, SendMessage)]


def _text_update(text: str, *, chat_type: str = "private", update_id: int = 1) -> Update:
    tg_user = TgUser(id=TG_ID, is_bot=False, first_name="U")
    chat_id = TG_ID if chat_type == "private" else -100
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=tg_user,
            text=text,
        ),
    )


def _callback_update(data: str) -> Update:
    return Update(
        update_id=100,
        callback_query=CallbackQuery(
            id="cb",
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=data,
        ),
    )


@pytest.mark.parametrize(
    ("text", "expected_start"),
    [
        (texts.BTN_MY_ORDERS, "<b>Последние заказы</b>"),
        (texts.BTN_BALANCE, "Ваш баланс"),
        (texts.BTN_HELP, "<b>Как это работает</b>"),
        ("/help", "<b>Как это работает</b>"),
        ("/start", "Привет"),
        (texts.BTN_NEW_REPORT, "Заказ #"),
    ],
)
async def test_menu_and_commands_do_not_reach_text_handler(
    session: AsyncSession,
    make_dispatcher: Callable[[], Dispatcher],
    bot: Bot,
    text: str,
    expected_start: str,
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)

    await make_dispatcher().feed_update(bot, _text_update(text))

    sent = _texts_sent(bot)
    assert sent and sent[0].startswith(expected_start)
    assert not any(t.startswith("✅ Записал") or t.startswith("⚠️") for t in sent)
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


@pytest.mark.parametrize("text", ["/unknown", "/unknown НИУ ВШЭ"])
async def test_unknown_commands_are_not_answers(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot, text: str
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)

    await make_dispatcher().feed_update(bot, _text_update(text))

    assert _texts_sent(bot) == []
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_group_messages_are_ignored(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot, chat_type: str
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)

    await make_dispatcher().feed_update(bot, _text_update("НИУ ВШЭ", chat_type=chat_type))

    assert _texts_sent(bot) == []
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


async def test_private_text_reaches_text_handler(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)

    await make_dispatcher().feed_update(bot, _text_update("НИУ ВШЭ"))

    assert _texts_sent(bot)[0].startswith("✅ Записал: вуз — НИУ ВШЭ.")
    assert (await _content(session, order_id))["university"] == "НИУ ВШЭ"


async def test_order_callback_goes_to_order_handler(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id).pack()

    await make_dispatcher().feed_update(bot, _callback_update(data))

    assert (await fresh_order(session, order_id)).status is S.CANCELLED
    assert (await _content(session, order_id)) == AFTER_ANALYSIS
    assert _texts_sent(bot) == [f"Заказ #{order_id} отменён."]


async def test_req_callback_goes_to_requirements_handler(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot
) -> None:
    order_id = await order_with_draft(
        session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION, tg_id=TG_ID
    )
    data = _req(RequirementsAction.CONFIRM, order_id, digest=content_digest(FULL)).pack()

    await make_dispatcher().feed_update(bot, _callback_update(data))

    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    assert _texts_sent(bot) == [texts.REQUIREMENTS_CONFIRMED]
    session_ = bot.session
    assert isinstance(session_, RecordingSession)
    assert any(isinstance(r, AnswerCallbackQuery) for r in session_.requests)


async def test_dialog_continues_after_restart(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher]
) -> None:
    """Состояние только в БД: новый диспетчер с пустым FSM-хранилищем продолжает диалог."""
    order_id = await order_with_draft(session, AFTER_ANALYSIS, tg_id=TG_ID)

    first_bot = Bot(token="42:TEST", session=RecordingSession())
    await make_dispatcher().feed_update(first_bot, _text_update("НИУ ВШЭ", update_id=1))
    restarted_bot = Bot(token="42:TEST", session=RecordingSession())
    restarted = make_dispatcher()
    await restarted.feed_update(restarted_bot, _text_update("/start", update_id=2))
    await restarted.feed_update(restarted_bot, _text_update("Иванов Иван", update_id=3))

    greeting, reminder, echo, summary = _texts_sent(restarted_bot)
    assert reminder == texts.question_reminder(order_id, "student_name")
    assert echo == "✅ Записал: ФИО студента — Иванов Иван."
    assert summary.startswith("📋 <b>ТЗ отчёта по практике")
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_step_changed_while_waiting_for_lock(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ упёрся в сменившийся шаг: откат, затем актуальное напоминание (user после rollback)."""
    user = await _user(session)
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    async def not_expected(*args: Any, **kwargs: Any) -> AnswerOutcome:
        return AnswerOutcome(AnswerKind.NOT_EXPECTED)

    monkeypatch.setattr(requirements_dialog, "submit_answer", not_expected)
    message = _message("НИУ ВШЭ")

    await answer_requirement(message, session, user)

    assert _answers(message) == [texts.question_reminder(order_id, "university")]
    assert (await _content(session, order_id)) == AFTER_ANALYSIS


# --- I-1: ошибка БД в кнопках ТЗ не пишет данные ТЗ в лог ---------------------------

SECRET_CONTENT = {
    **FULL,
    "topic": "Секретная тема практики",
    "practice_place": "ООО «Секретное место»",
    "university": "Секретный университет",
    "student_name": SECRET_NAME,
    "extra_requirements": ["Секретное требование"],
}


def _db_error(content: dict[str, Any]) -> OperationalError:
    # Как настоящая ошибка UPDATE: в тексте исключения — [parameters: ...] с данными ТЗ.
    return OperationalError(
        "UPDATE order_requirements_versions SET content=$1", {"content": content}, Exception("x")
    )


def _assert_log_is_safe(caplog: pytest.LogCaptureFixture) -> None:
    assert "Секрет" not in caplog.text
    assert "[parameters" not in caplog.text
    assert "UPDATE order_requirements_versions" not in caplog.text
    assert "Requirements action failed" in caplog.text
    assert "error=OperationalError" in caplog.text


async def test_edit_database_error_rolls_back_without_logging_data(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(
        session, SECRET_CONTENT, status=S.WAITING_FOR_DATA_CONFIRMATION
    )
    real_start_edit = requirements_dialog.start_edit

    async def failing_after_write(session_: AsyncSession, *args: Any) -> Any:
        await real_start_edit(session_, *args)  # поле очищено, статус сменён — не зафиксировано
        raise _db_error(SECRET_CONTENT)

    monkeypatch.setattr(requirements_dialog, "start_edit", failing_after_write)
    callback = _callback()
    bot = AsyncMock()
    log = _Order(session, monkeypatch)
    log.telegram(callback.answer, "alert")
    caplog.set_level(logging.DEBUG)

    await edit_requirement_field(
        callback,
        _req(RequirementsAction.EDIT_FIELD, order_id, field="student_name"),
        session,
        user,
        bot,
    )

    assert log.calls == ["rollback", "alert"]
    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_ACTION_FAILED, show_alert=True)
    bot.send_message.assert_not_awaited()
    _assert_log_is_safe(caplog)
    # Откат: поле не очищено, заказ на том же шаге.
    assert (await _content(session, order_id)) == SECRET_CONTENT
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


async def test_confirm_database_error_rolls_back_without_logging_data(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(
        session, SECRET_CONTENT, status=S.WAITING_FOR_DATA_CONFIRMATION
    )
    real_confirm = requirements_dialog.confirm_requirements

    async def failing_after_write(session_: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        # Версия подтверждена, статус сменён — не зафиксировано.
        await real_confirm(session_, *args, **kwargs)
        raise _db_error(SECRET_CONTENT)

    monkeypatch.setattr(requirements_dialog, "confirm_requirements", failing_after_write)
    callback = _callback()
    bot = AsyncMock()
    log = _Order(session, monkeypatch)
    log.telegram(callback.answer, "alert")
    caplog.set_level(logging.DEBUG)

    await _confirm(session, user, order_id, content_digest(SECRET_CONTENT), callback, bot)

    assert log.calls == ["rollback", "alert"]
    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_ACTION_FAILED, show_alert=True)
    bot.send_message.assert_not_awaited()
    _assert_log_is_safe(caplog)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [version] = await versions(session, order_id)
    assert version.status is VersionStatus.DRAFT


async def test_confirm_commit_error_is_handled_safely(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой самого commit (после успешного подтверждения в транзакции) — тот же безопасный путь."""
    user = await _user(session)
    order_id = await order_with_draft(
        session, SECRET_CONTENT, status=S.WAITING_FOR_DATA_CONFIRMATION
    )
    calls: list[str] = []
    original_rollback = session.rollback

    async def failing_commit() -> None:
        calls.append("commit")
        raise _db_error(SECRET_CONTENT)

    async def rollback() -> None:
        calls.append("rollback")
        await original_rollback()

    monkeypatch.setattr(session, "commit", failing_commit)
    monkeypatch.setattr(session, "rollback", rollback)
    callback = _callback()
    callback.answer.side_effect = lambda *args, **kwargs: calls.append("alert")
    bot = AsyncMock()
    caplog.set_level(logging.DEBUG)

    await _confirm(session, user, order_id, content_digest(SECRET_CONTENT), callback, bot)

    assert calls == ["commit", "rollback", "alert"]
    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_ACTION_FAILED, show_alert=True)
    bot.send_message.assert_not_awaited()
    _assert_log_is_safe(caplog)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


# --- Неподдерживаемый объём: предупреждение и согласие (Stage 3.10) ------------------

UNSUPPORTED = {**FULL, "target_pages": None, "unsupported_target_pages": 150}
LEGACY = {**FULL, "target_pages": 150}  # объём в target_pages, как до Stage 3.10
HUGE = 10**499 + 7


@pytest.mark.parametrize("content", [UNSUPPORTED, LEGACY])
async def test_summary_warns_and_offers_consent(
    session: AsyncSession, content: dict[str, Any]
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, content, status=S.WAITING_FOR_DATA_CONFIRMATION)
    message = AsyncMock()

    await cmd_start(message, AsyncMock(), user, session)

    summary = _answers(message)[1]
    assert summary == texts.requirements_summary(order_id, content)
    assert texts.volume_unsupported_warning(150) in summary
    assert summary.count("150 стр.") == 1  # исходный объём — один раз, в предупреждении
    assert texts.REQUIREMENTS_VOLUME_PROMPT in summary
    assert texts.REQUIREMENTS_CONFIRM_PROMPT not in summary
    markup = _markups(message)[1]
    assert _req_actions(markup) == ["no_volume", "edit", "cancel"]
    assert markup.inline_keyboard[0][0].text == texts.BTN_CONFIRM_WITHOUT_VOLUME
    data = RequirementsCallback.unpack(_callback_datas(markup)[0])
    assert data.digest == content_digest(content)
    assert all(len(d.encode()) <= 64 for d in _callback_datas(markup))


def test_supported_volume_summary_has_no_warning() -> None:
    summary = texts.requirements_summary(1, FULL)

    assert "⚠️" not in summary
    assert "25 стр." in summary
    assert texts.REQUIREMENTS_CONFIRM_PROMPT in summary


def test_summary_with_longest_unsupported_volume_fits_telegram_limit() -> None:
    """Число не сокращается (обрезанное число неверно): резюме всё равно укладывается."""
    worst = {
        **FULL,
        "topic": "&" * 500,
        "practice_type": "<" * 255,
        "practice_place": ">" * 255,
        "university": "&" * 255,
        "student_name": "&" * 255,
        "specialty": "&" * 255,
        "student_group": "&" * 255,
        "supervisor": "&" * 255,
        "extra_requirements": ["&" * 500] * 20,
        "target_pages": None,
        "unsupported_target_pages": HUGE,
    }

    summary = texts.requirements_summary(10**12, worst)

    assert len(summary) <= texts.MAX_MESSAGE_CHARS
    assert str(HUGE) in summary


async def _consent(
    session: AsyncSession,
    user: User,
    order_id: int,
    digest: str,
    callback: AsyncMock | None = None,
    bot: AsyncMock | None = None,
) -> AsyncMock:
    callback = callback or _callback(user.tg_id)
    data = _req(RequirementsAction.CONFIRM_WITHOUT_VOLUME, order_id, digest=digest)
    await confirm_requirements_button(callback, data, session, user, bot or AsyncMock())
    return callback


@pytest.mark.parametrize("content", [UNSUPPORTED, LEGACY])
async def test_consent_confirms_without_volume(
    session: AsyncSession, content: dict[str, Any]
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, content, status=S.WAITING_FOR_DATA_CONFIRMATION)
    bot = AsyncMock()

    callback = await _consent(session, user, order_id, content_digest(content), bot=bot)

    callback.answer.assert_awaited_once_with()
    assert _sent(bot) == [texts.REQUIREMENTS_CONFIRMED]
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    [version] = await versions(session, order_id)
    assert (version.status, version.content) == (VersionStatus.CONFIRMED, UNSUPPORTED)


@pytest.mark.parametrize(
    ("content", "action"),
    [
        (UNSUPPORTED, RequirementsAction.CONFIRM),
        (LEGACY, RequirementsAction.CONFIRM),
        (FULL, RequirementsAction.CONFIRM_WITHOUT_VOLUME),
    ],
)
async def test_button_not_matching_volume_is_refused(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    content: dict[str, Any],
    action: RequirementsAction,
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, content, status=S.WAITING_FOR_DATA_CONFIRMATION)
    callback = _callback()
    bot = AsyncMock()
    log = _Order(session, monkeypatch)
    log.telegram(callback.answer, "alert")
    data = _req(action, order_id, digest=content_digest(content))

    await confirm_requirements_button(callback, data, session, user, bot)

    assert log.calls[:2] == ["rollback", "alert"]
    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_CHANGED, show_alert=True)
    # Актуальное резюме с правильной кнопкой.
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    expected = "confirm" if content is FULL else "no_volume"
    assert _req_actions(markup)[0] == expected
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    assert await _content(session, order_id) == content


async def test_consent_database_error_rolls_back_transfer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, LEGACY, status=S.WAITING_FOR_DATA_CONFIRMATION)

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise _db_error(LEGACY)

    monkeypatch.setattr(requirements_dialog_service, "confirm_requirements_version", fail)
    callback = await _consent(session, user, order_id, content_digest(LEGACY))

    callback.answer.assert_awaited_once_with(texts.REQUIREMENTS_ACTION_FAILED, show_alert=True)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [draft] = await versions(session, order_id)
    assert (draft.status, draft.content) == (VersionStatus.DRAFT, LEGACY)


async def test_cancel_instead_of_consent(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await order_with_draft(session, LEGACY, status=S.WAITING_FOR_DATA_CONFIRMATION)
    callback = _callback(user.tg_id)
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order_id)

    await cancel_order(callback, data, session, user, AsyncMock())

    assert (await fresh_order(session, order_id)).status is S.CANCELLED
    [draft] = await versions(session, order_id)
    assert (draft.status, draft.content) == (VersionStatus.DRAFT, LEGACY)


async def test_consent_callback_goes_to_requirements_handler(
    session: AsyncSession, make_dispatcher: Callable[[], Dispatcher], bot: Bot
) -> None:
    order_id = await order_with_draft(
        session, UNSUPPORTED, status=S.WAITING_FOR_DATA_CONFIRMATION, tg_id=TG_ID
    )
    data = _req(
        RequirementsAction.CONFIRM_WITHOUT_VOLUME, order_id, digest=content_digest(UNSUPPORTED)
    ).pack()

    await make_dispatcher().feed_update(bot, _callback_update(data))

    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    assert _texts_sent(bot) == [texts.REQUIREMENTS_CONFIRMED]
