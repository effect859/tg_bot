"""Подтверждение плана отчёта: черновик плана → CONFIRMED, заказ → PAYMENT_PENDING.

Как подтверждение ТЗ (requirements_dialog.confirm_requirements): под блокировкой
заказа проверяются статус и отпечаток плана, который видел пользователь, затем
существующие операции версий и статусов. Подтверждение версии и переход
выполняются в одной транзакции; коммит — забота вызывающего кода.

Инвариант PAYMENT_PENDING (подтверждённый план на действующем подтверждённом ТЗ)
проверяет change_status.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderPlanVersion
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import InvalidStatusTransitionError, PlanChangedError, VersionNotFoundError
from app.domain.requirements_dialog import content_digest
from app.services.orders import change_status, confirm_plan_version, get_plan_versions, lock_order

S = OrderStatus


async def confirm_plan(session: AsyncSession, order_id: int, digest: str) -> OrderPlanVersion:
    """Подтверждение плана, который видел пользователь.

    ``digest`` — отпечаток содержимого плана с экрана: изменившийся с тех пор план
    не подтверждается.

    Raises InvalidStatusTransitionError (заказ не ждёт подтверждения плана),
    VersionNotFoundError (нет черновика плана), PlanChangedError, OutdatedVersionError
    (план построен не на действующем ТЗ).
    """
    order = await lock_order(session, order_id)
    if order.status is not S.WAITING_FOR_PLAN_CONFIRMATION:
        raise InvalidStatusTransitionError(order.status, S.PAYMENT_PENDING)
    draft = await _draft(session, order_id)
    if content_digest(draft.content) != digest:
        raise PlanChangedError(order_id)
    version = await confirm_plan_version(session, order_id, draft.id)
    await change_status(session, order_id, S.PAYMENT_PENDING)
    return version


async def _draft(session: AsyncSession, order_id: int) -> OrderPlanVersion:
    """Черновик плана — последняя версия, и она должна быть черновиком."""
    versions = await get_plan_versions(session, order_id)
    if not versions or versions[-1].status is not VersionStatus.DRAFT:
        raise VersionNotFoundError(f"Order {order_id} has no plan draft")
    return versions[-1]
