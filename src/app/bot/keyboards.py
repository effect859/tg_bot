from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts


class OrderAction(StrEnum):
    CONSENT = "consent"
    CANCEL = "cancel"


class OrderCallback(CallbackData, prefix="order"):
    """Кнопка заказа. order_id приходит от клиента: хендлер обязан проверить владельца."""

    action: OrderAction
    order_id: int


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_NEW_REPORT)],
            [KeyboardButton(text=texts.BTN_MY_ORDERS), KeyboardButton(text=texts.BTN_BALANCE)],
            [KeyboardButton(text=texts.BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def _order_button(text: str, action: OrderAction, order_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text, callback_data=OrderCallback(action=action, order_id=order_id).pack()
    )


def consent_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_order_button(texts.BTN_CONSENT, OrderAction.CONSENT, order_id)],
            [_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)],
        ]
    )


def cancel_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)]]
    )
