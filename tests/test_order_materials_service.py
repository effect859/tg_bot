import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderMaterial
from app.domain.enums import MaterialProcessingStatus, OrderStatus
from app.domain.errors import (
    ConsentNotGivenError,
    InvalidMaterialTransitionError,
    MaterialLimitError,
    MaterialNotFoundError,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    OrderNotFoundError,
    UnsupportedMaterialError,
)
from app.domain.materials import MAX_MATERIALS_PER_ORDER
from app.services import order_materials as materials
from app.services import orders
from app.services.users import register_or_update_user

S = OrderStatus
M = MaterialProcessingStatus
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FILES = [
    ("requirements.pdf", "application/pdf"),
    ("practice_assignment.docx", DOCX_MIME),
    ("sample_report.pdf", "application/pdf"),
    ("methodology.txt", "text/plain"),
    ("broken.pdf", None),
]
REQUIREMENTS = {"topic": "Анализ бизнес-процессов"}
PLAN = {"sections": [{"title": "Введение"}]}


async def _order(session: AsyncSession, tg_id: int = 1, *, consent: bool = True) -> Order:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    if consent:
        for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
            order = await orders.change_status(session, order.id, status)
    return order


async def _add(
    session: AsyncSession,
    order_id: int,
    filename: str = "requirements.pdf",
    mime_type: str | None = "application/pdf",
    size_bytes: int = 1024,
) -> OrderMaterial:
    return await materials.add_material(
        session, order_id, filename=filename, mime_type=mime_type, size_bytes=size_bytes
    )


async def _confirm_requirements(session: AsyncSession, order_id: int) -> None:
    version = await orders.create_requirements_version(session, order_id, REQUIREMENTS)
    await orders.confirm_requirements_version(session, order_id, version.id)


async def _material_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(OrderMaterial))


async def _insert_material(session: AsyncSession, order_id: int, **overrides: object) -> None:
    """INSERT в обход сервиса — проверка ограничений самой БД."""
    values: dict[str, object] = {
        "order_id": order_id,
        "upload_order": 1,
        "size_bytes": 10,
        "processing_status": "received",
        "processing_error": None,
    }
    values.update(overrides)
    await session.execute(
        text(
            "INSERT INTO order_materials (order_id, upload_order, original_filename, extension, "
            "size_bytes, storage_key, processing_status, processing_error) "
            "VALUES (:order_id, :upload_order, 'a.pdf', 'pdf', :size_bytes, 'k', "
            ":processing_status, :processing_error)"
        ),
        values,
    )


# --- A–D: создание и порядок ---------------------------------------------------------


async def test_add_material(session: AsyncSession) -> None:
    order = await _order(session)

    material = await _add(session, order.id, "Requirements.PDF", "application/pdf", 2048)

    assert material.id is not None
    assert material.order_id == order.id
    assert material.upload_order == 1
    assert material.original_filename == "Requirements.PDF"
    assert material.extension == "pdf"
    assert material.mime_type == "application/pdf"
    assert material.size_bytes == 2048
    assert material.storage_key == f"orders/{order.id}/materials/1.pdf"
    assert material.processing_status is M.RECEIVED
    assert material.processing_error is None
    assert material.created_at is not None
    assert material.updated_at is not None
    assert await materials.get_material(session, material.id) == material


async def test_many_materials_keep_upload_order(session: AsyncSession) -> None:
    order = await _order(session)
    added = [await _add(session, order.id, name, mime) for name, mime in FILES]

    assert [m.upload_order for m in added] == [1, 2, 3, 4, 5]
    # Порядок не зависит от статусов и времени изменения.
    await materials.mark_processing(session, added[0].id)
    await materials.mark_processing(session, added[4].id)
    await materials.mark_failed(session, added[4].id, "cannot open file")

    result = await materials.get_order_materials(session, order.id)
    assert [m.upload_order for m in result] == [1, 2, 3, 4, 5]
    assert [m.original_filename for m in result] == [name for name, _ in FILES]
    assert [m.extension for m in result] == ["pdf", "docx", "pdf", "txt", "pdf"]


async def test_materials_of_different_orders_do_not_conflict(session: AsyncSession) -> None:
    first = await _order(session, tg_id=1)
    second = await _order(session, tg_id=2)

    a1 = await _add(session, first.id)
    b1 = await _add(session, second.id)
    a2 = await _add(session, first.id)

    assert (a1.upload_order, a2.upload_order, b1.upload_order) == (1, 2, 1)
    assert a1.storage_key != b1.storage_key
    assert [m.id for m in await materials.get_order_materials(session, first.id)] == [a1.id, a2.id]
    assert [m.id for m in await materials.get_order_materials(session, second.id)] == [b1.id]


async def test_material_limit(session: AsyncSession) -> None:
    order = await _order(session)
    for _ in range(MAX_MATERIALS_PER_ORDER):
        await _add(session, order.id)

    with pytest.raises(MaterialLimitError):
        await _add(session, order.id)
    assert await _material_count(session) == MAX_MATERIALS_PER_ORDER


# --- E, F: форматы -------------------------------------------------------------------


@pytest.mark.parametrize(("filename", "mime_type"), FILES)
async def test_supported_formats_are_accepted(
    session: AsyncSession, filename: str, mime_type: str | None
) -> None:
    order = await _order(session)

    material = await _add(session, order.id, filename, mime_type)

    assert material.processing_status is M.RECEIVED


@pytest.mark.parametrize(
    ("filename", "mime_type", "size_bytes"),
    [
        ("photo.png", "image/png", 1024),
        ("virus.pdf.exe", None, 1024),
        ("fake.pdf", "image/jpeg", 1024),
        ("empty.txt", "text/plain", 0),
    ],
)
async def test_unsupported_material_is_not_created(
    session: AsyncSession, filename: str, mime_type: str | None, size_bytes: int
) -> None:
    order = await _order(session)

    with pytest.raises(UnsupportedMaterialError):
        await _add(session, order.id, filename, mime_type, size_bytes)
    assert await _material_count(session) == 0


# --- G, H, I: статусы обработки ------------------------------------------------------


async def test_processing_success(session: AsyncSession) -> None:
    material = await _add(session, (await _order(session)).id)

    material = await materials.mark_processing(session, material.id)
    assert material.processing_status is M.PROCESSING
    material = await materials.mark_processed(session, material.id)
    assert material.processing_status is M.PROCESSED
    assert material.processing_error is None


async def test_processing_failure_keeps_error(session: AsyncSession) -> None:
    material = await _add(session, (await _order(session)).id)
    await materials.mark_processing(session, material.id)

    material = await materials.mark_failed(session, material.id, "PDF is encrypted")

    assert material.processing_status is M.FAILED
    assert material.processing_error == "PDF is encrypted"


async def test_failed_material_can_be_reprocessed(session: AsyncSession) -> None:
    material = await _add(session, (await _order(session)).id)
    await materials.mark_processing(session, material.id)
    await materials.mark_failed(session, material.id, "timeout")

    material = await materials.mark_processing(session, material.id)
    assert material.processing_status is M.PROCESSING
    assert material.processing_error is None
    material = await materials.mark_processed(session, material.id)
    assert material.processing_status is M.PROCESSED


async def test_invalid_processing_transitions(session: AsyncSession) -> None:
    material = await _add(session, (await _order(session)).id)

    with pytest.raises(InvalidMaterialTransitionError):
        await materials.mark_processed(session, material.id)
    with pytest.raises(InvalidMaterialTransitionError):
        await materials.mark_failed(session, material.id, "error")
    await session.refresh(material)
    assert material.processing_status is M.RECEIVED

    await materials.mark_processing(session, material.id)
    await materials.mark_processed(session, material.id)
    with pytest.raises(InvalidMaterialTransitionError):
        await materials.mark_processing(session, material.id)
    with pytest.raises(InvalidMaterialTransitionError):
        await materials.mark_failed(session, material.id, "error")
    await session.refresh(material)
    assert material.processing_status is M.PROCESSED


async def test_mark_failed_requires_error_text(session: AsyncSession) -> None:
    material = await _add(session, (await _order(session)).id)
    await materials.mark_processing(session, material.id)

    with pytest.raises(ValueError):
        await materials.mark_failed(session, material.id, "  ")


async def test_unknown_material(session: AsyncSession) -> None:
    with pytest.raises(MaterialNotFoundError):
        await materials.get_material(session, 999_999_999)
    with pytest.raises(MaterialNotFoundError):
        await materials.mark_processing(session, 999_999_999)


# --- J, K: подтверждение ТЗ, согласие и статус заказа --------------------------------


async def test_material_can_be_added_before_requirements_confirmed(
    session: AsyncSession,
) -> None:
    order = await _order(session)
    await orders.create_requirements_version(session, order.id, REQUIREMENTS)

    material = await _add(session, order.id)

    assert material.upload_order == 1


async def test_material_cannot_be_added_after_requirements_confirmed(
    session: AsyncSession,
) -> None:
    order = await _order(session)
    await _add(session, order.id)
    await _confirm_requirements(session, order.id)

    with pytest.raises(MaterialsLockedError):
        await _add(session, order.id)

    # Черновик ТЗ v2 не снимает запрет: подтверждённая версия уже есть.
    await orders.create_requirements_version(session, order.id, REQUIREMENTS)
    with pytest.raises(MaterialsLockedError):
        await _add(session, order.id)
    assert await _material_count(session) == 1


async def test_existing_materials_can_be_processed_after_requirements_confirmed(
    session: AsyncSession,
) -> None:
    order = await _order(session)
    material = await _add(session, order.id)
    await _confirm_requirements(session, order.id)

    await materials.mark_processing(session, material.id)
    material = await materials.mark_processed(session, material.id)

    assert material.processing_status is M.PROCESSED


async def test_material_requires_consent(session: AsyncSession) -> None:
    order = await _order(session, consent=False)

    with pytest.raises(ConsentNotGivenError):
        await _add(session, order.id)

    # Даже если статус выставлен в обход сервиса, без согласия материал не принимается.
    await session.execute(
        text("UPDATE orders SET status = 'waiting_for_files' WHERE id = :id"), {"id": order.id}
    )
    with pytest.raises(ConsentNotGivenError):
        await _add(session, order.id)
    assert await _material_count(session) == 0


async def test_material_rejected_for_cancelled_order(session: AsyncSession) -> None:
    order = await _order(session)
    await orders.change_status(session, order.id, S.CANCELLED)

    with pytest.raises(MaterialsNotAcceptedError):
        await _add(session, order.id)


async def test_material_for_unknown_order(session: AsyncSession) -> None:
    with pytest.raises(OrderNotFoundError):
        await _add(session, 999_999_999)


# --- M: материалы сохраняются при смене статуса заказа ------------------------------


def _snapshot(items: list[OrderMaterial]) -> list[tuple]:
    return [(m.id, m.upload_order, m.original_filename, m.size_bytes, m.storage_key) for m in items]


async def test_materials_survive_order_lifecycle(session: AsyncSession) -> None:
    order = await _order(session)
    for name, mime in FILES:
        await _add(session, order.id, name, mime)
    before = _snapshot(await materials.get_order_materials(session, order.id))

    for status in (S.ANALYZING_MATERIALS, S.WAITING_FOR_DATA_CONFIRMATION):
        await orders.change_status(session, order.id, status)
    await _confirm_requirements(session, order.id)
    for status in (S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION):
        await orders.change_status(session, order.id, status)
    plan = await orders.create_plan_version(session, order.id, PLAN)
    await orders.confirm_plan_version(session, order.id, plan.id)
    for status in (S.PAYMENT_PENDING, S.PAID, S.GENERATING, S.COMPLETED):
        await orders.change_status(session, order.id, status)

    assert _snapshot(await materials.get_order_materials(session, order.id)) == before


async def test_materials_survive_order_cancellation(session: AsyncSession) -> None:
    order = await _order(session)
    await _add(session, order.id)
    await _add(session, order.id, "methodology.txt", "text/plain")
    before = _snapshot(await materials.get_order_materials(session, order.id))

    await orders.change_status(session, order.id, S.CANCELLED)

    assert _snapshot(await materials.get_order_materials(session, order.id)) == before


# --- L, N: ограничения PostgreSQL ----------------------------------------------------


async def test_database_rejects_unknown_order(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="fk_order_materials_order_id_orders"):
        async with session.begin_nested():
            await _insert_material(session, 999_999_999)


async def test_database_rejects_duplicate_upload_order(session: AsyncSession) -> None:
    order = await _order(session)
    await _add(session, order.id)

    with pytest.raises(IntegrityError, match="uq_order_materials_order_id_upload_order"):
        async with session.begin_nested():
            await _insert_material(session, order.id, upload_order=1)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"upload_order": 0}, "ck_order_materials_upload_order_positive"),
        ({"size_bytes": 0}, "ck_order_materials_size_positive"),
        ({"processing_status": "failed"}, "ck_order_materials_processing_error_consistent"),
        (
            {"processing_status": "processed", "processing_error": "x"},
            "ck_order_materials_processing_error_consistent",
        ),
    ],
)
async def test_database_check_constraints(
    session: AsyncSession, overrides: dict[str, object], constraint: str
) -> None:
    order = await _order(session)

    with pytest.raises(IntegrityError, match=constraint):
        async with session.begin_nested():
            await _insert_material(session, order.id, **overrides)


async def test_deleting_order_removes_its_materials(session: AsyncSession) -> None:
    """ON DELETE CASCADE: материалы без заказа не остаются."""
    order = await _order(session)
    await _add(session, order.id)
    await _add(session, order.id)

    await session.execute(text("DELETE FROM orders WHERE id = :id"), {"id": order.id})

    assert await _material_count(session) == 0
