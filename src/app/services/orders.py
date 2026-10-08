"""Доменные операции заказа: статусы и версии ТЗ/плана.

Каждая операция, меняющая заказ, блокирует его строку (SELECT ... FOR UPDATE):
параллельные изменения одного заказа выполняются последовательно. Коммит —
забота вызывающего кода.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BalanceTransaction, Order, OrderPlanVersion, OrderRequirementsVersion
from app.domain.billing import order_price
from app.domain.enums import DocType, OrderStatus, TransactionReason, VersionStatus
from app.domain.errors import (
    ActiveOrderExistsError,
    DraftVersionExistsError,
    OrderNotEditableError,
    OrderNotFoundError,
    OrderNotPaidError,
    OrderNotRefundedError,
    OutdatedVersionError,
    PlanNotConfirmedError,
    RequirementsNotConfirmedError,
    VersionAlreadyConfirmedError,
    VersionNotFoundError,
)
from app.domain.order_flow import (
    FAILABLE_STATUSES,
    INACTIVE_ORDER_STATUSES,
    PRE_PAYMENT_STATUSES,
    check_transition,
)
from app.domain.schemas import REQUIREMENTS_SCHEMAS, PlanContent

ACTIVE_ORDER_INDEX = "uq_orders_user_id_active"

Content = Mapping[str, Any] | BaseModel


# --- Заказ ---------------------------------------------------------------------------


async def get_active_order(session: AsyncSession, user_id: int) -> Order | None:
    stmt = select(Order).where(
        Order.user_id == user_id, Order.status.not_in(INACTIVE_ORDER_STATUSES)
    )
    return await session.scalar(stmt)


async def can_create_order(session: AsyncSession, user_id: int) -> bool:
    return await get_active_order(session, user_id) is None


async def get_user_order(session: AsyncSession, user_id: int, order_id: int) -> Order:
    """Заказ пользователя. Чужой и несуществующий заказ неразличимы: OrderNotFoundError."""
    order = await session.get(Order, order_id, populate_existing=True)
    if order is None or order.user_id != user_id:
        raise OrderNotFoundError(order_id)
    return order


async def lock_order(session: AsyncSession, order_id: int) -> Order:
    """Блокирует строку заказа до конца транзакции: изменения заказа идут по очереди."""
    stmt = (
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    order = await session.scalar(stmt)
    if order is None:
        raise OrderNotFoundError(order_id)
    return order


async def create_order(
    session: AsyncSession, *, user_id: int, doc_type: DocType = DocType.PRACTICE_REPORT
) -> Order:
    # Проверка — ради понятной ошибки; гарантию при гонке даёт уникальный индекс.
    if not await can_create_order(session, user_id):
        raise ActiveOrderExistsError(user_id)
    order = Order(user_id=user_id, doc_type=doc_type, status=OrderStatus.NEW)
    async with _guard_active_order(user_id), session.begin_nested():
        session.add(order)
    await session.refresh(order)
    return order


async def change_status(
    session: AsyncSession, order_id: int, target: OrderStatus, *, error: str | None = None
) -> Order:
    """Переводит заказ в target, проверяя карту переходов и доменные условия."""
    order = await lock_order(session, order_id)
    check_transition(order.status, target, order.failed_from_status)

    if target is OrderStatus.PREPARING_PLAN:
        await _require_confirmed_requirements(session, order_id)
    elif target in (OrderStatus.PAYMENT_PENDING, OrderStatus.PAID):
        await _require_confirmed_plan(session, order_id)
    if target is OrderStatus.PAID:
        # Оплата — только списанием кредитов (services.billing.pay_order).
        await _require_order_charge(session, order_id)
    if (
        target is OrderStatus.CANCELLED
        and order.status is OrderStatus.FAILED
        and order.failed_from_status not in PRE_PAYMENT_STATUSES
    ):
        # Отмена после оплаты — только с возвратом кредитов
        # (services.billing.refund_failed_order, в той же транзакции).
        await _require_order_refund(session, order_id)

    async with _guard_active_order(order.user_id), session.begin_nested():
        if target is OrderStatus.FAILED:
            order.failed_from_status = order.status
            order.error = error
        elif order.status is OrderStatus.FAILED:
            # Повтор шага: сбой больше не актуален.
            order.failed_from_status = None
            order.error = None
        # Согласие — только переход к приёму файлов; отмена на этом шаге его не даёт.
        if (
            order.status is OrderStatus.WAITING_FOR_CONSENT
            and target is OrderStatus.WAITING_FOR_FILES
        ):
            order.consent_given_at = func.now()
        # Цена в кредитах фиксируется при первом входе в оплату и больше не
        # пересчитывается: изменение прайса не меняет оформленный заказ.
        if target is OrderStatus.PAYMENT_PENDING and order.price is None:
            order.price = order_price(order.doc_type)
        if target is OrderStatus.COMPLETED:
            order.completed_at = func.now()
        elif target is OrderStatus.CANCELLED:
            order.cancelled_at = func.now()
        # Смена статуса завершает текущую попытку автоматического шага; вход в шаг
        # (в том числе повтор из FAILED) начинает счёт попыток заново. В FAILED счётчик
        # сохраняется — для диагностики.
        order.step_started_at = None
        if target in FAILABLE_STATUSES:
            order.step_attempts = 0
        order.status = target
    await session.refresh(order)
    return order


# --- Версии ТЗ -----------------------------------------------------------------------


async def has_confirmed_requirements(session: AsyncSession, order_id: int) -> bool:
    """Есть ли у заказа хотя бы одна подтверждённая версия ТЗ (любая, не только последняя)."""
    stmt = select(
        exists().where(
            OrderRequirementsVersion.order_id == order_id,
            OrderRequirementsVersion.status == VersionStatus.CONFIRMED,
        )
    )
    return bool(await session.scalar(stmt))


async def get_requirements_versions(
    session: AsyncSession, order_id: int
) -> list[OrderRequirementsVersion]:
    stmt = (
        select(OrderRequirementsVersion)
        .where(OrderRequirementsVersion.order_id == order_id)
        .order_by(OrderRequirementsVersion.version)
    )
    return list(await session.scalars(stmt))


async def create_requirements_version(
    session: AsyncSession, order_id: int, content: Content
) -> OrderRequirementsVersion:
    """Новый черновик ТЗ. Подтверждённые версии остаются нетронутыми."""
    order = await _lock_editable_order(session, order_id)
    latest = await _latest(session, OrderRequirementsVersion, order_id)
    if latest is not None and latest.status is VersionStatus.DRAFT:
        raise DraftVersionExistsError(f"Order {order_id} already has requirements draft")
    version = OrderRequirementsVersion(
        order_id=order_id,
        version=_next_number(latest),
        status=VersionStatus.DRAFT,
        content=_validate_requirements(order.doc_type, content),
    )
    session.add(version)
    await session.flush()
    await session.refresh(version)
    return version


async def update_requirements_draft(
    session: AsyncSession, order_id: int, version_id: int, content: Content
) -> OrderRequirementsVersion:
    order = await _lock_editable_order(session, order_id)
    version = await _get_draft(session, OrderRequirementsVersion, order_id, version_id)
    version.content = _validate_requirements(order.doc_type, content)
    await session.flush()
    await session.refresh(version)
    return version


async def confirm_requirements_version(
    session: AsyncSession, order_id: int, version_id: int
) -> OrderRequirementsVersion:
    await _lock_editable_order(session, order_id)
    version = await _get_draft(session, OrderRequirementsVersion, order_id, version_id)
    return await _confirm(session, version)


# --- Версии плана --------------------------------------------------------------------


async def get_plan_versions(session: AsyncSession, order_id: int) -> list[OrderPlanVersion]:
    stmt = (
        select(OrderPlanVersion)
        .where(OrderPlanVersion.order_id == order_id)
        .order_by(OrderPlanVersion.version)
    )
    return list(await session.scalars(stmt))


async def create_plan_version(
    session: AsyncSession, order_id: int, content: Content
) -> OrderPlanVersion:
    """Новый черновик плана на основе действующего подтверждённого ТЗ."""
    await _lock_editable_order(session, order_id)
    requirements = await _require_confirmed_requirements(session, order_id)
    latest = await _latest(session, OrderPlanVersion, order_id)
    if latest is not None and latest.status is VersionStatus.DRAFT:
        raise DraftVersionExistsError(f"Order {order_id} already has plan draft")
    version = OrderPlanVersion(
        order_id=order_id,
        version=_next_number(latest),
        status=VersionStatus.DRAFT,
        content=_validate_plan(content),
        requirements_version_id=requirements.id,
    )
    session.add(version)
    await session.flush()
    await session.refresh(version)
    return version


async def update_plan_draft(
    session: AsyncSession, order_id: int, version_id: int, content: Content
) -> OrderPlanVersion:
    """Правка черновика плана; он переносится на действующее подтверждённое ТЗ."""
    await _lock_editable_order(session, order_id)
    version = await _get_draft(session, OrderPlanVersion, order_id, version_id)
    requirements = await _require_confirmed_requirements(session, order_id)
    version.content = _validate_plan(content)
    version.requirements_version_id = requirements.id
    await session.flush()
    await session.refresh(version)
    return version


async def confirm_plan_version(
    session: AsyncSession, order_id: int, version_id: int
) -> OrderPlanVersion:
    await _lock_editable_order(session, order_id)
    version = await _get_draft(session, OrderPlanVersion, order_id, version_id)
    requirements = await _require_confirmed_requirements(session, order_id)
    if version.requirements_version_id != requirements.id:
        raise OutdatedVersionError(
            f"Plan version {version.id} is based on outdated requirements; update the draft"
        )
    return await _confirm(session, version)


# --- Внутреннее ----------------------------------------------------------------------


@asynccontextmanager
async def _guard_active_order(user_id: int) -> AsyncIterator[None]:
    """Переводит нарушение uq_orders_user_id_active в доменную ошибку."""
    try:
        yield
    except IntegrityError as exc:
        if ACTIVE_ORDER_INDEX in str(exc.orig):
            raise ActiveOrderExistsError(user_id) from exc
        raise


async def _lock_editable_order(session: AsyncSession, order_id: int) -> Order:
    """ТЗ и план меняются только до оплаты: оплачивается конкретный план."""
    order = await lock_order(session, order_id)
    if order.status not in PRE_PAYMENT_STATUSES:
        raise OrderNotEditableError(order.status)
    return order


async def _latest[V: (OrderRequirementsVersion, OrderPlanVersion)](
    session: AsyncSession, model: type[V], order_id: int
) -> V | None:
    stmt = select(model).where(model.order_id == order_id).order_by(model.version.desc()).limit(1)
    return await session.scalar(stmt)


def _next_number(latest: OrderRequirementsVersion | OrderPlanVersion | None) -> int:
    return 1 if latest is None else latest.version + 1


async def _get_draft[V: (OrderRequirementsVersion, OrderPlanVersion)](
    session: AsyncSession, model: type[V], order_id: int, version_id: int
) -> V:
    version = await session.get(model, version_id, populate_existing=True)
    if version is None or version.order_id != order_id:
        raise VersionNotFoundError(f"{model.__tablename__} {version_id} of order {order_id}")
    if version.status is VersionStatus.CONFIRMED:
        raise VersionAlreadyConfirmedError(
            f"{model.__tablename__} {version_id} is confirmed; create a new version instead"
        )
    return version


async def _confirm[V: (OrderRequirementsVersion, OrderPlanVersion)](
    session: AsyncSession, version: V
) -> V:
    version.status = VersionStatus.CONFIRMED
    version.confirmed_at = func.now()
    await session.flush()
    await session.refresh(version)
    return version


async def _require_confirmed_requirements(
    session: AsyncSession, order_id: int
) -> OrderRequirementsVersion:
    """Действующее ТЗ — последняя версия, и она должна быть подтверждена."""
    latest = await _latest(session, OrderRequirementsVersion, order_id)
    if latest is None or latest.status is not VersionStatus.CONFIRMED:
        raise RequirementsNotConfirmedError(f"Order {order_id} has no confirmed requirements")
    return latest


async def _require_order_charge(session: AsyncSession, order_id: int) -> None:
    stmt = select(
        exists().where(
            BalanceTransaction.order_id == order_id,
            BalanceTransaction.reason == TransactionReason.ORDER_CHARGE,
        )
    )
    if not await session.scalar(stmt):
        raise OrderNotPaidError(order_id)


async def _require_order_refund(session: AsyncSession, order_id: int) -> None:
    stmt = select(
        exists().where(
            BalanceTransaction.order_id == order_id,
            BalanceTransaction.reason == TransactionReason.ORDER_REFUND,
        )
    )
    if not await session.scalar(stmt):
        raise OrderNotRefundedError(order_id)


async def _require_confirmed_plan(session: AsyncSession, order_id: int) -> OrderPlanVersion:
    """Действующий план — последняя версия: подтверждена и построена на действующем ТЗ."""
    requirements = await _latest(session, OrderRequirementsVersion, order_id)
    plan = await _latest(session, OrderPlanVersion, order_id)
    if (
        requirements is None
        or requirements.status is not VersionStatus.CONFIRMED
        or plan is None
        or plan.status is not VersionStatus.CONFIRMED
        or plan.requirements_version_id != requirements.id
    ):
        raise PlanNotConfirmedError(f"Order {order_id} has no confirmed plan")
    return plan


def _dump(content: Content) -> Any:
    return content.model_dump(mode="json") if isinstance(content, BaseModel) else content


def _validate_requirements(doc_type: DocType, content: Content) -> dict[str, Any]:
    schema = REQUIREMENTS_SCHEMAS[doc_type]
    return schema.model_validate(_dump(content)).model_dump(mode="json")


def _validate_plan(content: Content) -> dict[str, Any]:
    return PlanContent.model_validate(_dump(content)).model_dump(mode="json")
