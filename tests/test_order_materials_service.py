from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderMaterial, OrderMaterialText
from app.domain.enums import MaterialFailureReason, MaterialProcessingStatus, OrderStatus
from app.domain.errors import (
    ConsentNotGivenError,
    InvalidMaterialTransitionError,
    InvalidStatusTransitionError,
    MaterialLimitError,
    MaterialNotFoundError,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    MaterialsNotReadyError,
    MaterialsNotReadyReason,
    OrderNotFoundError,
    UnsupportedMaterialError,
)
from app.domain.materials import (
    MAX_MATERIALS_PER_ORDER,
    MAX_PROCESSING_ATTEMPTS,
    MaterialCompletionState,
)
from app.services import order_materials as materials
from app.services import orders
from app.services.material_processing import find_recoverable_materials
from app.services.users import register_or_update_user
from billing_samples import pay_for_order

S = OrderStatus
M = MaterialProcessingStatus
R = MaterialFailureReason
C = MaterialCompletionState
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
    await orders.change_status(session, order.id, S.PAYMENT_PENDING)
    # PAID — только через списание кредитов (OD-4).
    await pay_for_order(session, order.id)
    await orders.change_status(session, order.id, S.COMPLETED)

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


# --- Завершение приёма материалов --------------------------------------------------------


async def _processed(session: AsyncSession, material: OrderMaterial, text: str | None) -> None:
    """Обработанный материал; ``text=None`` — без строки в order_material_texts."""
    await materials.mark_processing(session, material.id)
    await materials.mark_processed(session, material.id)
    if text is not None:
        session.add(
            OrderMaterialText(
                material_id=material.id, text=text, char_count=len(text), extractor="test"
            )
        )
        await session.flush()


async def _failed(
    session: AsyncSession, material: OrderMaterial, reason: R, attempts: int = 1
) -> None:
    await materials.mark_processing(session, material.id)
    await materials.mark_failed(session, material.id, "test failure", reason=reason)
    await session.execute(
        update(OrderMaterial)
        .where(OrderMaterial.id == material.id)
        .values(processing_attempts=attempts)
    )


async def _status(session: AsyncSession, order_id: int) -> OrderStatus:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order.status


async def _not_ready(session: AsyncSession, order_id: int) -> MaterialsNotReadyError:
    with pytest.raises(MaterialsNotReadyError) as caught:
        await materials.finish_materials(session, order_id)
    assert await _status(session, order_id) is S.WAITING_FOR_FILES
    return caught.value


async def test_finish_materials_moves_order_to_analysis(session: AsyncSession) -> None:
    order = await _order(session)
    task = await _add(session, order.id, "task.pdf")
    scan = await _add(session, order.id, "scan.pdf")
    locked = await _add(session, order.id, "locked.pdf")
    await _processed(session, task, "Задание на практику")
    await _processed(session, scan, "")
    await _failed(session, locked, R.ENCRYPTED)

    completion = await materials.finish_materials(session, order.id)

    assert completion.order.status is S.ANALYZING_MATERIALS
    assert await _status(session, order.id) is S.ANALYZING_MATERIALS
    assert completion.materials == [
        materials.MaterialSummary(1, "task.pdf", C.WITH_TEXT),
        materials.MaterialSummary(2, "scan.pdf", C.WITHOUT_TEXT),
        materials.MaterialSummary(3, "locked.pdf", C.FAILED, R.ENCRYPTED),
    ]
    assert [completion.count(state) for state in (C.WITH_TEXT, C.WITHOUT_TEXT, C.FAILED)] == [
        1,
        1,
        1,
    ]


async def test_finish_materials_without_materials(session: AsyncSession) -> None:
    order = await _order(session)

    error = await _not_ready(session, order.id)

    assert error.reason is MaterialsNotReadyReason.NO_MATERIALS


async def test_finish_materials_with_pending_materials(session: AsyncSession) -> None:
    order = await _order(session)
    done = await _add(session, order.id)
    await _processed(session, done, "текст")
    await _add(session, order.id)  # RECEIVED
    in_work = await _add(session, order.id)
    await materials.mark_processing(session, in_work.id)
    retrying = await _add(session, order.id)
    await _failed(session, retrying, R.STORAGE_ERROR, attempts=1)

    error = await _not_ready(session, order.id)

    assert (error.reason, error.pending) == (MaterialsNotReadyReason.PENDING, 3)


async def test_exhausted_retryable_failure_does_not_block(session: AsyncSession) -> None:
    order = await _order(session)
    done = await _add(session, order.id)
    await _processed(session, done, "текст")
    exhausted = await _add(session, order.id)
    await _failed(session, exhausted, R.STORAGE_ERROR, attempts=MAX_PROCESSING_ATTEMPTS)

    completion = await materials.finish_materials(session, order.id)

    assert completion.count(C.FAILED) == 1


async def test_finish_materials_when_all_failed(session: AsyncSession) -> None:
    order = await _order(session)
    for reason in (R.CORRUPTED, R.TIMEOUT):
        await _failed(session, await _add(session, order.id), reason)

    error = await _not_ready(session, order.id)

    assert error.reason is MaterialsNotReadyReason.NO_TEXT


@pytest.mark.parametrize("text_value", ["", None], ids=["empty-text", "no-text-row"])
async def test_processed_without_text_is_not_enough(
    session: AsyncSession, text_value: str | None
) -> None:
    order = await _order(session)
    await _processed(session, await _add(session, order.id), text_value)

    error = await _not_ready(session, order.id)

    assert error.reason is MaterialsNotReadyReason.NO_TEXT


async def test_refusal_changes_nothing(session: AsyncSession) -> None:
    order = await _order(session)
    done = await _add(session, order.id)
    await _processed(session, done, "текст")
    await _add(session, order.id)
    before = _snapshot(await materials.get_order_materials(session, order.id))

    await _not_ready(session, order.id)

    assert _snapshot(await materials.get_order_materials(session, order.id)) == before


@pytest.mark.parametrize(
    "status", [S.WAITING_FOR_CONSENT, S.CANCELLED, S.WAITING_FOR_MISSING_DATA, S.FAILED]
)
async def test_finish_materials_in_wrong_status(session: AsyncSession, status: S) -> None:
    if status in (S.WAITING_FOR_MISSING_DATA, S.FAILED):
        # Приём уже завершён, материалы готовы: карта переходов вела бы обратно в анализ
        # (повторный анализ, возврат из сбоя анализа), но старая кнопка не должна.
        order = await _order(session)
        await _processed(session, await _add(session, order.id), "текст")
        await materials.finish_materials(session, order.id)
        order = await orders.change_status(session, order.id, status)
        if status is S.FAILED:
            assert order.failed_from_status is S.ANALYZING_MATERIALS
    else:
        order = await _order(session, consent=False)
        order = await orders.change_status(session, order.id, S.WAITING_FOR_CONSENT)
        if status is S.CANCELLED:
            await orders.change_status(session, order.id, S.CANCELLED)

    with pytest.raises(InvalidStatusTransitionError):
        await materials.finish_materials(session, order.id)
    assert await _status(session, order.id) is status
    if status is S.FAILED:
        assert order.failed_from_status is S.ANALYZING_MATERIALS


async def test_finish_materials_twice(session: AsyncSession) -> None:
    order = await _order(session)
    await _processed(session, await _add(session, order.id), "текст")
    await materials.finish_materials(session, order.id)

    with pytest.raises(InvalidStatusTransitionError):
        await materials.finish_materials(session, order.id)
    assert await _status(session, order.id) is S.ANALYZING_MATERIALS


async def test_finish_materials_unknown_order(session: AsyncSession) -> None:
    with pytest.raises(OrderNotFoundError):
        await materials.finish_materials(session, 999_999)


async def test_no_files_after_materials_finished(session: AsyncSession) -> None:
    order = await _order(session)
    await _processed(session, await _add(session, order.id), "текст")
    await materials.finish_materials(session, order.id)

    with pytest.raises(MaterialsNotAcceptedError):
        await materials.ensure_can_add_material(
            session, order.id, filename="late.pdf", mime_type=None, size_bytes=10
        )
    with pytest.raises(MaterialsNotAcceptedError):
        await _add(session, order.id, "late.pdf")
    assert await _material_count(session) == 1


async def test_recovery_sees_pending_materials_until_finished(session: AsyncSession) -> None:
    """Незавершённые материалы остаются в зоне восстановления, пока приём не завершён."""
    order = await _order(session)
    await _processed(session, await _add(session, order.id), "текст")
    stale = await _add(session, order.id)
    await materials.mark_processing(session, stale.id)
    await session.execute(
        update(OrderMaterial)
        .where(OrderMaterial.id == stale.id)
        .values(processing_started_at=datetime.now(UTC) - timedelta(minutes=10))
    )
    retrying = await _add(session, order.id)
    await _failed(session, retrying, R.STORAGE_ERROR, attempts=1)

    await _not_ready(session, order.id)
    assert set(await find_recoverable_materials(session, limit=10)) == {stale.id, retrying.id}

    # Обработка закончилась: один сбой окончательный, другой исчерпал попытки.
    await materials.mark_failed(session, stale.id, "interrupted", reason=R.INTERRUPTED)
    await session.execute(
        update(OrderMaterial)
        .where(OrderMaterial.id.in_([stale.id, retrying.id]))
        .values(processing_attempts=MAX_PROCESSING_ATTEMPTS)
    )
    await materials.finish_materials(session, order.id)

    assert await find_recoverable_materials(session, limit=10) == []
