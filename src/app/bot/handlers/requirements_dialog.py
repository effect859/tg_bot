"""Уточнение и подтверждение ТЗ: ответы на вопросы, изменение поля, подтверждение.

Источник истины — статус заказа и черновик ТЗ в БД, FSM не используется: текущий
вопрос — первое незаполненное обязательное поле. Обычный текст принимается как
ответ только от владельца активного заказа в WAITING_FOR_MISSING_DATA; в других
шагах — напоминание, ничего не записывается. Роутер подключается последним:
команды, кнопки меню и другие хендлеры срабатывают раньше.

Текст ответа не логируется (в нём ФИО и другие персональные данные). При отказе
транзакция откатывается до ответа в Telegram.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.order_intake import _remove_keyboard, _rollback_quietly, _send
from app.bot.handlers.plan import run_plan
from app.bot.keyboards import (
    RequirementsAction,
    RequirementsCallback,
    cancel_order_keyboard,
    edit_fields_keyboard,
)
from app.bot.reminders import dialog_message, remind_active_order, summary_message
from app.db.models import User
from app.domain.enums import OrderStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    OrderNotFoundError,
    RequirementFieldNotEditableError,
    RequirementsChangedError,
    VersionNotFoundError,
)
from app.llm.base import LLMClient
from app.services.orders import get_active_order, get_user_order
from app.services.requirements_dialog import (
    AnswerOutcome,
    AnswerOutcomeKind,
    confirm_requirements,
    get_dialog,
    start_edit,
    submit_answer,
)

logger = logging.getLogger(__name__)

router = Router(name="requirements_dialog")

S = OrderStatus
K = AnswerOutcomeKind

# Кнопки главного меню перехватывает свой роутер раньше; здесь — страховка.
MENU_BUTTONS = frozenset(
    {texts.BTN_NEW_REPORT, texts.BTN_MY_ORDERS, texts.BTN_BALANCE, texts.BTN_HELP}
)


# --- Ответы на вопросы -------------------------------------------------------------


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTONS),
)
async def answer_requirement(message: Message, session: AsyncSession, user: User) -> None:
    """Обычный текст: ответ на текущий вопрос или напоминание о текущем шаге."""
    # После rollback объекты сессии (в том числе user) устаревают: id — заранее.
    user_id = user.id
    order = await get_active_order(session, user_id)
    if order is None:
        await message.answer(texts.NO_ACTIVE_ORDER)
        return
    if order.status is not S.WAITING_FOR_MISSING_DATA:
        await remind_active_order(message, session, order)
        return

    order_id = order.id
    assert message.text is not None  # гарантирует фильтр F.text
    try:
        outcome = await submit_answer(session, order_id, message.text)
        if outcome.kind in (K.REJECTED, K.NOT_EXPECTED):
            await session.rollback()
        else:
            await session.commit()
    except (SQLAlchemyError, VersionNotFoundError) as exc:
        # Без traceback: параметры SQL содержат ответ пользователя.
        logger.error(
            "Requirement answer failed: order_id=%s error=%s", order_id, type(exc).__name__
        )
        await _rollback_quietly(session)
        await message.answer(texts.ANSWER_FAILED)
        return
    await _reply_to_answer(message, session, user_id, order_id, outcome)


async def _reply_to_answer(
    message: Message, session: AsyncSession, user_id: int, order_id: int, outcome: AnswerOutcome
) -> None:
    dialog = outcome.dialog
    match outcome.kind:
        case K.NOT_EXPECTED:
            # Шаг сменился, пока ждали блокировку (например, заказ отменили).
            order = await get_active_order(session, user_id)
            if order is None:
                await message.answer(texts.NO_ACTIVE_ORDER)
            else:
                await remind_active_order(message, session, order)
        case K.REJECTED:
            assert outcome.field is not None and outcome.error is not None
            logger.info(
                "Requirement answer rejected: order_id=%s field=%s error=%s",
                order_id,
                outcome.field,
                outcome.error,
            )
            await message.answer(
                texts.answer_rejected(outcome.field, outcome.error),
                reply_markup=cancel_order_keyboard(order_id),
            )
        case K.DATES_RESET:
            assert dialog is not None and dialog.field is not None
            await message.answer(
                f"{texts.DATES_RESET}\n\n{texts.question(dialog.field)}",
                reply_markup=cancel_order_keyboard(order_id),
            )
        case K.ACCEPTED:
            assert outcome.field is not None and outcome.value is not None
            assert dialog is not None and dialog.field is not None
            await message.answer(
                f"{texts.answer_accepted(outcome.field, outcome.value)}\n\n"
                f"{texts.question(dialog.field)}",
                reply_markup=cancel_order_keyboard(order_id),
            )
        case K.COMPLETED:
            assert dialog is not None
            if outcome.field is not None and outcome.value is not None:
                await message.answer(texts.answer_accepted(outcome.field, outcome.value))
            text, markup = summary_message(dialog)
            await message.answer(text, reply_markup=markup)


# --- Подтверждение и изменение ТЗ --------------------------------------------------


@router.callback_query(RequirementsCallback.filter(F.action == RequirementsAction.CONFIRM))
async def confirm_requirements_button(
    callback: CallbackQuery,
    callback_data: RequirementsCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
    llm_client: LLMClient | None = None,
) -> None:
    """«Подтвердить ТЗ»: WAITING_FOR_DATA_CONFIRMATION -> PREPARING_PLAN, затем план.

    Под блокировкой заказа: двойное нажатие упирается в статус, кнопка под
    устаревшим резюме — в отпечаток содержимого. План составляется в этом же
    апдейте, если подключён LLM-клиент; иначе заказ ждёт прохода восстановления.
    """
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    order_id = order.id
    try:
        await confirm_requirements(session, order_id, callback_data.digest)
        await session.commit()
    except InvalidStatusTransitionError:
        # lock_order обновил order актуальным статусом; текст — до rollback.
        if order.status is S.PREPARING_PLAN:
            refusal = texts.REQUIREMENTS_ALREADY_CONFIRMED
        else:
            refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except RequirementsChangedError:
        await session.rollback()
        await callback.answer(texts.REQUIREMENTS_CHANGED, show_alert=True)
        await _send_current_dialog(callback, bot, session, order_id)
        return
    except VersionNotFoundError:
        logger.error("Requirements draft missing: order_id=%s", order_id)
        await session.rollback()
        await callback.answer(texts.REQUIREMENTS_UNAVAILABLE, show_alert=True)
        return
    except SQLAlchemyError as exc:
        await _database_error(callback, session, order_id, exc)
        return
    await callback.answer()
    await _remove_keyboard(callback, bot)
    if llm_client is None:
        # Отменить заказ можно и после подтверждения: план ещё не оплачен.
        await _send(callback, bot, texts.REQUIREMENTS_CONFIRMED, cancel_order_keyboard(order_id))
        return
    await _send(callback, bot, texts.REQUIREMENTS_CONFIRMED)
    await run_plan(callback, bot, session, llm_client, order_id)


@router.callback_query(RequirementsCallback.filter(F.action == RequirementsAction.EDIT))
async def edit_requirements(
    callback: CallbackQuery,
    callback_data: RequirementsCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """«Изменить»: список обязательных полей. Заказ не меняется."""
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if order.status is not S.WAITING_FOR_DATA_CONFIRMATION:
        refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    order_id = order.id
    await session.rollback()  # только чтение
    await callback.answer()
    await _send(callback, bot, texts.REQUIREMENTS_EDIT_CHOOSE, edit_fields_keyboard(order_id))


@router.callback_query(RequirementsCallback.filter(F.action == RequirementsAction.EDIT_FIELD))
async def edit_requirement_field(
    callback: CallbackQuery,
    callback_data: RequirementsCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """Выбор поля: оно очищается, заказ -> WAITING_FOR_MISSING_DATA, вопрос о нём."""
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    order_id = order.id
    try:
        dialog = await start_edit(session, order_id, callback_data.field)
        await session.commit()
    except InvalidStatusTransitionError:
        refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except RequirementFieldNotEditableError:
        await session.rollback()
        await callback.answer(texts.REQUIREMENT_NOT_EDITABLE, show_alert=True)
        return
    except VersionNotFoundError:
        logger.error("Requirements draft missing: order_id=%s", order_id)
        await session.rollback()
        await callback.answer(texts.REQUIREMENTS_UNAVAILABLE, show_alert=True)
        return
    except SQLAlchemyError as exc:
        await _database_error(callback, session, order_id, exc)
        return
    await callback.answer()
    await _remove_keyboard(callback, bot)
    assert dialog.field is not None  # поле только что очищено
    await _send(callback, bot, texts.question(dialog.field), cancel_order_keyboard(order_id))


async def _database_error(
    callback: CallbackQuery, session: AsyncSession, order_id: int, exc: SQLAlchemyError
) -> None:
    """Ошибка БД в кнопке ТЗ: откат и техническая ошибка пользователю.

    Без traceback и текста исключения: параметры SQL содержат данные ТЗ (ФИО, вуз,
    тему). Исключение не пробрасывается — иначе aiogram запишет его текст в лог.
    """
    logger.error("Requirements action failed: order_id=%s error=%s", order_id, type(exc).__name__)
    await _rollback_quietly(session)
    await callback.answer(texts.REQUIREMENTS_ACTION_FAILED, show_alert=True)


async def _send_current_dialog(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, order_id: int
) -> None:
    """Актуальный вопрос или резюме — после отказа по устаревшей кнопке."""
    try:
        dialog = await get_dialog(session, order_id)
    except (OrderNotFoundError, VersionNotFoundError):
        return
    if dialog.status in (S.WAITING_FOR_MISSING_DATA, S.WAITING_FOR_DATA_CONFIRMATION):
        text, markup = dialog_message(dialog)
        await _send(callback, bot, text, markup)
