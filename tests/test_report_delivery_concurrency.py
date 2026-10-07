"""Гонки доставки отчёта между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker):
блокировка строки заказа действует так же, как между двумя экземплярами бота.
Отправитель — подмена ReportSender; порядок задаётся событиями, а не sleep.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_report_completion_concurrency as completion_races
from app.db.models import OrderReportDelivery, OrderReportFile
from app.rendering.docx import PracticeReportDocxRenderer
from app.services import orders
from app.services.report_completion import CompletionOutcomeKind, complete_report
from app.services.report_delivery import DeliveryOutcomeKind, deliver_report
from app.services.storage.local import LocalFileStorage

K = DeliveryOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]

# Та же фикстура, что у гонок завершения: удаляет файлы (доставки — каскадом),
# разделы, llm_calls и версии тестовых заказов.
sessions = completion_races.sessions


class Sender:
    """ReportSender с учётом вызовов; ``on_send`` — перед «приёмом» документа."""

    def __init__(self, on_send: Callable[[], Awaitable[None]] | None = None) -> None:
        self.on_send = on_send
        self.calls: list[tuple[int, bytes, str]] = []

    async def __call__(self, chat_id: int, data: bytes, filename: str) -> None:
        self.calls.append((chat_id, data, filename))
        if self.on_send is not None:
            await self.on_send()


async def _completed(sessions: Sessionmaker, storage: LocalFileStorage, offset: int) -> int:
    """Заказ в COMPLETED с сохранённым файлом (зафиксировано)."""
    order_id = await completion_races._order(sessions, offset)
    async with sessions() as session:
        outcome = await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)
    assert outcome.kind is CompletionOutcomeKind.COMPLETED
    return order_id


async def _deliver(
    sessions: Sessionmaker, storage: LocalFileStorage, sender: Sender, order_id: int
) -> K:
    async with sessions() as session:
        return (await deliver_report(session, storage, sender, order_id)).kind


async def _deliveries(sessions: Sessionmaker, order_id: int) -> list[OrderReportDelivery]:
    async with sessions() as session:
        stmt = (
            select(OrderReportDelivery)
            .join(OrderReportFile, OrderReportFile.id == OrderReportDelivery.report_file_id)
            .where(OrderReportFile.order_id == order_id)
        )
        return list(await session.scalars(stmt))


async def test_two_workers_deliver_once(sessions: Sessionmaker, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed(sessions, storage, 401)
    barrier = asyncio.Barrier(2)
    release = asyncio.Event()
    # Первый захвативший «отправляет», пока второй не получит ответ.
    sender = Sender(on_send=lambda: asyncio.wait_for(release.wait(), 10))

    async def run() -> K:
        await asyncio.wait_for(barrier.wait(), 10)
        kind = await _deliver(sessions, storage, sender, order_id)
        if kind is K.BUSY:
            release.set()
        return kind

    kinds = await asyncio.gather(run(), run())

    assert sorted(kinds) == sorted([K.DELIVERED, K.BUSY])
    assert len(sender.calls) == 1
    [delivery] = await _deliveries(sessions, order_id)
    assert (delivery.attempts, delivery.started_at, delivery.error) == (1, None, None)
    assert delivery.delivered_at is not None


async def test_claim_waits_for_order_lock(sessions: Sessionmaker, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed(sessions, storage, 402)
    sender = Sender()

    async with sessions() as holder:
        await orders.lock_order(holder, order_id)
        task = asyncio.create_task(_deliver(sessions, storage, sender, order_id))
        await asyncio.sleep(0.5)
        assert not task.done()  # ждёт SELECT ... FOR UPDATE заказа
        assert sender.calls == []
        await holder.commit()
        kind = await asyncio.wait_for(task, 10)

    assert kind is K.DELIVERED
    assert len(sender.calls) == 1


async def test_lock_is_not_held_during_read_and_send(
    sessions: Sessionmaker, tmp_path: Path
) -> None:
    """Пока идут чтение файла и отправка, заказ можно заблокировать из другой сессии."""
    locked: list[str] = []

    async def lock_from_other_session(stage: str) -> None:
        async with sessions() as other:
            await asyncio.wait_for(orders.lock_order(other, order_id), 5)
            await other.commit()
        locked.append(stage)

    class LockingStorage(LocalFileStorage):
        async def read(self, key: str) -> bytes:
            await lock_from_other_session("read")
            return await super().read(key)

    storage = LockingStorage(tmp_path)
    order_id = await _completed(sessions, storage, 403)
    sender = Sender(on_send=lambda: lock_from_other_session("send"))

    kind = await _deliver(sessions, storage, sender, order_id)

    assert kind is K.DELIVERED
    assert locked == ["read", "send"]


async def test_stale_takeover_during_send_delivers_twice(
    sessions: Sessionmaker, tmp_path: Path
) -> None:
    """A завис в отправке, B перехватил зависшую попытку: оба отправили (at-least-once),
    T3 у A отвергнут, итог — одна доставка B."""
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed(sessions, storage, 404)
    paused = asyncio.Event()
    release = asyncio.Event()

    async def pause_first() -> None:
        if not paused.is_set():
            paused.set()
            await asyncio.wait_for(release.wait(), 10)

    sender = Sender(on_send=pause_first)
    task_a = asyncio.create_task(_deliver(sessions, storage, sender, order_id))
    try:
        await asyncio.wait_for(paused.wait(), 10)
        async with sessions() as session:
            [delivery] = await _deliveries(sessions, order_id)
            await session.execute(
                update(OrderReportDelivery)
                .where(OrderReportDelivery.id == delivery.id)
                .values(started_at=datetime.now(UTC) - timedelta(minutes=11))
            )
            await session.commit()

        kind_b = await _deliver(sessions, storage, sender, order_id)
        assert kind_b is K.DELIVERED
    finally:
        release.set()
    kind_a = await asyncio.wait_for(task_a, 10)

    assert kind_a is K.DISCARDED
    assert len(sender.calls) == 2
    [delivery] = await _deliveries(sessions, order_id)
    assert (delivery.attempts, delivery.started_at, delivery.error) == (2, None, None)
    assert delivery.delivered_at is not None


async def test_unique_constraint_blocks_second_delivery_row(
    sessions: Sessionmaker, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed(sessions, storage, 405)
    assert await _deliver(sessions, storage, Sender(), order_id) is K.DELIVERED
    [delivery] = await _deliveries(sessions, order_id)

    async with sessions() as session:
        session.add(OrderReportDelivery(report_file_id=delivery.report_file_id))
        with pytest.raises(IntegrityError, match="uq_order_report_deliveries_report_file_id"):
            await session.commit()
        await session.rollback()

    assert len(await _deliveries(sessions, order_id)) == 1
