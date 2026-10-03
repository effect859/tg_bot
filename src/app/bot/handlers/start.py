from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import main_menu_keyboard
from app.bot.reminders import remind_active_order
from app.db.models import User
from app.domain.enums import OrderStatus
from app.services.orders import get_active_order

router = Router(name="start")

# Шаги, в которых бот ждёт от пользователя ответа или решения по ТЗ: после /start
# (в том числе после рестарта бота) о них напоминаем сразу.
START_REMINDER_STATUSES = frozenset(
    {
        OrderStatus.WAITING_FOR_MISSING_DATA,
        OrderStatus.WAITING_FOR_DATA_CONFIRMATION,
        OrderStatus.PREPARING_PLAN,
    }
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: User, session: AsyncSession) -> None:
    # /start всегда возвращает в главное меню, прерывая незавершённый диалог.
    await state.clear()
    await message.answer(texts.start_greeting(user.first_name), reply_markup=main_menu_keyboard())
    order = await get_active_order(session, user.id)
    if order is not None and order.status in START_REMINDER_STATUSES:
        await remind_active_order(message, session, order)
