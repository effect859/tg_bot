from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.bot import texts


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_NEW_REPORT)],
            [KeyboardButton(text=texts.BTN_MY_ORDERS), KeyboardButton(text=texts.BTN_BALANCE)],
            [KeyboardButton(text=texts.BTN_HELP)],
        ],
        resize_keyboard=True,
    )
