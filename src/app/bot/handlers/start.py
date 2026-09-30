from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import main_menu_keyboard
from app.db.models import User

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: User) -> None:
    # /start всегда возвращает в главное меню, прерывая незавершённый диалог.
    await state.clear()
    await message.answer(texts.start_greeting(user.first_name), reply_markup=main_menu_keyboard())
