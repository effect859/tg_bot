"""Обработка материалов: извлечение текста и сохранение результата.

Один материал обрабатывается так:

1. Место в очереди извлечения (TextExtractor.slot). Берётся до захвата материала:
   иначе материал висел бы в PROCESSING, пока ждёт очереди. Не дождались —
   материал остаётся как был и обрабатывается при следующем вызове.
2. Захват (короткая транзакция, commit): блокировка строки, проверка статусов
   материала и заказа, переход в PROCESSING с номером попытки.
3. Без транзакции и блокировок: чтение файла и извлечение в дочернем процессе.
4. Сохранение (короткая транзакция, commit): под блокировкой строки результат
   принимается, только если материал всё ещё в PROCESSING с той же попыткой.
   Иначе (обработку признали прерванной и взяли заново) результат отбрасывается.

Файл материала здесь только читается: никакой сбой обработки его не удаляет и не
меняет. Повторяются только сбои из RETRYABLE_FAILURE_REASONS, не больше
MAX_PROCESSING_ATTEMPTS попыток. Фоновых задач нет: ожидающие материалы
обрабатываются, когда пользователь присылает следующий файл.

В отличие от остальных сервисов, здесь commit делается внутри: транзакция не
должна быть открыта во время извлечения.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderMaterial, OrderMaterialText
from app.domain.enums import MaterialFailureReason, MaterialProcessingStatus
from app.domain.materials import (
    MATERIAL_ACCEPTING_STATUSES,
    MAX_PROCESSING_ATTEMPTS,
    RETRYABLE_FAILURE_REASONS,
    STALE_PROCESSING_AFTER,
    is_retryable_failure,
)
from app.extraction.base import (
    Extraction,
    ExtractionFailure,
    ExtractionResult,
    ExtractorBusyError,
    TextExtractor,
)
from app.services.order_materials import apply_material_status, lock_material
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

M = MaterialProcessingStatus
R = MaterialFailureReason

MAX_ERROR_LENGTH = 1000


class OutcomeKind(StrEnum):
    PROCESSED = "processed"
    FAILED = "failed"
    # Очередь извлечения занята: материал не взят в работу.
    BUSY = "busy"
    # Брать в работу нечего: уже обработан, обрабатывается, заказ не принимает материалы.
    SKIPPED = "skipped"
    # Результат устарел: обработку признали прерванной и взяли заново.
    DISCARDED = "discarded"


@dataclass(frozen=True)
class ProcessingOutcome:
    kind: OutcomeKind
    material_id: int
    upload_order: int
    original_filename: str
    failure_reason: MaterialFailureReason | None = None
    # Будет ли следующая попытка (только для FAILED).
    will_retry: bool = False
    result: ExtractionResult | None = None
    # Заказ всё ещё принимает материалы: есть смысл сообщать пользователю.
    order_active: bool = True


@dataclass(frozen=True)
class _Claim:
    material_id: int
    attempt: int
    storage_key: str
    extension: str


async def process_material(
    session: AsyncSession,
    storage: FileStorage,
    extractor: TextExtractor,
    material_id: int,
) -> ProcessingOutcome:
    try:
        async with extractor.slot():
            claim, outcome = await _claim(session, material_id)
            if claim is None:
                assert outcome is not None
                return outcome
            extraction = await _read_and_extract(storage, extractor, claim)
            return await _finish(session, claim, extraction)
    except ExtractorBusyError:
        material = await session.get(OrderMaterial, material_id)
        assert material is not None
        logger.warning("Extraction queue is busy, material left as is: id=%s", material_id)
        return _outcome(material, OutcomeKind.BUSY)


async def process_pending_materials(
    session: AsyncSession,
    storage: FileStorage,
    extractor: TextExtractor,
    order_id: int,
) -> list[ProcessingOutcome]:
    """Обрабатывает материалы заказа, которые ждут обработки, в порядке загрузки.

    Ждут: новые (RECEIVED), прерванные (PROCESSING дольше STALE_PROCESSING_AFTER) и
    упавшие с повторяемой причиной, пока не исчерпаны попытки. Если очередь занята,
    остальные материалы не трогаются — их обработает следующий вызов.
    """
    stale_before = datetime.now(UTC) - STALE_PROCESSING_AFTER
    stmt = (
        select(OrderMaterial.id)
        .where(
            OrderMaterial.order_id == order_id,
            or_(
                OrderMaterial.processing_status == M.RECEIVED,
                (OrderMaterial.processing_status == M.PROCESSING)
                & (OrderMaterial.processing_started_at < stale_before),
                (OrderMaterial.processing_status == M.FAILED)
                & OrderMaterial.failure_reason.in_(RETRYABLE_FAILURE_REASONS)
                & (OrderMaterial.processing_attempts < MAX_PROCESSING_ATTEMPTS),
            ),
        )
        .order_by(OrderMaterial.upload_order)
    )
    material_ids = list(await session.scalars(stmt))
    await session.commit()

    outcomes: list[ProcessingOutcome] = []
    for material_id in material_ids:
        outcome = await process_material(session, storage, extractor, material_id)
        outcomes.append(outcome)
        if outcome.kind is OutcomeKind.BUSY:
            break
    return outcomes


async def get_material_text(session: AsyncSession, material_id: int) -> OrderMaterialText | None:
    return await session.get(OrderMaterialText, material_id)


async def _claim(
    session: AsyncSession, material_id: int
) -> tuple[_Claim | None, ProcessingOutcome | None]:
    material = await lock_material(session, material_id)
    order = await session.get(Order, material.order_id, populate_existing=True)
    assert order is not None

    interrupted = _is_stale(material)
    if interrupted:
        # PROCESSING -> FAILED — обычный переход; дальше — обычный повтор.
        logger.warning(
            "Material processing interrupted: id=%s attempt=%s",
            material.id,
            material.processing_attempts,
        )
        apply_material_status(
            material, M.FAILED, error="processing interrupted", reason=R.INTERRUPTED
        )

    order_active = order.status in MATERIAL_ACCEPTING_STATUSES
    claimable = material.processing_status is M.RECEIVED or (
        material.processing_status is M.FAILED
        and is_retryable_failure(material.failure_reason, material.processing_attempts)
    )
    if not order_active or not claimable:
        await session.flush()
        # Прерванная обработка без права на повтор — такой же сбой, как остальные.
        kind = OutcomeKind.FAILED if interrupted else OutcomeKind.SKIPPED
        outcome = _outcome(material, kind, order_active=order_active)
        await session.commit()
        return None, outcome

    apply_material_status(material, M.PROCESSING)
    claim = _Claim(
        material_id=material.id,
        attempt=material.processing_attempts,
        storage_key=material.storage_key,
        extension=material.extension,
    )
    await session.commit()
    return claim, None


def _is_stale(material: OrderMaterial) -> bool:
    started = material.processing_started_at
    return (
        material.processing_status is M.PROCESSING
        and started is not None
        and started < datetime.now(UTC) - STALE_PROCESSING_AFTER
    )


async def _read_and_extract(
    storage: FileStorage, extractor: TextExtractor, claim: _Claim
) -> Extraction:
    try:
        data = await storage.read(claim.storage_key)
    except FileNotFoundError:
        # Строка материала есть, а файла нет: нарушение целостности, не ошибка извлечения.
        logger.error(
            "Material file is missing: id=%s storage_key=%s", claim.material_id, claim.storage_key
        )
        return ExtractionFailure(R.FILE_MISSING, f"file missing: {claim.storage_key}")
    except Exception as exc:
        logger.exception("Material file read failed: id=%s", claim.material_id)
        return ExtractionFailure(R.STORAGE_ERROR, f"storage read: {type(exc).__name__}")
    return await extractor.extract(claim.extension, data)


async def _finish(
    session: AsyncSession, claim: _Claim, extraction: Extraction
) -> ProcessingOutcome:
    material = await lock_material(session, claim.material_id)
    if (
        material.processing_status is not M.PROCESSING
        or material.processing_attempts != claim.attempt
    ):
        logger.warning(
            "Stale extraction result discarded: id=%s attempt=%s current=%s/%s",
            claim.material_id,
            claim.attempt,
            material.processing_status,
            material.processing_attempts,
        )
        outcome = _outcome(material, OutcomeKind.DISCARDED)
        await session.commit()
        return outcome

    if isinstance(extraction, ExtractionResult):
        session.add(
            OrderMaterialText(
                material_id=material.id,
                text=extraction.text,
                char_count=len(extraction.text),
                page_count=extraction.page_count,
                pages_processed=extraction.pages_processed,
                truncation_reason=extraction.truncation,
                warnings=[w.to_json() for w in extraction.warnings],
                extractor=extraction.extractor,
            )
        )
        apply_material_status(material, M.PROCESSED)
        kind = OutcomeKind.PROCESSED
    else:
        log = logger.warning if extraction.reason in RETRYABLE_FAILURE_REASONS else logger.info
        log(
            "Material extraction failed: id=%s attempt=%s reason=%s detail=%s",
            material.id,
            claim.attempt,
            extraction.reason,
            extraction.detail,
        )
        apply_material_status(
            material,
            M.FAILED,
            error=(extraction.detail or extraction.reason.value)[:MAX_ERROR_LENGTH],
            reason=extraction.reason,
        )
        kind = OutcomeKind.FAILED
    await session.flush()

    order = await session.get(Order, material.order_id, populate_existing=True)
    assert order is not None
    # Заказ могли отменить во время извлечения: результат — свойство материала и
    # сохраняется, но сообщать о нём пользователю уже незачем.
    outcome = _outcome(
        material,
        kind,
        result=extraction if isinstance(extraction, ExtractionResult) else None,
        order_active=order.status in MATERIAL_ACCEPTING_STATUSES,
    )
    await session.commit()
    return outcome


def _outcome(
    material: OrderMaterial,
    kind: OutcomeKind,
    *,
    result: ExtractionResult | None = None,
    order_active: bool = True,
) -> ProcessingOutcome:
    reason = material.failure_reason if kind is OutcomeKind.FAILED else None
    return ProcessingOutcome(
        kind=kind,
        material_id=material.id,
        upload_order=material.upload_order,
        original_filename=material.original_filename,
        failure_reason=reason,
        will_retry=kind is OutcomeKind.FAILED
        and is_retryable_failure(reason, material.processing_attempts),
        result=result,
        order_active=order_active,
    )
