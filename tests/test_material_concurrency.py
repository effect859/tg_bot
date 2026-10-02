"""Гонки между независимыми сессиями PostgreSQL: за материал и за завершение приёма.

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

from app.db.models import Order, OrderMaterial, OrderMaterialText
from app.domain.enums import MaterialProcessingStatus, OrderStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    MaterialsNotAcceptedError,
    MaterialsNotReadyError,
    MaterialsNotReadyReason,
)
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


# --- Завершение приёма материалов -----------------------------------------------------


async def _order_with_text(
    sessionmaker: Sessionmaker, storage: LocalFileStorage, tg_id: int
) -> int:
    """Заказ в WAITING_FOR_FILES с одним обработанным материалом с текстом (зафиксировано)."""
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
        await materials.mark_processing(session, material.id)
        await materials.mark_processed(session, material.id)
        session.add(
            OrderMaterialText(material_id=material.id, text="текст", char_count=5, extractor="t")
        )
        order_id = order.id
        await session.commit()
    return order_id


async def _order_state(sessionmaker: Sessionmaker, order_id: int) -> tuple[S, int]:
    async with sessionmaker() as session:
        status = await session.scalar(select(Order.status).where(Order.id == order_id))
        count = await session.scalar(
            select(func.count())
            .select_from(OrderMaterial)
            .where(OrderMaterial.order_id == order_id)
        )
        assert status is not None and count is not None
        return status, count


async def test_file_waits_for_finish_and_is_rejected(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    """Завершение держит блокировку заказа: файл ждёт её и после перехода отклоняется."""
    order_id = await _order_with_text(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 11)

    async with committed_sessionmaker() as finishing, committed_sessionmaker() as uploading:
        await materials.finish_materials(finishing, order_id)  # блокировка заказа взята
        upload = asyncio.create_task(
            materials.store_material(
                uploading, storage, order_id, filename="late.txt", mime_type=None, data=TXT_BYTES
            )
        )
        await asyncio.sleep(0.5)
        assert not upload.done()  # ждёт SELECT ... FOR UPDATE заказа
        await finishing.commit()
        with pytest.raises(MaterialsNotAcceptedError):
            await asyncio.wait_for(upload, 10)

    assert await _order_state(committed_sessionmaker, order_id) == (S.ANALYZING_MATERIALS, 1)
    # Файл отклонённого материала не остаётся в хранилище.
    assert not await storage.exists(materials.material_storage_key(order_id, 2, "txt"))


async def test_finish_waits_for_new_file_and_refuses(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    """Файл добавляется под блокировкой заказа: завершение ждёт и видит новый материал."""
    order_id = await _order_with_text(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 12)

    async with committed_sessionmaker() as uploading, committed_sessionmaker() as finishing:
        await materials.store_material(
            uploading, storage, order_id, filename="new.txt", mime_type=None, data=TXT_BYTES
        )  # блокировка заказа взята
        finish = asyncio.create_task(materials.finish_materials(finishing, order_id))
        await asyncio.sleep(0.5)
        assert not finish.done()  # ждёт SELECT ... FOR UPDATE заказа
        await uploading.commit()
        with pytest.raises(MaterialsNotReadyError) as caught:
            await asyncio.wait_for(finish, 10)

    assert (caught.value.reason, caught.value.pending) == (MaterialsNotReadyReason.PENDING, 1)
    assert await _order_state(committed_sessionmaker, order_id) == (S.WAITING_FOR_FILES, 2)


async def test_finish_during_processing_is_refused(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    """Пока материал обрабатывается, завершение отказывает; материал обрабатывается."""
    order_id = await _order_with_text(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 13)
    async with committed_sessionmaker() as session:
        material = await materials.store_material(
            session, storage, order_id, filename="new.txt", mime_type=None, data=TXT_BYTES
        )
        material_id = material.id
        await session.commit()
    extractor = SlowExtractor(delay=0.5)
    started = asyncio.Event()

    async def mark_started() -> None:
        started.set()

    extractor.on_extract = mark_started

    async def processing() -> ProcessingOutcome:
        async with committed_sessionmaker() as session:
            return await process_material(session, storage, extractor, material_id)

    task = asyncio.create_task(processing())
    await asyncio.wait_for(started.wait(), 10)  # материал захвачен, идёт извлечение
    async with committed_sessionmaker() as finishing:
        with pytest.raises(MaterialsNotReadyError) as caught:
            await materials.finish_materials(finishing, order_id)
        await finishing.rollback()
    outcome = await asyncio.wait_for(task, 10)

    assert caught.value.reason is MaterialsNotReadyReason.PENDING
    assert outcome.kind is OutcomeKind.PROCESSED
    assert await _order_state(committed_sessionmaker, order_id) == (S.WAITING_FOR_FILES, 2)
    status, attempts, texts = await _state(committed_sessionmaker, material_id)
    assert (status, attempts, len(texts)) == (M.PROCESSED, 1, 1)


async def test_double_finish_in_two_sessions_moves_order_once(
    committed_sessionmaker: Sessionmaker, storage: LocalFileStorage
) -> None:
    order_id = await _order_with_text(committed_sessionmaker, storage, COMMITTED_TG_ID_BASE + 14)
    barrier = asyncio.Barrier(2)

    async def finish() -> str:
        async with committed_sessionmaker() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            try:
                await materials.finish_materials(session, order_id)
            except InvalidStatusTransitionError:
                await session.rollback()
                return "refused"
            await asyncio.sleep(0.3)  # держим блокировку: второй точно ждёт её
            await session.commit()
            return "finished"

    results = await asyncio.gather(finish(), finish())

    assert sorted(results) == ["finished", "refused"]
    assert await _order_state(committed_sessionmaker, order_id) == (S.ANALYZING_MATERIALS, 1)
