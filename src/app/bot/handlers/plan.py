"""План отчёта: составление после подтверждения ТЗ, повтор после сбоя, показ, подтверждение.

Составление идёт в том же апдейте (подтверждение ТЗ или «Повторить составление
плана»), если подключён LLM-клиент; то, что здесь не завершилось, доделывает проход
восстановления (PlanRecovery). Подтверждённый план переводит заказ в PAYMENT_PENDING
с ценой в кредитах; «Оплатить» списывает кредиты и запускает генерацию
(services.billing.pay_order).

Ошибки БД логируются только по типу: параметры SQL содержат план и ТЗ. При отказе
транзакция откатывается до ответа в Telegram.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.order_intake import _remove_keyboard, _rollback_quietly, _send
from app.bot.keyboards import (
    OrderAction,
    OrderCallback,
    PayOrderCallback,
    PlanAction,
    PlanCallback,
    cancel_order_keyboard,
    plan_confirmation_keyboard,
    plan_failed_keyboard,
)
from app.bot.reminders import active_order_reminder, payment_reply
from app.db.models import User
from app.domain.enums import OrderStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    OrderDomainError,
    OrderNotFoundError,
    PlanChangedError,
    PlanLimitReachedError,
)
from app.domain.plan import PlanOutcome, PlanOutcomeKind
from app.domain.requirements_dialog import content_digest
from app.llm.base import LLMClient
from app.services.billing import ChargeOutcomeKind, pay_order
from app.services.orders import get_user_order
from app.services.plan_confirmation import confirm_plan
from app.services.plan_generation import generate_plan, restart_plan_generation

logger = logging.getLogger(__name__)

router = Router(name="plan")

S = OrderStatus
K = PlanOutcomeKind


@router.callback_query(OrderCallback.filter(F.action == OrderAction.RETRY_PLAN))
async def retry_plan(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
    llm_client: LLMClient | None = None,
) -> None:
    """«Повторить составление плана»: FAILED -> PREPARING_PLAN, затем составление.

    Лимит вызовов модели и исходный статус проверяет сервис под блокировкой заказа:
    одновременные нажатия выполняются по очереди, второе получает отказ.
    """
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    order_id = order.id
    if llm_client is None:
        await session.rollback()
        await callback.answer(texts.PLAN_UNAVAILABLE, show_alert=True)
        return
    try:
        await restart_plan_generation(session, order_id)
        await session.commit()
    except InvalidStatusTransitionError:
        # lock_order обновил order актуальным статусом; текст — до rollback.
        if order.status is S.PREPARING_PLAN:
            refusal = texts.plan_already_restarted(order)
        else:
            refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except PlanLimitReachedError:
        await session.rollback()
        await callback.answer(texts.PLAN_LIMIT_REACHED, show_alert=True)
        return
    except SQLAlchemyError as exc:
        # Без traceback и текста исключения: исключение не пробрасывается, иначе
        # aiogram запишет его текст (с параметрами SQL) в лог.
        logger.error("Plan retry failed: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.PLAN_ACTION_FAILED, show_alert=True)
        return
    await callback.answer()
    await _remove_keyboard(callback, bot)
    await run_plan(callback, bot, session, llm_client, order_id)


async def run_plan(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, llm: LLMClient, order_id: int
) -> None:
    """Составление плана в апдейте кнопки: заказ уже в PREPARING_PLAN и закоммичен."""
    await _send(callback, bot, texts.PLAN_STARTED, cancel_order_keyboard(order_id))
    try:
        outcome = await generate_plan(session, llm, order_id)
    except Exception as exc:
        # Ошибка БД или непредвиденный сбой: попытка будет признана прерванной и
        # повторена проходом восстановления. Без traceback: параметры SQL содержат
        # план и ТЗ.
        logger.error("Plan generation failed: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await _send(callback, bot, texts.PLAN_DELAYED, cancel_order_keyboard(order_id))
        return
    reply = plan_reply(outcome)
    if reply is not None:
        text, markup = reply
        await _send(callback, bot, text, markup)


def plan_reply(outcome: PlanOutcome) -> tuple[str, InlineKeyboardMarkup | None] | None:
    """Сообщение об исходе составления плана; None — сообщать нечего (уже идёт, устарел)."""
    order_id = outcome.order_id
    match outcome.kind:
        case K.COMPLETED:
            assert outcome.plan is not None
            # Отпечаток — от сохранённого содержимого плана (outcome.plan совпадает с ним).
            markup = plan_confirmation_keyboard(order_id, content_digest(outcome.plan))
            return texts.plan_view(order_id, outcome.plan), markup
        case K.RETRY_LATER:
            return texts.PLAN_DELAYED, cancel_order_keyboard(order_id)
        case K.FAILED:
            return (
                texts.plan_failed(order_id, can_retry=outcome.can_retry),
                plan_failed_keyboard(order_id, can_retry=outcome.can_retry),
            )
        case _:
            return None


@router.callback_query(PlanCallback.filter(F.action == PlanAction.CONFIRM))
async def confirm_plan_button(
    callback: CallbackQuery,
    callback_data: PlanCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """«Подтвердить план»: план -> CONFIRMED, заказ -> PAYMENT_PENDING (один commit).

    Под блокировкой заказа: двойное нажатие упирается в статус, кнопка под
    устаревшим планом — в отпечаток содержимого. Сообщение об успехе — только после
    commit.
    """
    # После rollback объекты сессии (в том числе user) устаревают: id — заранее.
    user_id = user.id
    try:
        order = await get_user_order(session, user_id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    order_id = order.id
    try:
        await confirm_plan(session, order_id, callback_data.digest)
        await session.commit()
    except InvalidStatusTransitionError:
        # lock_order обновил order актуальным статусом; текст — до rollback.
        if order.status is S.PAYMENT_PENDING:
            refusal = texts.PLAN_ALREADY_CONFIRMED
        else:
            refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except PlanChangedError:
        await session.rollback()
        await callback.answer(texts.PLAN_CHANGED, show_alert=True)
        await _send_current_plan(callback, bot, session, user_id, order_id)
        return
    except OrderDomainError as exc:
        # Нет черновика, план не на действующем ТЗ и т. п.: нарушение целостности.
        logger.error(
            "Plan confirmation refused: order_id=%s error=%s", order_id, type(exc).__name__
        )
        await session.rollback()
        await callback.answer(texts.PLAN_CONFIRM_FAILED, show_alert=True)
        return
    except SQLAlchemyError as exc:
        # Без traceback и текста исключения (в нём SQL и параметры): исключение не
        # пробрасывается, иначе aiogram запишет его текст в лог.
        logger.error("Plan confirmation failed: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.PLAN_ACTION_FAILED, show_alert=True)
        return
    price = order.price
    await callback.answer()
    await _remove_keyboard(callback, bot)
    text, markup = await payment_reply(session, order_id, user_id, price)
    await _send(callback, bot, f"{texts.plan_confirmed(order_id)}\n\n{text}", markup)


@router.callback_query(PayOrderCallback.filter())
async def pay_order_button(
    callback: CallbackQuery,
    callback_data: PayOrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """«Оплатить N кредитов»: списание и PAYMENT_PENDING -> PAID -> GENERATING (один commit).

    Владельца, статус, цену и баланс проверяет pay_order под блокировкой заказа и
    пользователя: повторное или параллельное нажатие не спишет кредиты второй раз.
    Генерацию запускает проход восстановления (ReportGenerationRecovery).
    """
    user_id = user.id
    order_id = callback_data.order_id
    try:
        # Транзакция апдейта: UserMiddleware уже держит строку пользователя, pay_order
        # блокирует её же, затем заказ — порядок пользователь → заказ, как у всех
        # хендлеров заказа (отмена, план, ТЗ): взаимной блокировки с ними нет.
        outcome = await pay_order(session, user_id, order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    except OrderDomainError as exc:
        # Нарушение целостности (например, нет подтверждённого плана): без списания.
        logger.error("Order payment refused: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.PAYMENT_ACTION_FAILED, show_alert=True)
        return
    except SQLAlchemyError as exc:
        # Без traceback и текста исключения (в нём SQL и параметры).
        logger.error("Order payment failed: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.PAYMENT_ACTION_FAILED, show_alert=True)
        return
    match outcome.kind:
        case ChargeOutcomeKind.PAID:
            await callback.answer()
            await _remove_keyboard(callback, bot)
            await _send(callback, bot, texts.order_paid(order_id, outcome.price, outcome.balance))
        case ChargeOutcomeKind.INSUFFICIENT:
            await callback.answer(
                texts.insufficient_credits(outcome.price, outcome.balance), show_alert=True
            )
        case ChargeOutcomeKind.ALREADY_PAID:
            await callback.answer(texts.ORDER_ALREADY_PAID, show_alert=True)
        case ChargeOutcomeKind.NOT_PAYABLE:
            await callback.answer(texts.ORDER_NOT_PAYABLE, show_alert=True)


async def _send_current_plan(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, user_id: int, order_id: int
) -> None:
    """Актуальный шаг заказа (план с новой кнопкой) — после отказа по устаревшей кнопке."""
    try:
        order = await get_user_order(session, user_id, order_id)
    except OrderNotFoundError:
        return
    text, markup = await active_order_reminder(session, order)
    await session.rollback()  # только чтение
    await _send(callback, bot, text, markup)
