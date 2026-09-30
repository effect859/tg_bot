from unittest.mock import AsyncMock

from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.menu import my_orders, show_balance
from app.bot.handlers.start import cmd_start
from app.db.models import Order, User
from app.domain.enums import DocType
from app.services.users import register_or_update_user


async def test_start_greets_and_shows_menu() -> None:
    message = AsyncMock()
    state = AsyncMock()
    user = User(tg_id=1, first_name="<b>Пётр</b>")

    await cmd_start(message, state, user)

    state.clear.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "&lt;b&gt;Пётр&lt;/b&gt;" in text  # имя экранировано для HTML
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(keyboard, ReplyKeyboardMarkup)
    buttons = [button.text for row in keyboard.keyboard for button in row]
    assert buttons == [
        texts.BTN_NEW_REPORT,
        texts.BTN_MY_ORDERS,
        texts.BTN_BALANCE,
        texts.BTN_HELP,
    ]


async def test_balance_is_formatted() -> None:
    message = AsyncMock()

    await show_balance(message, User(tg_id=1, balance=150050))

    assert "1500,50 ₽" in message.answer.await_args.args[0]


async def test_my_orders_empty(session: AsyncSession) -> None:
    user = await register_or_update_user(
        session, tg_id=1, username=None, first_name=None, language_code=None
    )
    message = AsyncMock()

    await my_orders(message, session, user)

    message.answer.assert_awaited_once_with(texts.NO_ORDERS)


async def test_my_orders_lists_orders(session: AsyncSession) -> None:
    user = await register_or_update_user(
        session, tg_id=1, username=None, first_name=None, language_code=None
    )
    order = Order(user_id=user.id, doc_type=DocType.PRACTICE_REPORT)
    session.add(order)
    await session.flush()
    await session.refresh(order)
    message = AsyncMock()

    await my_orders(message, session, user)

    text = message.answer.await_args.args[0]
    assert f"#{order.id}" in text
    assert "Отчёт по практике" in text
    assert "новый" in text
