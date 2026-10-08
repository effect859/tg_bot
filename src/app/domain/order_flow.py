"""Карта статусов заказа: допустимые переходы, активные и терминальные статусы.

Модуль чистый (без БД). Проверки, которым нужны данные — подтверждено ли ТЗ,
подтверждён ли план, — выполняет app.services.orders.
"""

from app.domain.enums import OrderStatus
from app.domain.errors import InvalidStatusTransitionError

S = OrderStatus

# Статусы до оплаты: здесь можно менять ТЗ и план и отменять заказ.
PRE_PAYMENT_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        S.NEW,
        S.WAITING_FOR_CONSENT,
        S.WAITING_FOR_FILES,
        S.ANALYZING_MATERIALS,
        S.WAITING_FOR_MISSING_DATA,
        S.WAITING_FOR_DATA_CONFIRMATION,
        S.PREPARING_PLAN,
        S.WAITING_FOR_PLAN_CONFIRMATION,
        S.PAYMENT_PENDING,
    }
)

# Автоматические шаги, которые могут завершиться сбоем.
FAILABLE_STATUSES: frozenset[OrderStatus] = frozenset(
    {S.ANALYZING_MATERIALS, S.PREPARING_PLAN, S.GENERATING, S.REGENERATING}
)

# Неактивный заказ не мешает создать новый. COMPLETED не терминален:
# ревизия (COMPLETED -> REVISION_REQUESTED) снова делает заказ активным.
# Список продублирован в условии индекса uq_orders_user_id_active:
# при изменении нужна миграция, пересоздающая индекс.
INACTIVE_ORDER_STATUSES: frozenset[OrderStatus] = frozenset({S.COMPLETED, S.CANCELLED})

TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset({S.CANCELLED})

# Статические переходы. FAILED здесь нет: из него можно вернуться только в статус,
# на котором произошёл сбой (failed_from_status), — см. allowed_transitions().
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.NEW: frozenset({S.WAITING_FOR_CONSENT, S.CANCELLED}),
    S.WAITING_FOR_CONSENT: frozenset({S.WAITING_FOR_FILES, S.CANCELLED}),
    S.WAITING_FOR_FILES: frozenset({S.ANALYZING_MATERIALS, S.CANCELLED}),
    S.ANALYZING_MATERIALS: frozenset(
        {S.WAITING_FOR_MISSING_DATA, S.WAITING_FOR_DATA_CONFIRMATION, S.FAILED, S.CANCELLED}
    ),
    S.WAITING_FOR_MISSING_DATA: frozenset(
        {S.WAITING_FOR_DATA_CONFIRMATION, S.ANALYZING_MATERIALS, S.CANCELLED}
    ),
    S.WAITING_FOR_DATA_CONFIRMATION: frozenset(
        {S.PREPARING_PLAN, S.WAITING_FOR_MISSING_DATA, S.CANCELLED}
    ),
    S.PREPARING_PLAN: frozenset({S.WAITING_FOR_PLAN_CONFIRMATION, S.FAILED, S.CANCELLED}),
    S.WAITING_FOR_PLAN_CONFIRMATION: frozenset(
        {S.PAYMENT_PENDING, S.PREPARING_PLAN, S.WAITING_FOR_DATA_CONFIRMATION, S.CANCELLED}
    ),
    S.PAYMENT_PENDING: frozenset({S.PAID, S.WAITING_FOR_PLAN_CONFIRMATION, S.CANCELLED}),
    # Отмена после оплаты требует возврата денег — это отдельный будущий этап.
    S.PAID: frozenset({S.GENERATING}),
    S.GENERATING: frozenset({S.COMPLETED, S.FAILED}),
    S.COMPLETED: frozenset({S.REVISION_REQUESTED}),
    S.REVISION_REQUESTED: frozenset({S.REGENERATING, S.COMPLETED}),
    S.REGENERATING: frozenset({S.COMPLETED, S.FAILED}),
    S.CANCELLED: frozenset(),
}


def allowed_transitions(
    current: OrderStatus, failed_from: OrderStatus | None = None
) -> frozenset[OrderStatus]:
    if current is not S.FAILED:
        return TRANSITIONS[current]
    if failed_from is None:
        return frozenset()
    # Повтор — только в исходный шаг: сбой до оплаты не открывает путь к генерации.
    targets = {failed_from}
    if failed_from in PRE_PAYMENT_STATUSES:
        targets.add(S.CANCELLED)
    elif failed_from is S.GENERATING:
        # Сбой генерации после оплаты: отмена — только вместе с возвратом кредитов
        # (это проверяет services.orders.change_status, возврат делает
        # services.billing.refund_failed_order).
        targets.add(S.CANCELLED)
    return frozenset(targets)


def check_transition(
    current: OrderStatus, target: OrderStatus, failed_from: OrderStatus | None = None
) -> None:
    if target not in allowed_transitions(current, failed_from):
        raise InvalidStatusTransitionError(current, target)


def is_active(status: OrderStatus) -> bool:
    return status not in INACTIVE_ORDER_STATUSES
