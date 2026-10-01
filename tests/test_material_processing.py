"""Сервис обработки материалов: статусы, повторы, прерванная обработка, сохранность файлов.

БД — настоящая PostgreSQL, хранилище — LocalFileStorage во временном каталоге,
извлечение — подмена (сами извлекатели проверяются в test_*_extraction.py).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderMaterial, OrderMaterialText
from app.domain.enums import (
    MaterialFailureReason,
    MaterialProcessingStatus,
    OrderStatus,
    TextTruncationReason,
)
from app.domain.materials import MAX_PROCESSING_ATTEMPTS
from app.extraction.base import (
    Extraction,
    ExtractionFailure,
    ExtractionResult,
    ExtractionWarning,
    WarningCode,
)
from app.services import order_materials as materials
from app.services import orders
from app.services.material_processing import (
    OutcomeKind,
    get_material_text,
    process_material,
    process_pending_materials,
)
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from fake_extractor import OK, FakeExtractor
from material_samples import PDF_BYTES, TXT_BYTES

S = OrderStatus
M = MaterialProcessingStatus
R = MaterialFailureReason


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "storage")


async def _order(session: AsyncSession, tg_id: int = 1) -> int:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
        order = await orders.change_status(session, order.id, status)
    return order.id


async def _store(
    session: AsyncSession,
    storage: LocalFileStorage,
    order_id: int,
    filename: str = "task.txt",
    data: bytes = TXT_BYTES,
) -> OrderMaterial:
    material = await materials.store_material(
        session, storage, order_id, filename=filename, mime_type=None, data=data
    )
    await session.commit()
    return material


async def _fresh(session: AsyncSession, material_id: int) -> OrderMaterial:
    material = await session.get(OrderMaterial, material_id, populate_existing=True)
    assert material is not None
    return material


async def _text_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(OrderMaterialText))


# --- Успех --------------------------------------------------------------------------------


async def test_processed_material_gets_text(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    result = ExtractionResult(
        text="Ёлка\fстраница 2",
        extractor="pypdfium2 test",
        truncation=TextTruncationReason.PAGE_LIMIT,
        page_count=3,
        pages_processed=2,
        warnings=(ExtractionWarning(WarningCode.PAGES_WITHOUT_TEXT, (2,)),),
    )
    extractor = FakeExtractor(result)

    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.kind is OutcomeKind.PROCESSED
    assert outcome.result == result
    assert outcome.order_active
    assert extractor.calls == [("txt", TXT_BYTES)]
    material = await _fresh(session, material.id)
    assert material.processing_status is M.PROCESSED
    assert material.processing_attempts == 1
    assert material.processing_started_at is None
    assert material.failure_reason is None and material.processing_error is None
    saved = await get_material_text(session, material.id)
    assert saved is not None
    assert saved.text == "Ёлка\fстраница 2"
    assert saved.char_count == len(saved.text)
    assert (saved.page_count, saved.pages_processed) == (3, 2)
    assert saved.truncation_reason is TextTruncationReason.PAGE_LIMIT
    assert saved.warnings == [{"code": "pages_without_text", "kind": "fact", "pages": [2]}]
    assert saved.extractor == "pypdfium2 test"


async def test_empty_text_is_processed_not_failed(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    empty = ExtractionResult(
        text="", extractor="fake", warnings=(ExtractionWarning(WarningCode.NO_TEXT),)
    )

    outcome = await process_material(session, storage, FakeExtractor(empty), material.id)

    assert outcome.kind is OutcomeKind.PROCESSED
    assert (await _fresh(session, material.id)).processing_status is M.PROCESSED
    saved = await get_material_text(session, material.id)
    assert saved is not None and saved.text == "" and saved.char_count == 0


async def test_large_text_is_stored(session: AsyncSession, storage: LocalFileStorage) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    big = ExtractionResult(text="я" * 500_000, extractor="fake")

    await process_material(session, storage, FakeExtractor(big), material.id)

    saved = await get_material_text(session, material.id)
    assert saved is not None and saved.char_count == 500_000


async def test_processed_material_is_not_processed_again(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor()
    await process_material(session, storage, extractor, material.id)

    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.kind is OutcomeKind.SKIPPED
    assert len(extractor.calls) == 1
    assert await _text_count(session) == 1


async def test_material_in_processing_is_not_taken_twice(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    """Пока одна обработка идёт, повторный вызов ничего не делает."""
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor()
    inner: list[OutcomeKind] = []

    async def nested_call() -> None:
        outcome = await process_material(session, storage, FakeExtractor(), material.id)
        inner.append(outcome.kind)

    extractor.on_extract = nested_call
    outcome = await process_material(session, storage, extractor, material.id)

    assert inner == [OutcomeKind.SKIPPED]
    assert outcome.kind is OutcomeKind.PROCESSED
    assert await _text_count(session) == 1


async def test_no_transaction_is_open_during_extraction(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    seen: list[bool] = []
    extractor = FakeExtractor()

    async def check() -> None:
        seen.append(session.in_transaction())

    extractor.on_extract = check
    await process_material(session, storage, extractor, material.id)

    assert seen == [False]


# --- Отказы и повторы -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [R.CORRUPTED, R.ENCRYPTED, R.TIMEOUT, R.EXTRACTOR_CRASH, R.LIMIT_EXCEEDED, R.UNSUPPORTED],
)
async def test_permanent_failure_is_not_retried(
    session: AsyncSession, storage: LocalFileStorage, reason: MaterialFailureReason
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor(ExtractionFailure(reason, "details for logs"))

    outcome = await process_material(session, storage, extractor, material.id)
    again = await process_pending_materials(session, storage, extractor, order_id)

    assert outcome.kind is OutcomeKind.FAILED
    assert outcome.failure_reason is reason
    assert not outcome.will_retry
    assert again == []
    material = await _fresh(session, material.id)
    assert material.processing_status is M.FAILED
    assert material.failure_reason is reason
    assert material.processing_error == "details for logs"
    assert await _text_count(session) == 0


async def test_temporary_failure_is_retried_until_success(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor(ExtractionFailure(R.EXTRACTOR_UNAVAILABLE, "spawn"), OK)

    first = await process_pending_materials(session, storage, extractor, order_id)
    second = await process_pending_materials(session, storage, extractor, order_id)

    assert [o.kind for o in first] == [OutcomeKind.FAILED]
    assert first[0].will_retry
    assert [o.kind for o in second] == [OutcomeKind.PROCESSED]
    material = await _fresh(session, material.id)
    assert material.processing_status is M.PROCESSED
    assert material.processing_attempts == 2


async def test_retries_stop_after_max_attempts(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor(ExtractionFailure(R.EXTRACTOR_UNAVAILABLE, "spawn"))

    outcomes = []
    for _ in range(MAX_PROCESSING_ATTEMPTS + 2):
        outcomes += await process_pending_materials(session, storage, extractor, order_id)

    assert len(outcomes) == MAX_PROCESSING_ATTEMPTS
    assert [o.will_retry for o in outcomes] == [True] * (MAX_PROCESSING_ATTEMPTS - 1) + [False]
    assert len(extractor.calls) == MAX_PROCESSING_ATTEMPTS
    material = await _fresh(session, material.id)
    assert material.processing_attempts == MAX_PROCESSING_ATTEMPTS
    assert material.processing_status is M.FAILED


async def test_missing_file_is_integrity_failure(
    session: AsyncSession, storage: LocalFileStorage, caplog: pytest.LogCaptureFixture
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await storage.delete(material.storage_key)
    extractor = FakeExtractor()

    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.failure_reason is R.FILE_MISSING
    assert not outcome.will_retry
    assert extractor.calls == []
    assert any(
        r.levelname == "ERROR" and "Material file is missing" in r.message for r in caplog.records
    )


async def test_storage_read_error_is_retryable(
    session: AsyncSession, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)

    async def broken_read(key: str) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(storage, "read", broken_read)
    outcome = await process_material(session, storage, FakeExtractor(), material.id)

    assert outcome.failure_reason is R.STORAGE_ERROR
    assert outcome.will_retry


async def test_files_survive_every_failure(
    session: AsyncSession, storage: LocalFileStorage, tmp_path: Path
) -> None:
    """Обработка только читает файл: ни один сбой его не удаляет и не меняет."""
    order_id = await _order(session)
    stored = [
        await _store(session, storage, order_id, f"f{i}.txt", f"файл {i}".encode())
        for i in range(4)
    ]
    answers: list[Extraction] = [
        ExtractionFailure(R.CORRUPTED, "x"),
        ExtractionFailure(R.TIMEOUT, "x"),
        ExtractionFailure(R.EXTRACTOR_CRASH, "x"),
        ExtractionFailure(R.EXTRACTOR_UNAVAILABLE, "x"),
    ]
    await process_pending_materials(session, storage, FakeExtractor(*answers), order_id)

    for i, material in enumerate(stored):
        assert await storage.read(material.storage_key) == f"файл {i}".encode()


async def test_database_error_after_extraction_leaves_file_and_recoverable_state(
    session: AsyncSession, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    from app.services import material_processing

    async def failing_finish(*args: object, **kwargs: object) -> object:
        raise IntegrityError("stmt", {}, Exception("db down"))

    monkeypatch.setattr(material_processing, "_finish", failing_finish)
    with pytest.raises(IntegrityError):
        await process_material(session, storage, FakeExtractor(), material.id)
    await session.rollback()

    material = await _fresh(session, material.id)
    # Захват уже зафиксирован: материал в PROCESSING и будет признан прерванным.
    assert material.processing_status is M.PROCESSING
    assert await storage.read(material.storage_key) == TXT_BYTES


# --- Прерванная обработка -------------------------------------------------------------------


async def _make_stale(session: AsyncSession, material_id: int, minutes: int = 10) -> None:
    await materials.mark_processing(session, material_id)
    await session.execute(
        update(OrderMaterial)
        .where(OrderMaterial.id == material_id)
        .values(processing_started_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await session.commit()


async def test_stale_processing_is_recovered(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await _make_stale(session, material.id)

    outcomes = await process_pending_materials(session, storage, FakeExtractor(), order_id)

    assert [o.kind for o in outcomes] == [OutcomeKind.PROCESSED]
    material = await _fresh(session, material.id)
    assert material.processing_status is M.PROCESSED
    assert material.processing_attempts == 2


async def test_recent_processing_is_left_alone(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await materials.mark_processing(session, material.id)
    await session.commit()

    outcomes = await process_pending_materials(session, storage, FakeExtractor(), order_id)
    single = await process_material(session, storage, FakeExtractor(), material.id)

    assert outcomes == []
    assert single.kind is OutcomeKind.SKIPPED
    assert (await _fresh(session, material.id)).processing_status is M.PROCESSING


async def test_stale_result_of_old_attempt_is_discarded(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    """Пока шло извлечение, обработку признали прерванной и взяли заново."""
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor(ExtractionResult(text="старый результат", extractor="old"))

    async def taken_over() -> None:
        await session.execute(
            update(OrderMaterial)
            .where(OrderMaterial.id == material.id)
            .values(processing_attempts=OrderMaterial.processing_attempts + 1)
        )
        await session.commit()

    extractor.on_extract = taken_over
    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.kind is OutcomeKind.DISCARDED
    assert await _text_count(session) == 0
    assert (await _fresh(session, material.id)).processing_status is M.PROCESSING


async def test_exhausted_stale_material_stays_failed(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await session.execute(
        update(OrderMaterial)
        .where(OrderMaterial.id == material.id)
        .values(processing_attempts=MAX_PROCESSING_ATTEMPTS - 1)
    )
    await _make_stale(session, material.id)

    outcomes = await process_pending_materials(session, storage, FakeExtractor(), order_id)

    assert [(o.kind, o.failure_reason, o.will_retry) for o in outcomes] == [
        (OutcomeKind.FAILED, R.INTERRUPTED, False)
    ]
    material = await _fresh(session, material.id)
    assert material.processing_status is M.FAILED
    assert material.failure_reason is R.INTERRUPTED


# --- Заказ и очередь ------------------------------------------------------------------------


async def test_cancelled_order_materials_are_not_processed(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await orders.change_status(session, order_id, S.CANCELLED)
    await session.commit()
    extractor = FakeExtractor()

    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.kind is OutcomeKind.SKIPPED
    assert not outcome.order_active
    assert extractor.calls == []
    assert (await _fresh(session, material.id)).processing_status is M.RECEIVED


async def test_order_cancelled_during_extraction_keeps_result_without_notice(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    extractor = FakeExtractor()

    async def cancel() -> None:
        await orders.change_status(session, order_id, S.CANCELLED)
        await session.commit()

    extractor.on_extract = cancel
    outcome = await process_material(session, storage, extractor, material.id)

    assert outcome.kind is OutcomeKind.PROCESSED
    assert not outcome.order_active
    assert (await _fresh(session, material.id)).processing_status is M.PROCESSED


async def test_busy_queue_leaves_material_received(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    first = await _store(session, storage, order_id, "a.txt")
    await _store(session, storage, order_id, "b.txt")
    extractor = FakeExtractor()
    extractor.busy = True

    outcomes = await process_pending_materials(session, storage, extractor, order_id)

    # Остальные не трогаются, пока очередь занята.
    assert [(o.kind, o.material_id) for o in outcomes] == [(OutcomeKind.BUSY, first.id)]
    statuses = [m.processing_status for m in await materials.get_order_materials(session, order_id)]
    assert statuses == [M.RECEIVED, M.RECEIVED]


async def test_pending_materials_in_upload_order(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    stored = [await _store(session, storage, order_id, f"{i}.txt") for i in range(3)]
    extractor = FakeExtractor()

    outcomes = await process_pending_materials(session, storage, extractor, order_id)

    assert [o.material_id for o in outcomes] == [m.id for m in stored]
    assert [o.upload_order for o in outcomes] == [1, 2, 3]


async def test_orders_of_different_users_are_independent(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    first_order = await _order(session, tg_id=1)
    second_order = await _order(session, tg_id=2)
    mine = await _store(session, storage, first_order, "mine.txt", "моё".encode())
    theirs = await _store(session, storage, second_order, "theirs.pdf", PDF_BYTES)
    extractor = FakeExtractor(ExtractionResult(text="моё", extractor="fake"))

    outcomes = await process_pending_materials(session, storage, extractor, first_order)

    assert [o.material_id for o in outcomes] == [mine.id]
    assert extractor.calls == [("txt", "моё".encode())]
    assert (await _fresh(session, theirs.id)).processing_status is M.RECEIVED
    assert await get_material_text(session, theirs.id) is None


# --- Схема БД -------------------------------------------------------------------------------


async def test_text_is_deleted_with_material(
    session: AsyncSession, storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)
    await process_material(session, storage, FakeExtractor(), material.id)

    await session.execute(text("DELETE FROM orders WHERE id = :id"), {"id": order_id})

    assert await _text_count(session) == 0


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "UPDATE order_materials SET processing_status = 'failed', "
            "processing_error = 'x' WHERE id = :id",
            "ck_order_materials_processing_failure_reason_consistent",
        ),
        (
            "UPDATE order_materials SET processing_status = 'processing' WHERE id = :id",
            "ck_order_materials_processing_started_at_consistent",
        ),
        (
            "UPDATE order_materials SET processing_attempts = -1 WHERE id = :id",
            "ck_order_materials_processing_attempts_non_negative",
        ),
        (
            "INSERT INTO order_material_texts (material_id, text, char_count, extractor) "
            "VALUES (:id, 'abc', 2, 'x')",
            "ck_order_material_texts_char_count_matches",
        ),
        (
            "INSERT INTO order_material_texts (material_id, text, char_count, extractor, "
            "page_count) VALUES (:id, 'abc', 3, 'x', 2)",
            "ck_order_material_texts_page_counters_together",
        ),
        (
            "INSERT INTO order_material_texts (material_id, text, char_count, extractor, "
            "page_count, pages_processed) VALUES (:id, 'abc', 3, 'x', 2, 3)",
            "ck_order_material_texts_pages_processed_range",
        ),
    ],
)
async def test_database_constraints(
    session: AsyncSession, storage: LocalFileStorage, sql: str, constraint: str
) -> None:
    order_id = await _order(session)
    material = await _store(session, storage, order_id)

    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            await session.execute(text(sql), {"id": material.id})
