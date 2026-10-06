"""Гонки завершения отчёта между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между двумя экземплярами бота.
Хранилище — LocalFileStorage во временном каталоге; порядок записи в потоках
задаётся событиями (TmpWriteGate из test_storage_local), без sleep.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Order, OrderReportFile
from app.domain.enums import OrderStatus
from app.domain.report_generation import ReportOutcomeKind
from app.generation.schemas import DocumentContent
from app.rendering.docx import PracticeReportDocxRenderer
from app.services import orders
from app.services.report_completion import CompletionOutcomeKind, complete_report
from app.services.report_generation import generate_next_section
from app.services.storage.local import LocalFileStorage
from conftest import COMMITTED_TG_ID_BASE
from fake_llm import FakeLLMClient
from report_samples import ALL_GOOD, generating_order
from test_storage_local import TmpWriteGate, tmp_files

S = OrderStatus
K = CompletionOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]

VERSION_TABLES = ("order_plan_versions", "order_requirements_versions")
TEST_ORDERS = "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base"


@pytest.fixture
async def sessions(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление файлов, разделов, llm_calls и версий тестовых заказов.

    llm_calls не удаляются каскадом (FK SET NULL). Файлы и разделы ссылаются на версии
    плана и удаляются раньше них. Подтверждённые версии БД удалить не даёт (триггер
    неизменности): триггеры отключаются только внутри транзакции очистки.
    """
    yield committed_sessionmaker
    base = {"base": COMMITTED_TG_ID_BASE}
    async with committed_sessionmaker() as session:
        for table in ("order_report_files", "order_report_sections", "llm_calls"):
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


class SlowRenderer(PracticeReportDocxRenderer):
    """Рендер с паузой (в потоке asyncio.to_thread) и счётчиком вызовов."""

    def __init__(self, delay: float = 0.3) -> None:
        super().__init__()
        self.delay = delay
        self.calls = 0

    def render(self, content: DocumentContent) -> bytes:
        self.calls += 1
        time.sleep(self.delay)
        return super().render(content)


async def _order(sessions: Sessionmaker, offset: int) -> int:
    """Заказ в GENERATING со всеми сгенерированными разделами (зафиксировано)."""
    async with sessions() as session:
        order_id = await generating_order(session, tg_id=COMMITTED_TG_ID_BASE + offset)
        llm = FakeLLMClient(*ALL_GOOD)
        for _ in ALL_GOOD:
            outcome = await generate_next_section(session, llm, order_id)
            assert outcome.kind is ReportOutcomeKind.SAVED
        return order_id


async def _complete(
    sessions: Sessionmaker, storage: LocalFileStorage, renderer: object, order_id: int
) -> K:
    async with sessions() as session:
        return (await complete_report(session, storage, renderer, order_id)).kind  # type: ignore[arg-type]


async def _assert_single_result(
    sessions: Sessionmaker, storage: LocalFileStorage, order_id: int
) -> None:
    """COMPLETED, ровно одна строка файла, и файл по её ключу совпадает с ней."""
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status is S.COMPLETED
        assert order.completed_at is not None
        rows = list(
            await session.scalars(
                select(OrderReportFile).where(OrderReportFile.order_id == order_id)
            )
        )
    assert len(rows) == 1
    [row] = rows
    data = await storage.read(row.storage_key)
    assert len(data) == row.size_bytes
    assert hashlib.sha256(data).hexdigest() == row.sha256


async def test_two_workers_complete_once(sessions: Sessionmaker, tmp_path: Path) -> None:
    order_id = await _order(sessions, 301)
    storage = LocalFileStorage(tmp_path)
    renderer = SlowRenderer()
    barrier = asyncio.Barrier(2)

    async def run() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        return await _complete(sessions, storage, renderer, order_id)

    kinds = await asyncio.gather(run(), run())

    assert sorted(kinds) == sorted([K.COMPLETED, K.BUSY])
    assert renderer.calls == 1
    await _assert_single_result(sessions, storage, order_id)
    assert tmp_files(tmp_path) == []


async def test_claim_waits_for_order_lock(sessions: Sessionmaker, tmp_path: Path) -> None:
    order_id = await _order(sessions, 302)
    storage = LocalFileStorage(tmp_path)
    renderer = SlowRenderer(delay=0)

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        task = asyncio.create_task(_complete(sessions, storage, renderer, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
        assert renderer.calls == 0
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.COMPLETED
    await _assert_single_result(sessions, storage, order_id)


async def test_lock_is_not_held_during_render_and_save(
    sessions: Sessionmaker, tmp_path: Path
) -> None:
    """Пока идут рендер и сохранение, заказ можно заблокировать из другой сессии."""
    order_id = await _order(sessions, 303)
    loop = asyncio.get_running_loop()
    locked: list[str] = []

    async def lock_from_other_session(stage: str) -> None:
        async with sessions() as other:
            await asyncio.wait_for(orders.lock_order(other, order_id), 5)
            await other.commit()
        locked.append(stage)

    class LockingRenderer(PracticeReportDocxRenderer):
        def render(self, content: DocumentContent) -> bytes:
            # Поток рендера: блокировка берётся в цикле событий, рендер её ждёт.
            asyncio.run_coroutine_threadsafe(lock_from_other_session("render"), loop).result(10)
            return super().render(content)

    class LockingStorage(LocalFileStorage):
        async def save(self, key: str, data: bytes) -> None:
            await lock_from_other_session("save")
            await super().save(key, data)

    storage = LockingStorage(tmp_path)

    kind = await _complete(sessions, storage, LockingRenderer(), order_id)

    assert kind is K.COMPLETED
    assert locked == ["render", "save"]
    await _assert_single_result(sessions, storage, order_id)


@pytest.mark.parametrize("a_outcome", ["fails", "completes"])
async def test_stale_takeover_inside_storage_save(
    sessions: Sessionmaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, a_outcome: str
) -> None:
    """A завис внутри storage.save() после открытия своего tmp, B перехватил попытку.

    Сразу после commit B и после того, как A упал или дописал, файл по ключу совпадает
    со строкой (sha256, size_bytes), строка одна, T3 у A отвергнут, tmp не осталось.
    """
    order_id = await _order(sessions, 304 if a_outcome == "fails" else 305)
    storage = LocalFileStorage(tmp_path)
    renderer = PracticeReportDocxRenderer()
    # Первое открытие tmp — запись A: половина данных, затем пауза.
    gate = TmpWriteGate(open_number=1, pause_at="write", fail=a_outcome == "fails")
    gate.install(monkeypatch)
    try:
        task_a = asyncio.create_task(_complete(sessions, storage, renderer, order_id))
        await gate.wait_paused()

        async with sessions() as session:
            await session.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(step_started_at=datetime.now(UTC) - timedelta(minutes=11))
            )
            await session.commit()
        kind_b = await _complete(sessions, storage, renderer, order_id)

        assert kind_b is K.COMPLETED
        await _assert_single_result(sessions, storage, order_id)

        gate.release()
        kind_a = await asyncio.wait_for(task_a, 10)
    finally:
        gate.release()

    assert kind_a is K.DISCARDED
    await _assert_single_result(sessions, storage, order_id)
    async with sessions() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert (order.step_attempts, order.error) == (2, None)
    assert tmp_files(tmp_path) == []


async def test_unique_constraint_blocks_duplicate_file(
    sessions: Sessionmaker, tmp_path: Path
) -> None:
    order_id = await _order(sessions, 306)
    storage = LocalFileStorage(tmp_path)
    assert await _complete(sessions, storage, PracticeReportDocxRenderer(), order_id) is K.COMPLETED

    async with sessions() as session:
        [row] = await session.scalars(
            select(OrderReportFile).where(OrderReportFile.order_id == order_id)
        )
        session.add(
            OrderReportFile(
                order_id=order_id,
                plan_version_id=row.plan_version_id,
                storage_key=row.storage_key,
                size_bytes=row.size_bytes,
                sha256=row.sha256,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    await _assert_single_result(sessions, storage, order_id)
