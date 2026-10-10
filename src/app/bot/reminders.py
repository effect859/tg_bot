"""Напоминание о текущем шаге активного заказа: что ждёт бот и какие кнопки действуют.

Общее для «Новый отчёт», /start и текста, который пришёл не в шаге уточнения
данных. Состояние берётся из БД, FSM не нужен: после рестарта бота и спустя дни
напоминание то же самое.
"""

import logging

from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    analysis_failed_keyboard,
    cancel_order_keyboard,
    confirmation_keyboard,
    consent_keyboard,
    materials_keyboard,
    payment_keyboard,
    plan_confirmation_keyboard,
    plan_failed_keyboard,
)
from app.db.models import Order
from app.domain.enums import OrderStatus
from app.domain.errors import VersionNotFoundError
from app.domain.plan import MAX_PLAN_LLM_CALLS, unsupported_volume
from app.domain.requirements import MAX_ANALYSIS_LLM_CALLS
from app.domain.requirements_dialog import content_digest
from app.services.billing import get_balance
from app.services.plan_generation import (
    count_plan_calls,
    get_plan_draft,
    plan_target_pages_unsupported,
)
from app.services.requirements_analysis import count_analysis_calls
from app.services.requirements_dialog import RequirementsDialog, get_dialog

logger = logging.getLogger(__name__)

S = OrderStatus

Reply = tuple[str, InlineKeyboardMarkup | None]


async def remind_active_order(message: Message, session: AsyncSession, order: Order) -> None:
    text, markup = await active_order_reminder(session, order)
    if markup is None:
        await message.answer(text)
    else:
        await message.answer(text, reply_markup=markup)


async def active_order_reminder(session: AsyncSession, order: Order) -> Reply:
    match order.status:
        case S.WAITING_FOR_CONSENT:
            return texts.CONSENT_REQUEST, consent_keyboard(order.id)
        case S.WAITING_FOR_FILES:
            return texts.waiting_for_files(order), materials_keyboard(order.id)
        case S.ANALYZING_MATERIALS:
            return texts.analysis_in_progress(order), cancel_order_keyboard(order.id)
        case S.FAILED if order.failed_from_status is S.ANALYZING_MATERIALS:
            calls = await count_analysis_calls(session, order.id)
            can_retry = calls < MAX_ANALYSIS_LLM_CALLS
            return (
                texts.analysis_failed(order.id, can_retry=can_retry),
                analysis_failed_keyboard(order.id, can_retry=can_retry),
            )
        case S.WAITING_FOR_MISSING_DATA | S.WAITING_FOR_DATA_CONFIRMATION:
            order_id = order.id
            try:
                dialog = await get_dialog(session, order_id)
            except VersionNotFoundError:
                # Нарушение целостности: шаг уточнения без черновика ТЗ.
                logger.error("Requirements draft missing: order_id=%s", order_id)
                return texts.REQUIREMENTS_UNAVAILABLE, cancel_order_keyboard(order_id)
            return dialog_message(dialog)
        case S.PREPARING_PLAN:
            return texts.plan_in_progress(order.id), cancel_order_keyboard(order.id)
        case S.FAILED if order.failed_from_status is S.PREPARING_PLAN:
            if await plan_target_pages_unsupported(session, order.id):
                return (
                    texts.plan_target_pages_unsupported(order.id),
                    plan_failed_keyboard(order.id, can_retry=False),
                )
            calls = await count_plan_calls(session, order.id)
            can_retry = calls < MAX_PLAN_LLM_CALLS
            return (
                texts.plan_failed(order.id, can_retry=can_retry),
                plan_failed_keyboard(order.id, can_retry=can_retry),
            )
        case S.WAITING_FOR_PLAN_CONFIRMATION:
            order_id = order.id
            plan = await get_plan_draft(session, order_id)
            if plan is None:
                # Нарушение целостности: шаг плана без черновика плана.
                logger.error("Plan draft missing: order_id=%s", order_id)
                return texts.PLAN_SHOW_FAILED, cancel_order_keyboard(order_id)
            markup = plan_confirmation_keyboard(order_id, content_digest(plan))
            return texts.plan_view(order_id, plan), markup
        case S.PAYMENT_PENDING:
            return await payment_reply(session, order.id, order.user_id, order.price)
        case S.GENERATING:
            return texts.generation_in_progress(order.id), None
        case S.FAILED if order.failed_from_status is S.GENERATING:
            # Возврат кредитов и отмену выполнит RefundRecovery (services.order_refunds).
            return texts.generation_failed(order.id), None
        case _:
            return texts.active_order_exists(order), None


async def payment_reply(
    session: AsyncSession, order_id: int, user_id: int, price: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Заказ ждёт оплаты: цена, баланс и «Оплатить» или «Пополнить»."""
    if price is None:
        # Цена фиксируется при входе в PAYMENT_PENDING: нарушение целостности.
        logger.error("Payment pending order has no price: order_id=%s", order_id)
        return texts.PAYMENT_UNAVAILABLE, cancel_order_keyboard(order_id)
    balance = await get_balance(session, user_id)
    return (
        texts.payment_pending(order_id, price, balance),
        payment_keyboard(order_id, price, can_pay=balance >= price),
    )


def dialog_message(dialog: RequirementsDialog) -> tuple[str, InlineKeyboardMarkup]:
    """Текущий вопрос или — если всё заполнено — резюме ТЗ с кнопками подтверждения."""
    field = dialog.field
    if field is not None:
        return texts.question_reminder(dialog.order_id, field), cancel_order_keyboard(
            dialog.order_id
        )
    return summary_message(dialog)


def summary_message(dialog: RequirementsDialog) -> tuple[str, InlineKeyboardMarkup]:
    without_volume = unsupported_volume(dialog.content) is not None
    return (
        texts.requirements_summary(dialog.order_id, dialog.content),
        confirmation_keyboard(dialog.order_id, dialog.digest, without_volume=without_volume),
    )
