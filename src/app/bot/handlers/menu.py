import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import ReportCallback, orders_keyboard
from app.db.models import User
from app.domain.enums import OrderStatus
from app.domain.errors import OrderNotFoundError
from app.services.orders import get_user_order
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
    await message.answer(texts.balance(user.balance))


@router.message(Command("help"))
@router.message(F.text == texts.BTN_HELP)
async def show_help(message: Message) -> None:
    await message.answer(texts.HELP)
