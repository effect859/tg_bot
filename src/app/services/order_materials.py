"""Материалы заказа: приём метаданных и статус обработки.

Сервис не работает с байтами файлов и хранилищем: storage_key — логический
ключ будущего объекта в FileStorage, физическое сохранение — следующий этап.
Коммит — забота вызывающего кода.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderMaterial
from app.domain.enums import MaterialProcessingStatus
from app.domain.errors import (
    ConsentNotGivenError,
    MaterialLimitError,
    MaterialNotFoundError,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
)
from app.domain.materials import (
    MATERIAL_ACCEPTING_STATUSES,
    MAX_MATERIALS_PER_ORDER,
    check_material,
    check_material_transition,
)
from app.services.orders import has_confirmed_requirements, lock_order

M = MaterialProcessingStatus


def material_storage_key(order_id: int, upload_order: int, extension: str) -> str:
    # Имя файла от пользователя в ключ не попадает: оно не контролируется нами.
    return f"orders/{order_id}/materials/{upload_order}.{extension}"


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
    if order.consent_given_at is None:
        raise ConsentNotGivenError(order_id)
    if order.status not in MATERIAL_ACCEPTING_STATUSES:
        raise MaterialsNotAcceptedError(order.status)
    if await has_confirmed_requirements(session, order_id):
        raise MaterialsLockedError(order_id)
    extension = check_material(filename, mime_type, size_bytes)

    count, last = (
        await session.execute(
            select(func.count(), func.coalesce(func.max(OrderMaterial.upload_order), 0)).where(
                OrderMaterial.order_id == order_id
            )
        )
    ).one()
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
