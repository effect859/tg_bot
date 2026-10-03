"""Гонки анализа материалов между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между апдейтом кнопки и проходом
восстановления или двумя экземплярами бота. LLM — подмена с паузой.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMCall, Order, OrderMaterialText, OrderRequirementsVersion
from app.domain.enums import OrderStatus
from app.domain.errors import InvalidStatusTransitionError
from app.llm.base import LLMPermanentError
from app.services import order_materials as materials
from app.services import orders
from app.services.requirements_analysis import (
    AnalysisOutcomeKind,
    analyze_requirements,
    restart_analysis,
)
from app.services.users import register_or_update_user
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient, response_json, result

S = OrderStatus
K = AnalysisOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]
MATERIAL = "Тема: Анализ бизнес-процессов отдела продаж."
GOOD = result(response_json(topic=("Анализ бизнес-процессов", "Тема: Анализ бизнес-процессов")))


@pytest.fixture
async def sessions(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление llm_calls тестовых заказов (FK SET NULL их не удалит)."""
    yield committed_sessionmaker
    async with committed_sessionmaker() as session:
        orders_of_test_users = (
            "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base"
        )
        await session.execute(
            text(f"DELETE FROM llm_calls WHERE order_id IN ({orders_of_test_users})"),
            {"base": COMMITTED_TG_ID_BASE},
        )
        await session.commit()


class SlowLLM(FakeLLMClient):
    def __init__(self, *answers: object, delay: float = 0.3) -> None:
        super().__init__(*answers)  # type: ignore[arg-type]
        self.delay = delay

    async def complete(self, **kwargs: object) -> object:  # type: ignore[override]
        await asyncio.sleep(self.delay)
        return await super().complete(**kwargs)  # type: ignore[arg-type]


async def _analyzing_order(sessionmaker: Sessionmaker, tg_id: int) -> int:
    async with sessionmaker() as session:
        user = await register_or_update_user(
            session, tg_id=tg_id, username=None, first_name=None, language_code=None
        )
        order = await orders.create_order(session, user_id=user.id)
        for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
            order = await orders.change_status(session, order.id, status)
        material = await materials.add_material(
            session, order.id, filename="t.txt", mime_type=None, size_bytes=10
        )
        await materials.mark_processing(session, material.id)
        await materials.mark_processed(session, material.id)
        session.add(
            OrderMaterialText(
                material_id=material.id, text=MATERIAL, char_count=len(MATERIAL), extractor="t"
            )
        )
        await session.flush()
        await materials.finish_materials(session, order.id)
        order_id = order.id
        await session.commit()
    return order_id


async def _counts(sessionmaker: Sessionmaker, order_id: int) -> tuple[S, int, int]:
    async with sessionmaker() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
        calls = await session.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.order_id == order_id)
        )
        drafts = await session.scalar(
            select(func.count())
            .select_from(OrderRequirementsVersion)
            .where(OrderRequirementsVersion.order_id == order_id)
        )
        assert status is not None
        return status, int(calls or 0), int(drafts or 0)


async def test_two_simultaneous_analyses_call_llm_once(sessions: Sessionmaker) -> None:
    """Апдейт кнопки и проход восстановления (или два экземпляра) одновременно."""
    order_id = await _analyzing_order(sessions, COMMITTED_TG_ID_BASE + 21)
    first, second = SlowLLM(GOOD), SlowLLM(GOOD)
    barrier = asyncio.Barrier(2)

    async def run(llm: SlowLLM) -> K:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await analyze_requirements(session, llm, order_id)).kind

    kinds = await asyncio.gather(run(first), run(second))

    assert sorted(kinds) == sorted([K.COMPLETED, K.BUSY])
    assert len(first.calls) + len(second.calls) == 1
    assert await _counts(sessions, order_id) == (S.WAITING_FOR_MISSING_DATA, 1, 1)


async def test_claim_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id = await _analyzing_order(sessions, COMMITTED_TG_ID_BASE + 22)
    llm = FakeLLMClient(GOOD)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        async with sessions() as contender:
            task = asyncio.create_task(analyze_requirements(contender, llm, order_id))
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
            assert llm.calls == []
            await holder.commit()
            outcome = await asyncio.wait_for(task, 10)

    assert outcome.kind is K.COMPLETED


async def test_lock_is_not_held_during_llm_call(sessions: Sessionmaker) -> None:
    """Пока идёт вызов модели, заказ можно заблокировать из другой сессии (и отменить)."""
    order_id = await _analyzing_order(sessions, COMMITTED_TG_ID_BASE + 23)
    llm = FakeLLMClient(GOOD)

    async def cancel_from_other_session() -> None:
        async with sessions() as other:
            await asyncio.wait_for(orders.change_status(other, order_id, S.CANCELLED), 5)
            await other.commit()

    llm.on_complete = cancel_from_other_session
    async with sessions() as session:
        outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    assert await _counts(sessions, order_id) == (S.CANCELLED, 1, 0)


async def test_double_restart_press_runs_once(sessions: Sessionmaker) -> None:
    order_id = await _analyzing_order(sessions, COMMITTED_TG_ID_BASE + 24)
    async with sessions() as session:
        await analyze_requirements(session, FakeLLMClient(LLMPermanentError("http_400")), order_id)
    barrier = asyncio.Barrier(2)

    async def press() -> str:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            try:
                await restart_analysis(session, order_id)
            except InvalidStatusTransitionError:
                await session.rollback()
                return "refused"
            await asyncio.sleep(0.3)  # держим блокировку: второе нажатие точно ждёт
            await session.commit()
            return "restarted"

    results = await asyncio.gather(press(), press())

    assert sorted(results) == ["refused", "restarted"]
    assert (await _counts(sessions, order_id))[0] is S.ANALYZING_MATERIALS
