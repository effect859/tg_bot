"""Подтверждение плана (сервис): CONFIRMED + PAYMENT_PENDING, отпечаток, отказы, атомарность.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_plan_confirmation_concurrency.py.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderPlanVersion, OrderRequirementsVersion
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    OutdatedVersionError,
    PlanChangedError,
    VersionNotFoundError,
)
from app.domain.requirements_dialog import content_digest
from app.services import orders
from app.services import plan_confirmation as service
from app.services.orders import _require_confirmed_plan
from app.services.plan_confirmation import confirm_plan
from app.services.plan_generation import generate_plan
from fake_llm import FakeLLMClient
from plan_samples import GOOD_PLAN, REQUIREMENTS, preparing_order

S = OrderStatus


async def _planned_order(session: AsyncSession, tg_id: int = 1) -> int:
    """Заказ в WAITING_FOR_PLAN_CONFIRMATION с черновиком плана v1 (зафиксировано)."""
    order_id = await preparing_order(session, tg_id=tg_id)
    await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)
    return order_id


async def _fresh(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _plans(session: AsyncSession, order_id: int) -> list[OrderPlanVersion]:
    stmt = (
        select(OrderPlanVersion)
        .where(OrderPlanVersion.order_id == order_id)
        .order_by(OrderPlanVersion.version)
    )
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _digest(session: AsyncSession, order_id: int) -> str:
    [plan] = await _plans(session, order_id)
    return content_digest(plan.content)


async def _set_status(session: AsyncSession, order_id: int, status: S) -> None:
    failed_from = S.PREPARING_PLAN if status is S.FAILED else None
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(status=status, failed_from_status=failed_from)
    )
    await session.commit()


# --- Успех ----------------------------------------------------------------------------


async def test_confirm_plan_confirms_draft_and_moves_to_payment_pending(
    session: AsyncSession,
) -> None:
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)

    version = await confirm_plan(session, order_id, digest)
    await session.commit()

    [plan] = await _plans(session, order_id)
    assert plan.id == version.id
    assert (plan.version, plan.status) == (1, VersionStatus.CONFIRMED)
    assert plan.confirmed_at is not None
    [requirements] = await orders.get_requirements_versions(session, order_id)
    assert requirements.status is VersionStatus.CONFIRMED
    assert plan.requirements_version_id == requirements.id
    assert (await _fresh(session, order_id)).status is S.PAYMENT_PENDING
    # Инвариант PAYMENT_PENDING: подтверждённый план на действующем подтверждённом ТЗ.
    assert (await _require_confirmed_plan(session, order_id)).id == plan.id


async def test_confirmed_plan_is_immutable(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    await confirm_plan(session, order_id, await _digest(session, order_id))
    await session.commit()
    [plan] = await _plans(session, order_id)

    with pytest.raises(IntegrityError, match="immutable"):
        await session.execute(
            update(OrderPlanVersion)
            .where(OrderPlanVersion.id == plan.id)
            .values(content={"sections": [{"title": "Другой"}]})
        )
    await session.rollback()


async def test_digest_of_shown_plan_matches_stored_plan(session: AsyncSession) -> None:
    """Кнопка строится из исхода генерации; подтверждение сверяет с сохранённым JSONB."""
    order_id = await preparing_order(session)
    outcome = await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)

    assert outcome.plan is not None
    assert content_digest(outcome.plan) == await _digest(session, order_id)


async def test_cancel_after_confirmation(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    await confirm_plan(session, order_id, await _digest(session, order_id))
    await session.commit()

    await orders.change_status(session, order_id, S.CANCELLED)
    await session.commit()

    assert (await _fresh(session, order_id)).status is S.CANCELLED
    [plan] = await _plans(session, order_id)
    assert plan.status is VersionStatus.CONFIRMED


# --- Отказы ---------------------------------------------------------------------------


async def _assert_unchanged(session: AsyncSession, order_id: int, status: S) -> None:
    assert (await _fresh(session, order_id)).status is status
    assert [p.status for p in await _plans(session, order_id)] == [VersionStatus.DRAFT]


async def test_changed_plan_is_not_confirmed(session: AsyncSession) -> None:
    order_id = await _planned_order(session)

    with pytest.raises(PlanChangedError):
        await confirm_plan(session, order_id, content_digest({"sections": []}))
    await session.rollback()

    await _assert_unchanged(session, order_id, S.WAITING_FOR_PLAN_CONFIRMATION)


async def test_empty_digest_is_not_accepted(session: AsyncSession) -> None:
    order_id = await _planned_order(session)

    with pytest.raises(PlanChangedError):
        await confirm_plan(session, order_id, "")
    await session.rollback()


async def test_missing_draft(session: AsyncSession) -> None:
    order_id = await preparing_order(session)
    # Нарушение целостности: шаг подтверждения плана без черновика.
    await _set_status(session, order_id, S.WAITING_FOR_PLAN_CONFIRMATION)

    with pytest.raises(VersionNotFoundError):
        await confirm_plan(session, order_id, "x" * 12)
    await session.rollback()

    assert (await _fresh(session, order_id)).status is S.WAITING_FOR_PLAN_CONFIRMATION


@pytest.mark.parametrize("status", [S.PREPARING_PLAN, S.CANCELLED, S.FAILED])
async def test_wrong_status(session: AsyncSession, status: S) -> None:
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    if status is S.CANCELLED:
        await orders.change_status(session, order_id, S.CANCELLED)
        await session.commit()
    else:
        await _set_status(session, order_id, status)

    with pytest.raises(InvalidStatusTransitionError):
        await confirm_plan(session, order_id, digest)
    await session.rollback()

    await _assert_unchanged(session, order_id, status)


async def test_repeated_confirmation_is_refused(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    await confirm_plan(session, order_id, digest)
    await session.commit()

    with pytest.raises(InvalidStatusTransitionError):
        await confirm_plan(session, order_id, digest)
    await session.rollback()

    assert len(await _plans(session, order_id)) == 1
    assert (await _fresh(session, order_id)).status is S.PAYMENT_PENDING


async def test_plan_on_outdated_requirements_is_refused(session: AsyncSession) -> None:
    order_id = await _planned_order(session)
    # Действующее ТЗ новее того, на котором построен план (через UI сейчас недостижимо).
    session.add(
        OrderRequirementsVersion(
            order_id=order_id,
            version=2,
            status=VersionStatus.CONFIRMED,
            content=REQUIREMENTS,
            confirmed_at=func.now(),
        )
    )
    await session.commit()

    with pytest.raises(OutdatedVersionError):
        await confirm_plan(session, order_id, await _digest(session, order_id))
    await session.rollback()

    await _assert_unchanged(session, order_id, S.WAITING_FOR_PLAN_CONFIRMATION)


async def test_failed_transition_rolls_back_confirmation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Подтверждение версии и переход — одна транзакция: сбой перехода откатывает оба."""
    order_id = await _planned_order(session)
    digest = await _digest(session, order_id)
    monkeypatch.setattr(service, "change_status", AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await confirm_plan(session, order_id, digest)
    await session.rollback()

    await _assert_unchanged(session, order_id, S.WAITING_FOR_PLAN_CONFIRMATION)
