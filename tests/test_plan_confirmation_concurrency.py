"""Гонки подтверждения плана между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между двумя апдейтами кнопок.
После каждой гонки проверяется инвариант: подтверждённый план бывает только у
заказа, который прошёл через PAYMENT_PENDING.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Order, OrderPlanVersion
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import InvalidStatusTransitionError
from app.domain.requirements_dialog import content_digest
from app.services import orders
from app.services.plan_confirmation import confirm_plan
from app.services.plan_generation import generate_plan
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient
from plan_samples import GOOD_PLAN, preparing_order

S = OrderStatus
V = VersionStatus
Sessionmaker = async_sessionmaker[AsyncSession]
Operation = Callable[[AsyncSession], Awaitable[Any]]

VERSION_TABLES = ("order_plan_versions", "order_requirements_versions")
TEST_ORDERS = "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base"


@pytest.fixture
async def sessions(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление llm_calls и версий (в том числе подтверждённых).

    llm_calls не удаляются каскадом (FK SET NULL). Подтверждённые версии БД удалить
    не даёт (триггер неизменности): триггеры отключаются только внутри транзакции
    очистки — DDL в PostgreSQL транзакционный.
    """
    yield committed_sessionmaker
    base = {"base": COMMITTED_TG_ID_BASE}
    async with committed_sessionmaker() as session:
        await session.execute(
            text(f"DELETE FROM llm_calls WHERE order_id IN ({TEST_ORDERS})"), base
        )
        for table in VERSION_TABLES:
            trigger = f"trg_{table}_immutable_confirmed"
            await session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
            await session.execute(
                text(f"DELETE FROM {table} WHERE order_id IN ({TEST_ORDERS})"), base
            )
            await session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
        await session.commit()


async def _planned_order(sessions: Sessionmaker, offset: int) -> tuple[int, str]:
    async with sessions() as session:
        order_id = await preparing_order(session, tg_id=COMMITTED_TG_ID_BASE + offset)
        outcome = await generate_plan(session, FakeLLMClient(GOOD_PLAN), order_id)
        assert outcome.plan is not None
        return order_id, content_digest(outcome.plan)


async def _state(sessions: Sessionmaker, order_id: int) -> tuple[S, list[V]]:
    async with sessions() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
        plans = await session.scalars(
            select(OrderPlanVersion.status)
            .where(OrderPlanVersion.order_id == order_id)
            .order_by(OrderPlanVersion.version)
        )
        assert status is not None
        return status, list(plans)


def _confirm(order_id: int, digest: str) -> Operation:
    async def run(session: AsyncSession) -> object:
        return await confirm_plan(session, order_id, digest)

    return run


def _change(order_id: int, target: S) -> Operation:
    async def run(session: AsyncSession) -> object:
        return await orders.change_status(session, order_id, target)

    return run


async def _race(sessions: Sessionmaker, *operations: Operation, hold: float = 0.3) -> list[str]:
    """Операции стартуют вместе; победитель держит блокировку ``hold`` секунд до commit."""
    barrier = asyncio.Barrier(len(operations))

    async def run(operation: Operation) -> str:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            try:
                await operation(session)
            except InvalidStatusTransitionError:
                await session.rollback()
                return "refused"
            await asyncio.sleep(hold)
            await session.commit()
            return "done"

    return list(await asyncio.gather(*(run(op) for op in operations)))


async def test_two_simultaneous_confirmations_succeed_once(sessions: Sessionmaker) -> None:
    order_id, digest = await _planned_order(sessions, 101)

    results = await _race(sessions, _confirm(order_id, digest), _confirm(order_id, digest))

    assert sorted(results) == ["done", "refused"]
    assert await _state(sessions, order_id) == (S.PAYMENT_PENDING, [V.CONFIRMED])


async def test_confirmation_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id, digest = await _planned_order(sessions, 102)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        async with sessions() as contender:
            task = asyncio.create_task(confirm_plan(contender, order_id, digest))
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
            await holder.commit()
            await asyncio.wait_for(task, 10)
            await contender.commit()

    assert await _state(sessions, order_id) == (S.PAYMENT_PENDING, [V.CONFIRMED])


async def _sequential(sessions: Sessionmaker, first: Operation, second: Operation) -> list[str]:
    """Первая операция берёт блокировку и держит её, вторая стартует, пока та не закончилась."""
    results: list[str] = []
    started = asyncio.Event()

    async def run(operation: Operation, signal: bool) -> None:
        async with sessions() as session:
            try:
                await operation(session)
            except InvalidStatusTransitionError:
                await session.rollback()
                results.append("refused")
                return
            if signal:
                started.set()
                await asyncio.sleep(0.3)
            await session.commit()
            results.append("done")

    async def second_after_first() -> None:
        await asyncio.wait_for(started.wait(), 10)
        await run(second, signal=False)

    await asyncio.gather(run(first, signal=True), second_after_first())
    return results


async def test_cancel_then_confirm(sessions: Sessionmaker) -> None:
    order_id, digest = await _planned_order(sessions, 103)

    results = await _sequential(
        sessions, _change(order_id, S.CANCELLED), _confirm(order_id, digest)
    )

    assert results == ["done", "refused"]
    assert await _state(sessions, order_id) == (S.CANCELLED, [V.DRAFT])


async def test_confirm_then_cancel(sessions: Sessionmaker) -> None:
    order_id, digest = await _planned_order(sessions, 104)

    results = await _sequential(
        sessions, _confirm(order_id, digest), _change(order_id, S.CANCELLED)
    )

    assert results == ["done", "done"]
    assert await _state(sessions, order_id) == (S.CANCELLED, [V.CONFIRMED])


async def test_simultaneous_confirm_and_cancel_are_consistent(sessions: Sessionmaker) -> None:
    order_id, digest = await _planned_order(sessions, 105)

    await _race(sessions, _confirm(order_id, digest), _change(order_id, S.CANCELLED))

    # Любой порядок: отмена первой — план не подтверждён; подтверждение первым —
    # подтверждённый план и отмена из PAYMENT_PENDING.
    assert await _state(sessions, order_id) in [
        (S.CANCELLED, [V.DRAFT]),
        (S.CANCELLED, [V.CONFIRMED]),
    ]


async def test_confirm_against_other_transition_keeps_invariant(sessions: Sessionmaker) -> None:
    """Другой переход из WAITING_FOR_PLAN_CONFIRMATION (в карте есть, кнопки пока нет)."""
    order_id, digest = await _planned_order(sessions, 106)

    results = await _race(sessions, _confirm(order_id, digest), _change(order_id, S.PREPARING_PLAN))

    assert sorted(results) == ["done", "refused"]
    # Нет подтверждённого плана без PAYMENT_PENDING.
    assert await _state(sessions, order_id) in [
        (S.PAYMENT_PENDING, [V.CONFIRMED]),
        (S.PREPARING_PLAN, [V.DRAFT]),
    ]
