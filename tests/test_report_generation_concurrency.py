"""Гонки генерации отчёта между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между двумя экземплярами бота.
LLM — подмена с паузой.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMCall, Order, OrderPlanVersion, OrderReportSection
from app.domain.enums import LLMCallStatus, OrderStatus
from app.domain.report_generation import ReportOutcomeKind
from app.services import orders
from app.services import report_generation as service
from app.services.report_generation import generate_next_section
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient
from report_samples import generating_order, good

S = OrderStatus
K = ReportOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]

VERSION_TABLES = ("order_plan_versions", "order_requirements_versions")
TEST_ORDERS = "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base"


@pytest.fixture
async def sessions(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление разделов, llm_calls и версий тестовых заказов.

    llm_calls не удаляются каскадом (FK SET NULL). Разделы ссылаются на версии плана
    и удаляются раньше них. Подтверждённые версии БД удалить не даёт (триггер
    неизменности): триггеры отключаются только внутри транзакции очистки.
    """
    yield committed_sessionmaker
    base = {"base": COMMITTED_TG_ID_BASE}
    async with committed_sessionmaker() as session:
        for table in ("order_report_sections", "llm_calls"):
            await session.execute(
                text(f"DELETE FROM {table} WHERE order_id IN ({TEST_ORDERS})"), base
            )
        for table in VERSION_TABLES:
            trigger = f"trg_{table}_immutable_confirmed"
            await session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
            await session.execute(
                text(f"DELETE FROM {table} WHERE order_id IN ({TEST_ORDERS})"), base
            )
            await session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
        await session.commit()


class SlowLLM(FakeLLMClient):
    def __init__(self, *answers: object, delay: float = 0.3) -> None:
        super().__init__(*answers)  # type: ignore[arg-type]
        self.delay = delay

    async def complete(self, **kwargs: object) -> object:  # type: ignore[override]
        await asyncio.sleep(self.delay)
        return await super().complete(**kwargs)  # type: ignore[arg-type]


async def _order(sessions: Sessionmaker, offset: int) -> int:
    async with sessions() as session:
        return await generating_order(session, tg_id=COMMITTED_TG_ID_BASE + offset)


async def _counts(sessions: Sessionmaker, order_id: int) -> tuple[int, int]:
    async with sessions() as session:
        calls = await session.scalar(
            select(func.count())
            .select_from(LLMCall)
            .where(LLMCall.order_id == order_id, LLMCall.step == service.LLM_STEP)
        )
        sections = await session.scalar(
            select(func.count())
            .select_from(OrderReportSection)
            .where(OrderReportSection.order_id == order_id)
        )
        return int(calls or 0), int(sections or 0)


async def test_two_workers_generate_section_once(sessions: Sessionmaker) -> None:
    order_id = await _order(sessions, 201)
    first, second = SlowLLM(good(0)), SlowLLM(good(0))
    barrier = asyncio.Barrier(2)

    async def run(llm: SlowLLM) -> K:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await generate_next_section(session, llm, order_id)).kind

    kinds = await asyncio.gather(run(first), run(second))

    assert sorted(kinds) == sorted([K.SAVED, K.BUSY])
    assert len(first.calls) + len(second.calls) == 1
    assert await _counts(sessions, order_id) == (1, 1)


async def test_claim_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id = await _order(sessions, 202)
    llm = FakeLLMClient(good(0))

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        async with sessions() as contender:
            task = asyncio.create_task(generate_next_section(contender, llm, order_id))
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
            assert llm.calls == []
            await holder.commit()
            outcome = await asyncio.wait_for(task, 10)

    assert outcome.kind is K.SAVED


async def test_lock_is_not_held_during_llm_call(sessions: Sessionmaker) -> None:
    """Пока идёт вызов модели, заказ можно заблокировать из другой сессии."""
    order_id = await _order(sessions, 203)
    llm = FakeLLMClient(good(0))

    async def lock_from_other_session() -> None:
        async with sessions() as other:
            await asyncio.wait_for(orders.lock_order(other, order_id), 5)
            await other.commit()

    llm.on_complete = lock_from_other_session
    async with sessions() as session:
        outcome = await generate_next_section(session, llm, order_id)

    assert outcome.kind is K.SAVED


async def test_takeover_during_call_discards_result(sessions: Sessionmaker) -> None:
    """Попытку признали зависшей и перехватили во время вызова: дубля раздела нет."""
    order_id = await _order(sessions, 204)
    llm = FakeLLMClient(good(0))

    async def take_over() -> None:
        async with sessions() as other:
            await other.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(step_attempts=Order.step_attempts + 1)
            )
            await other.commit()

    llm.on_complete = take_over
    async with sessions() as session:
        outcome = await generate_next_section(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    assert await _counts(sessions, order_id) == (1, 0)


async def test_unique_constraint_blocks_duplicate_section(sessions: Sessionmaker) -> None:
    order_id = await _order(sessions, 205)
    async with sessions() as session:
        await generate_next_section(session, FakeLLMClient(good(0)), order_id)
        plan_id = await session.scalar(
            select(OrderPlanVersion.id).where(OrderPlanVersion.order_id == order_id)
        )
        session.add(
            OrderReportSection(
                order_id=order_id,
                plan_version_id=plan_id,
                section_index=0,
                content={"paragraphs": ["x"], "subsections": []},
                char_count=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    assert await _counts(sessions, order_id) == (1, 1)


async def test_concurrent_claims_do_not_exceed_report_budget(sessions: Sessionmaker) -> None:
    """Остался один вызов из 3N: одновременные захваты не расходуют больше."""
    order_id = await _order(sessions, 206)
    async with sessions() as session:
        session.add_all(
            LLMCall(
                order_id=order_id,
                step=service.LLM_STEP,
                provider="fake",
                model="m",
                status=LLMCallStatus.ERROR,
                error="http_500",
            )
            for _ in range(11)  # 4 раздела × 3 = 12
        )
        await session.commit()
    llms = [SlowLLM(good(0)) for _ in range(4)]
    barrier = asyncio.Barrier(len(llms))

    async def run(llm: SlowLLM) -> K:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await generate_next_section(session, llm, order_id)).kind

    kinds = await asyncio.gather(*(run(llm) for llm in llms))

    assert sum(len(llm.calls) for llm in llms) == 1
    assert kinds.count(K.SAVED) == 1
    assert set(kinds) <= {K.SAVED, K.BUSY, K.FAILED, K.SKIPPED}
    calls, _ = await _counts(sessions, order_id)
    assert calls == 12
