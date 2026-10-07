import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    BillingAction,
    BillingCallback,
    ReportCallback,
    orders_keyboard,
    topup_keyboard,
)
from app.bot.reminders import payment_reply
from app.bot.stars import send_star_invoice
from app.db.models import User
from app.domain.billing import credit_package
from app.domain.enums import OrderStatus
from app.domain.errors import OrderNotFoundError
from app.services.billing import (
    TopupOutcomeKind,
    check_pre_checkout,
    confirm_topup,
    create_topup,
    get_balance,
    payable_order,
    topup_payload,
)
from app.services.orders import get_user_order
from app.services.payments.telegram_stars import TelegramStarsGateway
from app.services.report_delivery import ReportFile, ReportUnavailableError, load_report_file
from app.services.storage.base import FileStorage
from app.services.users import get_user_orders

logger = logging.getLogger(__name__)

router = Router(name="menu")


@router.message(F.text == texts.BTN_MY_ORDERS)
async def my_orders(message: Message, session: AsyncSession, user: User) -> None:
    orders = await get_user_orders(session, user.id)
    if not orders:
        await message.answer(texts.NO_ORDERS)
        return
    markup = orders_keyboard(orders)
    if markup is None:
        await message.answer(texts.orders_list(orders))
    else:
        await message.answer(texts.orders_list(orders), reply_markup=markup)


@router.callback_query(ReportCallback.filter())
async def download_report(
    callback: CallbackQuery,
    callback_data: ReportCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
    file_storage: FileStorage,
) -> None:
    """«Скачать отчёт»: свой COMPLETED-заказ → файл, сверенный с SHA-256 → документ.

    Сколько угодно раз; состояние фоновой доставки не меняется. Во время чтения файла
    и отправки транзакция не открыта (load_report_file закрывает её).
    """
    order_id = callback_data.order_id
    try:
        order = await get_user_order(session, user.id, order_id)
        if order.status is not OrderStatus.COMPLETED:
            await session.rollback()
            await callback.answer(texts.REPORT_NOT_READY, show_alert=True)
            return
        report = await load_report_file(session, file_storage, order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    except ReportUnavailableError as exc:
        logger.error("Report file unavailable: order_id=%s code=%s", order_id, exc.code)
        await callback.answer(texts.REPORT_UNAVAILABLE, show_alert=True)
        return
    except (SQLAlchemyError, OSError) as exc:
        # Без traceback и текста исключения: в нём могут быть параметры SQL или пути.
        logger.error("Report download failed: order_id=%s error=%s", order_id, type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.REPORT_DOWNLOAD_FAILED, show_alert=True)
        return
    await callback.answer()
    await _send_document(callback, bot, report, order_id)


async def _send_document(
    callback: CallbackQuery, bot: Bot, report: ReportFile, order_id: int
) -> None:
    # Сообщение с кнопкой может быть уже недоступно; в личном чате chat_id = id пользователя.
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    document = BufferedInputFile(report.data, filename=report.filename)
    try:
        await bot.send_document(chat_id, document, caption=texts.report_ready(order_id))
    except TelegramAPIError as exc:
        # Кнопка остаётся: пользователь может нажать её ещё раз.
        logger.warning(
            "Report download sending failed: order_id=%s error=%s", order_id, type(exc).__name__
        )


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except SQLAlchemyError as exc:
        logger.error("Rollback after failed download also failed: error=%s", type(exc).__name__)


@router.message(F.text == texts.BTN_BALANCE)
async def show_balance(message: Message, user: User) -> None:
    await message.answer(texts.balance_with_packages(user.balance), reply_markup=topup_keyboard())


# --- Пополнение кредитов (Telegram Stars) ---------------------------------------------


def _gateway(bot: Bot) -> TelegramStarsGateway:
    async def send(chat_id: int, payload: str, title: str, description: str, amount: int) -> None:
        await send_star_invoice(bot, chat_id, payload, title, description, amount)

    return TelegramStarsGateway(send)


def _chat_id(callback: CallbackQuery) -> int:
    # Сообщение с кнопкой может быть уже недоступно; в личном чате chat_id = id пользователя.
    return callback.message.chat.id if callback.message else callback.from_user.id


@router.callback_query(BillingCallback.filter(F.action == BillingAction.TOPUP_MENU))
async def topup_menu(callback: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    """«Пополнить баланс» под заказом: баланс и пакеты."""
    balance = await get_balance(session, user.id)
    await callback.answer()
    await bot.send_message(
        _chat_id(callback), texts.balance_with_packages(balance), reply_markup=topup_keyboard()
    )


@router.callback_query(BillingCallback.filter(F.action == BillingAction.BUY))
async def buy_credits(
    callback: CallbackQuery,
    callback_data: BillingCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    """Пакет кредитов: счёт в payments (commit), затем sendInvoice — вне транзакции.

    Сбой отправки оставляет счёт в PENDING без последствий: кредиты зачисляются только
    по подтверждению оплаты.
    """
    package = credit_package(callback_data.package)
    if package is None:
        await callback.answer(texts.PACKAGE_UNKNOWN, show_alert=True)
        return
    user_id = user.id
    try:
        payment = await create_topup(session, user_id, package)
    except SQLAlchemyError as exc:
        logger.error("Topup invoice creation failed: error=%s", type(exc).__name__)
        await _rollback_quietly(session)
        await callback.answer(texts.TOPUP_FAILED, show_alert=True)
        return
    try:
        await _gateway(bot).create_invoice(
            chat_id=_chat_id(callback),
            payload=topup_payload(payment.id),
            amount=payment.amount,
            title=texts.invoice_title(package),
            description=texts.invoice_description(package),
        )
    except TelegramAPIError as exc:
        logger.error(
            "Topup invoice sending failed: payment_id=%s error=%s", payment.id, type(exc).__name__
        )
        await callback.answer(texts.TOPUP_FAILED, show_alert=True)
        return
    await callback.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
    """answerPreCheckoutQuery: счёт свой, не оплачен, не устарел, сумма совпадает.

    Telegram ждёт ответа не дольше 10 секунд: только чтение, без блокировок.
    """
    try:
        refusal = await check_pre_checkout(
            session,
            payload=query.invoice_payload,
            payer_tg_id=query.from_user.id,
            amount=query.total_amount,
            currency=query.currency,
        )
    except SQLAlchemyError as exc:
        logger.error("Pre-checkout check failed: error=%s", type(exc).__name__)
        await _rollback_quietly(session)
        await query.answer(ok=False, error_message=texts.PAYMENT_TEMPORARILY_UNAVAILABLE)
        return
    if refusal is None:
        await query.answer(ok=True)
    else:
        logger.warning("Pre-checkout refused: code=%s", refusal)
        await query.answer(ok=False, error_message=texts.pre_checkout_refusal(refusal))


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, user: User, bot: Bot) -> None:
    """successful_payment: зачисление кредитов ровно один раз (services.billing).

    Если обработать не удалось (ошибка БД), счёт остаётся PENDING и кредиты зачислит
    сверка платежей (PaymentReconciliation).
    """
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    user_id = user.id
    notification = await _gateway(bot).parse_notification(
        {
            "telegram_payment_charge_id": payment.telegram_payment_charge_id,
            "total_amount": payment.total_amount,
            "currency": payment.currency,
            "invoice_payload": payment.invoice_payload,
            "payer_tg_id": message.from_user.id,
        }
    )
    try:
        # UserMiddleware обновил строку пользователя и держит её блокировку до конца
        # транзакции. Фиксируем её до подтверждения: порядок блокировок — платёж →
        # пользователь, как у сверки, иначе возможна взаимная блокировка.
        await session.commit()
        outcome = await confirm_topup(session, notification)
    except SQLAlchemyError as exc:
        logger.error("Payment confirmation failed: error=%s", type(exc).__name__)
        await _rollback_quietly(session)
        await message.answer(texts.PAYMENT_PROCESSING_DELAYED)
        return
    match outcome.kind:
        case TopupOutcomeKind.CREDITED:
            await message.answer(texts.topup_credited(outcome.credits, outcome.balance or 0))
            await _offer_payment(message, session, user_id)
        case TopupOutcomeKind.ALREADY_CREDITED:
            pass  # повтор апдейта: о зачислении уже сообщили
        case TopupOutcomeKind.REJECTED:
            await message.answer(texts.payment_rejected(notification.provider_payment_id))


async def _offer_payment(message: Message, session: AsyncSession, user_id: int) -> None:
    """После пополнения — кнопка оплаты ждущего заказа. Кредиты сами не списываются."""
    order = await payable_order(session, user_id)
    if order is None:
        return
    text, markup = await payment_reply(session, order.id, user_id, order.price)
    await message.answer(text, reply_markup=markup)


@router.message(Command("help"))
@router.message(F.text == texts.BTN_HELP)
async def show_help(message: Message) -> None:
    await message.answer(texts.HELP)
