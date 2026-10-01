"""Гонки за материал между независимыми сессиями PostgreSQL.

В отличие от остальных тестов БД, здесь каждая сессия — своё соединение и настоящие
транзакции (фикстура committed_sessionmaker): блокировки строк действуют так же, как
между апдейтом и проходом восстановления или двумя экземплярами бота. Извлечение —
подмена с паузой, чтобы гонки гарантированно пересекались.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OrderMaterial, OrderMaterialText
from app.domain.enums import MaterialProcessingStatus, OrderStatus
from app.extraction.base import Extraction, ExtractionResult
from app.services import order_materials as materials
from app.services import orders
from app.services.material_processing import (
    OutcomeKind,
    ProcessingOutcome,
    process_material,
    process_pending_materials,
)
from app.services.material_recovery import run_recovery_pass
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from conftest import COMMITTED_TG_ID_BASE
from fake_extractor import FakeExtractor
from material_samples import TXT_BYTES

S = OrderStatus
M = MaterialProcessingStatus

Sessionmaker = async_sessionmaker[AsyncSession]


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "storage")


class SlowExtractor(FakeExtractor):
    """Извлечение с паузой: пока оно идёт, другая сессия успевает вмешаться."""

    def __init__(self, *answers: Extraction, delay: float = 0.3) -> None:
        super().__init__(*answers)
        self.delay = delay

    async def extract(self, extension: str, data: bytes) -> Extraction:
        await asyncio.sleep(self.delay)
        return await super().extract(extension, data)


class Notifications:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    async def __call__(self, chat_id: int, outcome: ProcessingOutcome) -> None:
        self.sent.append((chat_id, outcome.material_id))


def _meet_before_claim(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
    """Участники гонки входят в process_material только все вместе.

    Так оба уже выбрали материал и одновременно идут его захватывать: гонка решается
    блокировкой строки, а не тем, кто раньше выполнил выборку.
    """
    from app.services import material_processing, material_recovery

    barrier = asyncio.Barrier(parties)
    original = material_processing.process_material

    async def meet_then_process(*args: object, **kwargs: object) -> ProcessingOutcome:
        await asyncio.wait_for(barrier.wait(), 10)
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(material_processing, "process_material", meet_then_process)
    monkeypatch.setattr(material_recovery, "process_material", meet_then_process)


async def _old_material(sessionmaker: Sessionmaker, storage: LocalFileStorage, tg_id: int) -> int:
    """Заказ пользователя и материал в RECEIVED старше окна ожидания — всё зафиксировано."""
    async with sessionmaker() as session:
        user = await register_or_update_user(
            session, tg_id=tg_id, username=None, first_name=None, language_code=None
        )
        order = await orders.create_order(session, user_id=user.id)
        for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
            order = await orders.change_status(session, order.id, status)
        material = await materials.store_material(
            session, storage, order.id, filename="task.txt", mime_type=None, data=TXT_BYTES
        )
        material_id = material.id
        await session.execute(
            update(OrderMaterial)
            .where(OrderMaterial.id == material_id)
            .values(created_at=func.now() - timedelta(minutes=10))
        )
        await session.commit()
    return material_id


async def _state(sessionmaker: Sessionmaker, material_id: int) -> tuple[M, int, list[str]]:
    async with sessionmaker() as session:
        material = await session.get(OrderMaterial, material_id)
        assert material is not None
        texts = list(
            await session.scalars(
                select(OrderMaterialText.text).where(OrderMaterialText.material_id == material_id)
            )
        )
        return material.processing_status, material.processing_attempts, texts


async def _order_id(sessionmaker: Sessionmaker, material_id: int) -> int:
    async with sessionmaker() as session:
        material = await session.get(OrderMaterial, material_id)
        assert material is not None
        return material.order_id


async def test_claim_waits_for_row_lock_of_another_session(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    """Захват действительно ждёт блокировку строки, взятую другой сессией."""
    material_id = await _old_material(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 1)
    extractor = FakeExtractor()

    async with committed_sessionmaker() as holder:
        material = await materials.lock_material(holder, material_id)
        async with committed_sessionmaker() as contender:
            task = asyncio.create_task(process_material(contender, storage, extractor, material_id))
            await asyncio.sleep(0.5)
            assert not task.done()  # ждёт SELECT ... FOR UPDATE
            # Держатель блокировки сам берёт материал в работу.
            materials.apply_material_status(material, M.PROCESSING)
            await holder.commit()
            outcome = await asyncio.wait_for(task, 10)

    assert outcome.kind is OutcomeKind.SKIPPED
    assert extractor.calls == []
    status, attempts, _ = await _state(committed_sessionmaker, material_id)
    assert (status, attempts) == (M.PROCESSING, 1)


async def test_concurrent_recovery_passes_take_material_once(
    committed_sessionmaker: Sessionmaker,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Два прохода восстановления (как два экземпляра бота) одновременно."""
    material_id = await _old_material(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 2)
    _meet_before_claim(monkeypatch, parties=2)
    first, second = SlowExtractor(), SlowExtractor()
    notify = Notifications()

    results = await asyncio.gather(
        run_recovery_pass(committed_sessionmaker, storage, first, notify),
        run_recovery_pass(committed_sessionmaker, storage, second, notify),
    )

    kinds = sorted(o.kind for outcomes in results for o in outcomes)
    assert kinds == sorted([OutcomeKind.PROCESSED, OutcomeKind.SKIPPED])
    assert len(first.calls) + len(second.calls) == 1
    status, attempts, texts = await _state(committed_sessionmaker, material_id)
    assert (status, attempts, len(texts)) == (M.PROCESSED, 1, 1)
    assert notify.sent == [(COMMITTED_TG_ID_BASE + 2, material_id)]


async def test_update_and_recovery_race_for_material(
    committed_sessionmaker: Sessionmaker,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обработка в апдейте и проход восстановления одновременно берут один материал."""
    material_id = await _old_material(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 3)
    order_id = await _order_id(committed_sessionmaker, material_id)
    _meet_before_claim(monkeypatch, parties=2)
    in_update, in_recovery = SlowExtractor(), SlowExtractor()
    notify = Notifications()

    async def update_path() -> list[ProcessingOutcome]:
        async with committed_sessionmaker() as session:
            return await process_pending_materials(session, storage, in_update, order_id)

    by_update, by_recovery = await asyncio.gather(
        update_path(),
        run_recovery_pass(committed_sessionmaker, storage, in_recovery, notify),
    )

    kinds = sorted(o.kind for o in [*by_update, *by_recovery])
    assert kinds == sorted([OutcomeKind.PROCESSED, OutcomeKind.SKIPPED])
    assert len(in_update.calls) + len(in_recovery.calls) == 1
    status, attempts, texts = await _state(committed_sessionmaker, material_id)
    assert (status, attempts, len(texts)) == (M.PROCESSED, 1, 1)
    # Сообщение от прохода — только если материал обработал он.
    assert len(notify.sent) == len(in_recovery.calls)


async def test_old_attempt_loses_to_recovered_attempt(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    """Попытку 1 признали прерванной, проход взял попытку 2; результат 1 отброшен."""
    material_id = await _old_material(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 4)
    old = FakeExtractor(ExtractionResult(text="старый", extractor="old"))
    new = FakeExtractor(ExtractionResult(text="новый", extractor="new"))
    recovered: list[ProcessingOutcome] = []

    async def attempt_becomes_stale_and_is_recovered() -> None:
        async with committed_sessionmaker() as other:
            await other.execute(
                update(OrderMaterial)
                .where(OrderMaterial.id == material_id)
                .values(processing_started_at=datetime.now(UTC) - timedelta(minutes=10))
            )
            await other.commit()
        recovered.extend(
            await run_recovery_pass(committed_sessionmaker, storage, new, Notifications())
        )

    old.on_extract = attempt_becomes_stale_and_is_recovered
    async with committed_sessionmaker() as session:
        outcome = await process_material(session, storage, old, material_id)

    assert [o.kind for o in recovered] == [OutcomeKind.PROCESSED]
    assert outcome.kind is OutcomeKind.DISCARDED
    status, attempts, texts = await _state(committed_sessionmaker, material_id)
    assert (status, attempts, texts) == (M.PROCESSED, 2, ["новый"])
