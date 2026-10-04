"""Гонки составления плана между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между апдейтом кнопки и проходом
восстановления или двумя экземплярами бота. LLM — подмена с паузой.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMCall, Order, OrderPlanVersion
from app.domain.enums import LLMCallStatus, OrderStatus
from app.domain.errors import InvalidStatusTransitionError, PlanLimitReachedError
from app.domain.plan import MAX_PLAN_LLM_CALLS, PlanOutcomeKind
from app.llm.base import LLMPermanentError, LLMTemporaryError
from app.services import orders
from app.services import plan_generation as service
from app.services.plan_generation import generate_plan, restart_plan_generation
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient
from plan_samples import GOOD_PLAN, preparing_order

S = OrderStatus
K = PlanOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]


REQUIREMENTS_VERSIONS = "order_requirements_versions"
IMMUTABLE_TRIGGER = f"trg_{REQUIREMENTS_VERSIONS}_immutable_confirmed"
TEST_ORDERS = "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base"


@pytest.fixture
async def sessions(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление llm_calls, планов и подтверждённых ТЗ тестовых заказов.

    llm_calls не удаляются каскадом (FK SET NULL). Подтверждённую версию ТЗ БД удалить
    не даёт (триггер неизменности): триггер отключается только внутри транзакции
    очистки — DDL в PostgreSQL транзакционный.
    """
    yield committed_sessionmaker
    base = {"base": COMMITTED_TG_ID_BASE}
    async with committed_sessionmaker() as session:
        await session.execute(
            text(f"DELETE FROM llm_calls WHERE order_id IN ({TEST_ORDERS})"), base
        )
        await session.execute(
            text(f"DELETE FROM order_plan_versions WHERE order_id IN ({TEST_ORDERS})"), base
        )
        await session.execute(
            text(f"ALTER TABLE {REQUIREMENTS_VERSIONS} DISABLE TRIGGER {IMMUTABLE_TRIGGER}")
        )
        await session.execute(
            text(f"DELETE FROM {REQUIREMENTS_VERSIONS} WHERE order_id IN ({TEST_ORDERS})"), base
        )
        await session.execute(
            text(f"ALTER TABLE {REQUIREMENTS_VERSIONS} ENABLE TRIGGER {IMMUTABLE_TRIGGER}")
        )
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
        return await preparing_order(session, tg_id=COMMITTED_TG_ID_BASE + offset)


async def _counts(sessions: Sessionmaker, order_id: int) -> tuple[S, int, int]:
    async with sessions() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
        calls = await session.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.order_id == order_id)
        )
        plans = await session.scalar(
            select(func.count())
            .select_from(OrderPlanVersion)
            .where(OrderPlanVersion.order_id == order_id)
        )
        assert status is not None
        return status, int(calls or 0), int(plans or 0)


async def test_two_simultaneous_generations_call_llm_once(sessions: Sessionmaker) -> None:
    """Апдейт подтверждения ТЗ и проход восстановления (или два экземпляра) одновременно."""
    order_id = await _order(sessions, 91)
    first, second = SlowLLM(GOOD_PLAN), SlowLLM(GOOD_PLAN)
    barrier = asyncio.Barrier(2)

    async def run(llm: SlowLLM) -> K:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await generate_plan(session, llm, order_id)).kind

    kinds = await asyncio.gather(run(first), run(second))

    assert sorted(kinds) == sorted([K.COMPLETED, K.BUSY])
    assert len(first.calls) + len(second.calls) == 1
    assert await _counts(sessions, order_id) == (S.WAITING_FOR_PLAN_CONFIRMATION, 1, 1)


async def test_claim_waits_for_order_lock(sessions: Sessionmaker) -> None:
    order_id = await _order(sessions, 92)
    llm = FakeLLMClient(GOOD_PLAN)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        async with sessions() as contender:
            task = asyncio.create_task(generate_plan(contender, llm, order_id))
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
            assert llm.calls == []
            await holder.commit()
            outcome = await asyncio.wait_for(task, 10)

    assert outcome.kind is K.COMPLETED


async def test_cancel_during_generation_discards_result(sessions: Sessionmaker) -> None:
    """Блокировка не держится во время вызова: отмена из другой сессии проходит сразу."""
    order_id = await _order(sessions, 93)
    llm = FakeLLMClient(GOOD_PLAN)

    async def cancel_from_other_session() -> None:
        async with sessions() as other:
            await asyncio.wait_for(orders.change_status(other, order_id, S.CANCELLED), 5)
            await other.commit()

    llm.on_complete = cancel_from_other_session
    async with sessions() as session:
        outcome = await generate_plan(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    assert await _counts(sessions, order_id) == (S.CANCELLED, 1, 0)


async def test_answer_after_takeover_does_not_touch_new_attempt(sessions: Sessionmaker) -> None:
    """Старая попытка признана зависшей и перехвачена: её ответ отбрасывается."""
    order_id = await _order(sessions, 94)
    old_llm = FakeLLMClient(GOOD_PLAN)
    new_llm = SlowLLM(LLMTemporaryError("http_529"), delay=0.5)

    async def taken_over_by_other_session() -> None:
        async with sessions() as other:
            # Попытка «зависла»: перехват другим экземпляром.
            await other.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(step_started_at=datetime.now(UTC) - timedelta(minutes=11))
            )
            await other.commit()
        async with sessions() as other:
            claimed = await service._claim(other, new_llm, order_id)
            assert isinstance(claimed, service._Claim)

    old_llm.on_complete = taken_over_by_other_session
    async with sessions() as session:
        outcome = await generate_plan(session, old_llm, order_id)

    assert outcome.kind is K.DISCARDED
    status, calls, plans = await _counts(sessions, order_id)
    assert (status, calls, plans) == (S.PREPARING_PLAN, 2, 0)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.step_attempts == 2 and order.step_started_at is not None


async def test_double_retry_press_restarts_once(sessions: Sessionmaker) -> None:
    order_id = await _order(sessions, 95)
    async with sessions() as session:
        await generate_plan(session, FakeLLMClient(LLMPermanentError("http_400")), order_id)
    barrier = asyncio.Barrier(2)

    async def press() -> str:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            try:
                await restart_plan_generation(session, order_id)
            except InvalidStatusTransitionError:
                await session.rollback()
                return "refused"
            await asyncio.sleep(0.3)  # держим блокировку: второе нажатие точно ждёт
            await session.commit()
            return "restarted"

    results = await asyncio.gather(press(), press())

    assert sorted(results) == ["refused", "restarted"]
    assert (await _counts(sessions, order_id))[0] is S.PREPARING_PLAN


async def test_concurrent_claims_do_not_exceed_call_limit(sessions: Sessionmaker) -> None:
    """Остался один вызов из шести: одновременные захваты не расходуют больше."""
    order_id = await _order(sessions, 96)
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
            for _ in range(MAX_PLAN_LLM_CALLS - 1)
        )
        await session.commit()
    llms = [SlowLLM(LLMTemporaryError("http_500")) for _ in range(4)]
    barrier = asyncio.Barrier(len(llms))

    async def run(llm: SlowLLM) -> K:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await generate_plan(session, llm, order_id)).kind

    kinds = await asyncio.gather(*(run(llm) for llm in llms))

    assert sum(len(llm.calls) for llm in llms) == 1
    assert kinds.count(K.FAILED) == 1  # последний вызов исчерпал лимит
    assert set(kinds) <= {K.FAILED, K.BUSY, K.SKIPPED}
    status, calls, _ = await _counts(sessions, order_id)
    assert (status, calls) == (S.FAILED, MAX_PLAN_LLM_CALLS)
    async with sessions() as session:
        with pytest.raises(PlanLimitReachedError):
            await restart_plan_generation(session, order_id)
