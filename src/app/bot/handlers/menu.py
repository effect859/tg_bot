from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.db.models import User
from app.services.users import get_user_orders

router = Router(name="menu")


@router.message(F.text == texts.BTN_MY_ORDERS)
async def my_orders(message: Message, session: AsyncSession, user: User) -> None:
    orders = await get_user_orders(session, user.id)
    await message.answer(texts.orders_list(orders) if orders else texts.NO_ORDERS)


@router.message(F.text == texts.BTN_BALANCE)
async def show_balance(message: Message, user: User) -> None:
    await message.answer(texts.balance(user.balance))


@router.message(Command("help"))
@router.message(F.text == texts.BTN_HELP)
async def show_help(message: Message) -> None:
    await message.answer(texts.HELP)
