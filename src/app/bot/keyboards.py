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
    FINISH_MATERIALS = "finish"
    RETRY_ANALYSIS = "retry_analysis"


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


def analysis_failed_keyboard(order_id: int, *, can_retry: bool) -> InlineKeyboardMarkup:
    """Сбой анализа: повторить (пока не исчерпан лимит) или отменить заказ."""
    rows = [[_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)]]
    if can_retry:
        rows.insert(
            0, [_order_button(texts.BTN_RETRY_ANALYSIS, OrderAction.RETRY_ANALYSIS, order_id)]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def materials_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Шаг загрузки, когда файлы уже есть: завершить приём или отменить заказ."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_order_button(texts.BTN_FINISH_MATERIALS, OrderAction.FINISH_MATERIALS, order_id)],
            [_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)],
        ]
    )
