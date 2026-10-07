from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts
from app.db.models import Order
from app.domain.enums import OrderStatus
from app.domain.requirements_dialog import EDITABLE_FIELDS


class OrderAction(StrEnum):
    CONSENT = "consent"
    CANCEL = "cancel"
    FINISH_MATERIALS = "finish"
    RETRY_ANALYSIS = "retry_analysis"
    RETRY_PLAN = "retry_plan"


class OrderCallback(CallbackData, prefix="order"):
    """Кнопка заказа. order_id приходит от клиента: хендлер обязан проверить владельца."""

    action: OrderAction
    order_id: int


class RequirementsAction(StrEnum):
    CONFIRM = "confirm"
    EDIT = "edit"
    EDIT_FIELD = "field"


class RequirementsCallback(CallbackData, prefix="req"):
    """Кнопка уточнения ТЗ. Всё приходит от клиента: владельца, статус и поле
    проверяет хендлер и сервис.

    ``digest`` — отпечаток ТЗ на экране подтверждения: «Подтвердить» под устаревшим
    резюме не подтвердит изменившееся с тех пор ТЗ.
    """

    action: RequirementsAction
    order_id: int
    field: str = ""
    digest: str = ""


class PlanAction(StrEnum):
    CONFIRM = "confirm"


class PlanCallback(CallbackData, prefix="plan"):
    """Кнопка экрана плана. Всё приходит от клиента: владельца и статус проверяет
    хендлер и сервис.

    ``digest`` — отпечаток плана на экране: «Подтвердить» под устаревшим планом не
    подтвердит изменившийся с тех пор план.
    """

    action: PlanAction
    order_id: int
    digest: str = ""


class ReportCallback(CallbackData, prefix="report"):
    """Кнопка «Скачать отчёт». order_id приходит от клиента: владельца и статус
    проверяет хендлер."""

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


def orders_keyboard(orders: list[Order]) -> InlineKeyboardMarkup | None:
    """«Мои заказы»: кнопка скачивания для каждого готового заказа; нет готовых — None."""
    rows = [
        [
            InlineKeyboardButton(
                text=texts.download_report_button(order.id),
                callback_data=ReportCallback(order_id=order.id).pack(),
            )
        ]
        for order in orders
        if order.status is OrderStatus.COMPLETED
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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


def plan_failed_keyboard(order_id: int, *, can_retry: bool) -> InlineKeyboardMarkup:
    """Сбой составления плана: повторить (пока не исчерпан лимит) или отменить заказ."""
    rows = [[_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)]]
    if can_retry:
        rows.insert(0, [_order_button(texts.BTN_RETRY_PLAN, OrderAction.RETRY_PLAN, order_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_confirmation_keyboard(order_id: int, digest: str) -> InlineKeyboardMarkup:
    """Экран плана: подтвердить план или отменить заказ."""
    confirm = PlanCallback(action=PlanAction.CONFIRM, order_id=order_id, digest=digest)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_CONFIRM_PLAN, callback_data=confirm.pack())],
            [_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)],
        ]
    )


def _requirements_button(
    text: str, action: RequirementsAction, order_id: int, **kwargs: str
) -> InlineKeyboardButton:
    data = RequirementsCallback(action=action, order_id=order_id, **kwargs)
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def confirmation_keyboard(order_id: int, digest: str) -> InlineKeyboardMarkup:
    """Экран подтверждения ТЗ: подтвердить, изменить обязательное поле или отменить заказ."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _requirements_button(
                    texts.BTN_CONFIRM_REQUIREMENTS,
                    RequirementsAction.CONFIRM,
                    order_id,
                    digest=digest,
                )
            ],
            [_requirements_button(texts.BTN_EDIT_REQUIREMENTS, RequirementsAction.EDIT, order_id)],
            [_order_button(texts.BTN_CANCEL_ORDER, OrderAction.CANCEL, order_id)],
        ]
    )


def edit_fields_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Обязательные поля, которые можно изменить; обе даты — одной кнопкой."""
    rows = []
    for field in EDITABLE_FIELDS:
        if field == "practice_end":
            continue  # меняется вместе с датой начала
        label = texts.BTN_EDIT_DATES if field == "practice_start" else texts.field_label(field)
        rows.append(
            [_requirements_button(label, RequirementsAction.EDIT_FIELD, order_id, field=field)]
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
