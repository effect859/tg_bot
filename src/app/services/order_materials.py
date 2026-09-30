"""Материалы заказа: приём файлов и статус обработки.

add_material регистрирует метаданные материала; store_material дополнительно
проверяет содержимое и сохраняет байты в FileStorage. Путь хранения — storage_key,
имя файла пользователя в него не попадает. Коммит — забота вызывающего кода.
"""

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderMaterial
from app.domain.enums import MaterialProcessingStatus
from app.domain.errors import (
    ConsentNotGivenError,
    MaterialLimitError,
    MaterialNotFoundError,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    OrderNotFoundError,
)
from app.domain.material_content import check_material_content
from app.domain.materials import (
    MATERIAL_ACCEPTING_STATUSES,
    MAX_MATERIALS_PER_ORDER,
    check_material,
    check_material_transition,
)
from app.services.orders import has_confirmed_requirements, lock_order
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

M = MaterialProcessingStatus


class MaterialStorageError(Exception):
    """Хранилище не смогло сохранить файл материала; материал не создан."""

    def __init__(self, storage_key: str) -> None:
        super().__init__(f"Failed to save material file {storage_key!r}")
        self.storage_key = storage_key


def material_storage_key(order_id: int, upload_order: int, extension: str) -> str:
    # Имя файла от пользователя в ключ не попадает: оно не контролируется нами.
    return f"orders/{order_id}/materials/{upload_order}.{extension}"


async def ensure_can_add_material(
    session: AsyncSession,
    order_id: int,
    *,
    filename: str,
    mime_type: str | None,
    size_bytes: int | None,
) -> str:
    """Предварительная проверка до скачивания файла: заведомо отклоняемое не качаем.

    Правила те же, что в add_material, но заказ не блокируется и место под файл не
    резервируется: пока файл скачивается, условия могут измениться (параллельный
    файл займёт последнее место, заказ отменят). Окончательное решение принимает
    add_material. ``size_bytes=None`` — Telegram не сообщил размер.

    Возвращает нормализованное расширение.
    """
    order = await session.get(Order, order_id, populate_existing=True)
    if order is None:
        raise OrderNotFoundError(order_id)
    await _check_order_accepts_materials(session, order)
    extension = check_material(filename, mime_type, size_bytes)
    count, _ = await _material_stats(session, order_id)
    if count >= MAX_MATERIALS_PER_ORDER:
        raise MaterialLimitError(order_id, MAX_MATERIALS_PER_ORDER)
    return extension


async def add_material(
    session: AsyncSession,
    order_id: int,
    *,
    filename: str,
    mime_type: str | None,
    size_bytes: int,
) -> OrderMaterial:
    """Регистрирует новый материал заказа в статусе RECEIVED.

    Блокировка заказа упорядочивает параллельные добавления (upload_order) и
    исключает гонку с подтверждением ТЗ, которое блокирует ту же строку.
    """
    order = await lock_order(session, order_id)
    await _check_order_accepts_materials(session, order)
    extension = check_material(filename, mime_type, size_bytes)

    count, last = await _material_stats(session, order_id)
    if count >= MAX_MATERIALS_PER_ORDER:
        raise MaterialLimitError(order_id, MAX_MATERIALS_PER_ORDER)

    upload_order = last + 1
    material = OrderMaterial(
        order_id=order_id,
        upload_order=upload_order,
        original_filename=filename.strip(),
        extension=extension,
        mime_type=mime_type,
        size_bytes=size_bytes,
        storage_key=material_storage_key(order_id, upload_order, extension),
        processing_status=M.RECEIVED,
    )
    session.add(material)
    await session.flush()
    await session.refresh(material)
    return material


async def store_material(
    session: AsyncSession,
    storage: FileStorage,
    order_id: int,
    *,
    filename: str,
    mime_type: str | None,
    data: bytes,
) -> OrderMaterial:
    """Проверяет скачанный файл, регистрирует материал и сохраняет байты в хранилище.

    Порядок: метаданные и содержимое (без БД) → SAVEPOINT: add_material (блокировка
    заказа и окончательная проверка согласия, статуса, ТЗ и лимита) → запись файла.
    Если что-то не удалось, SAVEPOINT откатывается, а уже записанный файл удаляется:
    его ключ принадлежит незафиксированной строке, и ни одна зафиксированная запись
    на него не ссылается. Ранее принятые материалы не затрагиваются.

    Файл пишется, пока заказ заблокирован: параллельный файл того же заказа ждёт
    commit и получает следующий upload_order, а не тот же ключ.

    Вызывающий код должен сделать commit. Сбой самого commit здесь не
    компенсируется: он мог фактически завершиться, и удаление файла оставило бы
    зафиксированную строку без файла.

    Raises UnsupportedMaterialError, доменные ошибки add_material, MaterialStorageError,
    ошибки БД.
    """
    extension = check_material(filename, mime_type, len(data))
    # До 20 МиБ: разбор ZIP и декодирование текста не должны блокировать event loop.
    await asyncio.to_thread(check_material_content, extension, data)

    storage_key: str | None = None
    try:
        async with session.begin_nested():
            material = await add_material(
                session, order_id, filename=filename, mime_type=mime_type, size_bytes=len(data)
            )
            storage_key = material.storage_key
            try:
                await storage.save(storage_key, data)
            except Exception as exc:
                raise MaterialStorageError(storage_key) from exc
    except BaseException:
        if storage_key is not None:
            await _discard_file(storage, storage_key)
        raise
    return material


async def _check_order_accepts_materials(session: AsyncSession, order: Order) -> None:
    if order.consent_given_at is None:
        raise ConsentNotGivenError(order.id)
    if order.status not in MATERIAL_ACCEPTING_STATUSES:
        raise MaterialsNotAcceptedError(order.status)
    if await has_confirmed_requirements(session, order.id):
        raise MaterialsLockedError(order.id)


async def _material_stats(session: AsyncSession, order_id: int) -> tuple[int, int]:
    """Число материалов заказа и наибольший upload_order (0, если материалов нет)."""
    stmt = select(func.count(), func.coalesce(func.max(OrderMaterial.upload_order), 0)).where(
        OrderMaterial.order_id == order_id
    )
    count, last = (await session.execute(stmt)).one()
    return count, last


async def _discard_file(storage: FileStorage, storage_key: str) -> None:
    try:
        await storage.delete(storage_key)
    except Exception:
        # Не маскируем исходную ошибку: сирота останется, ключ — в логе.
        logger.exception("Failed to delete file of rolled back material: %s", storage_key)


async def get_order_materials(session: AsyncSession, order_id: int) -> list[OrderMaterial]:
    stmt = (
        select(OrderMaterial)
        .where(OrderMaterial.order_id == order_id)
        .order_by(OrderMaterial.upload_order)
    )
    return list(await session.scalars(stmt))


async def get_material(session: AsyncSession, material_id: int) -> OrderMaterial:
    material = await session.get(OrderMaterial, material_id)
    if material is None:
        raise MaterialNotFoundError(material_id)
    return material


async def mark_processing(session: AsyncSession, material_id: int) -> OrderMaterial:
    """Начало обработки; из FAILED — повторная попытка, прошлая ошибка сбрасывается."""
    return await _transition(session, material_id, M.PROCESSING)


async def mark_processed(session: AsyncSession, material_id: int) -> OrderMaterial:
    return await _transition(session, material_id, M.PROCESSED)


async def mark_failed(session: AsyncSession, material_id: int, error: str) -> OrderMaterial:
    if not error.strip():
        raise ValueError("Processing error description must not be empty")
    return await _transition(session, material_id, M.FAILED, error=error)


async def _transition(
    session: AsyncSession,
    material_id: int,
    target: MaterialProcessingStatus,
    *,
    error: str | None = None,
) -> OrderMaterial:
    stmt = (
        select(OrderMaterial)
        .where(OrderMaterial.id == material_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    material = await session.scalar(stmt)
    if material is None:
        raise MaterialNotFoundError(material_id)
    check_material_transition(material.processing_status, target)
    material.processing_status = target
    material.processing_error = error
    await session.flush()
    await session.refresh(material)
    return material
