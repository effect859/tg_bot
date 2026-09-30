"""Приём файла сервисом: PostgreSQL + LocalFileStorage во временном каталоге."""

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderMaterial
from app.domain.enums import OrderStatus
from app.domain.errors import (
    ConsentNotGivenError,
    MaterialLimitError,
    MaterialRejectReason,
    MaterialsLockedError,
    MaterialsNotAcceptedError,
    OrderNotFoundError,
    UnsupportedMaterialError,
)
from app.domain.materials import MAX_MATERIAL_SIZE_BYTES, MAX_MATERIALS_PER_ORDER
from app.services import order_materials as materials
from app.services import orders
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from material_samples import DOCX_BYTES, DOCX_MIME, PDF_BYTES, PNG_BYTES, TXT_BYTES, make_zip

S = OrderStatus
REQUIREMENTS = {"topic": "Анализ бизнес-процессов"}


class FailingStorage(LocalFileStorage):
    """Хранилище, которое не может сохранить файл."""

    def __init__(self, root: Path, *, write_before_failing: bool = False) -> None:
        super().__init__(root)
        self.write_before_failing = write_before_failing
        self.save_calls: list[str] = []

    async def save(self, key: str, data: bytes) -> None:
        self.save_calls.append(key)
        if self.write_before_failing:
            await super().save(key, data)
        raise OSError("disk full")


class SpyStorage(LocalFileStorage):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.save_calls: list[str] = []

    async def save(self, key: str, data: bytes) -> None:
        self.save_calls.append(key)
        await super().save(key, data)


@pytest.fixture
def storage(tmp_path: Path) -> SpyStorage:
    return SpyStorage(tmp_path / "storage")


async def _order(session: AsyncSession, tg_id: int = 1, *, consent: bool = True) -> Order:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    order = await orders.change_status(session, order.id, S.WAITING_FOR_CONSENT)
    if consent:
        order = await orders.change_status(session, order.id, S.WAITING_FOR_FILES)
    return order


async def _store(
    session: AsyncSession,
    storage: LocalFileStorage,
    order_id: int,
    filename: str = "task.pdf",
    data: bytes = PDF_BYTES,
    mime_type: str | None = "application/pdf",
) -> OrderMaterial:
    return await materials.store_material(
        session, storage, order_id, filename=filename, mime_type=mime_type, data=data
    )


async def _count(session: AsyncSession, order_id: int) -> int:
    stmt = select(func.count()).where(OrderMaterial.order_id == order_id)
    return await session.scalar(stmt)


def _stored_files(storage_root: Path) -> list[str]:
    if not storage_root.exists():
        return []
    return sorted(p.relative_to(storage_root).as_posix() for p in storage_root.rglob("*.*"))


# --- Успешный приём ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime_type", "data", "extension"),
    [
        ("Задание.pdf", "application/pdf", PDF_BYTES, "pdf"),
        ("Методичка.docx", DOCX_MIME, DOCX_BYTES, "docx"),
        ("notes.txt", "text/plain", TXT_BYTES, "txt"),
        ("no_mime.pdf", None, PDF_BYTES, "pdf"),
    ],
)
async def test_store_material_saves_row_and_file(
    session: AsyncSession,
    storage: SpyStorage,
    filename: str,
    mime_type: str | None,
    data: bytes,
    extension: str,
) -> None:
    order = await _order(session)

    material = await _store(session, storage, order.id, filename, data, mime_type)

    assert material.upload_order == 1
    assert material.extension == extension
    assert material.size_bytes == len(data)
    assert material.original_filename == filename
    assert material.storage_key == f"orders/{order.id}/materials/1.{extension}"
    # Имя пользователя в путь хранения не попадает.
    assert filename not in material.storage_key
    assert await storage.read(material.storage_key) == data


async def test_sequential_files_get_sequential_keys(
    session: AsyncSession, storage: SpyStorage
) -> None:
    order = await _order(session)

    added = [
        await _store(session, storage, order.id, "a.pdf", PDF_BYTES),
        await _store(session, storage, order.id, "b.docx", DOCX_BYTES, DOCX_MIME),
        await _store(session, storage, order.id, "c.txt", TXT_BYTES, "text/plain"),
    ]

    assert [m.upload_order for m in added] == [1, 2, 3]
    for material, data in zip(added, [PDF_BYTES, DOCX_BYTES, TXT_BYTES], strict=True):
        assert await storage.read(material.storage_key) == data


# --- Отказ до записи в хранилище -----------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime_type", "data"),
    [
        ("fake.pdf", "application/pdf", PNG_BYTES),
        ("fake.docx", DOCX_MIME, PDF_BYTES),
        ("zip_only.docx", DOCX_MIME, make_zip({"word/other.xml": "<x/>"})),
        ("binary.txt", "text/plain", PNG_BYTES),
    ],
)
async def test_content_mismatch_creates_nothing(
    session: AsyncSession,
    storage: SpyStorage,
    filename: str,
    mime_type: str,
    data: bytes,
) -> None:
    order = await _order(session)

    with pytest.raises(UnsupportedMaterialError) as exc_info:
        await _store(session, storage, order.id, filename, data, mime_type)

    assert exc_info.value.reason is MaterialRejectReason.CONTENT
    assert await _count(session, order.id) == 0
    assert storage.save_calls == []


async def test_actual_size_is_checked(session: AsyncSession, storage: SpyStorage) -> None:
    order = await _order(session)
    data = PDF_BYTES + b"\x00" * (MAX_MATERIAL_SIZE_BYTES + 1 - len(PDF_BYTES))

    with pytest.raises(UnsupportedMaterialError) as exc_info:
        await _store(session, storage, order.id, data=data)

    assert exc_info.value.reason is MaterialRejectReason.SIZE
    assert storage.save_calls == []


async def test_no_consent_creates_nothing(session: AsyncSession, storage: SpyStorage) -> None:
    order = await _order(session, consent=False)

    with pytest.raises(ConsentNotGivenError):
        await _store(session, storage, order.id)

    assert storage.save_calls == []
    assert await _count(session, order.id) == 0


# --- Окончательная проверка при сохранении --------------------------------------------


async def test_final_check_catches_limit_filled_after_precheck(
    session: AsyncSession, storage: SpyStorage
) -> None:
    order = await _order(session)
    for i in range(MAX_MATERIALS_PER_ORDER - 1):
        await _store(session, storage, order.id, f"{i}.pdf")

    # Две предварительные проверки проходят: место не резервируется.
    for _ in range(2):
        await materials.ensure_can_add_material(
            session, order.id, filename="x.pdf", mime_type=None, size_bytes=10
        )
    await _store(session, storage, order.id, "last.pdf")
    saves_before = len(storage.save_calls)

    with pytest.raises(MaterialLimitError):
        await _store(session, storage, order.id, "extra.pdf")

    assert await _count(session, order.id) == MAX_MATERIALS_PER_ORDER
    assert len(storage.save_calls) == saves_before


async def test_final_check_catches_cancel_after_precheck(
    session: AsyncSession, storage: SpyStorage
) -> None:
    order = await _order(session)
    await materials.ensure_can_add_material(
        session, order.id, filename="a.pdf", mime_type=None, size_bytes=10
    )
    await orders.change_status(session, order.id, S.CANCELLED)

    with pytest.raises(MaterialsNotAcceptedError):
        await _store(session, storage, order.id)

    assert storage.save_calls == []
    assert await _count(session, order.id) == 0


async def test_final_check_catches_requirements_confirmed_after_precheck(
    session: AsyncSession, storage: SpyStorage
) -> None:
    order = await _order(session)
    await materials.ensure_can_add_material(
        session, order.id, filename="a.pdf", mime_type=None, size_bytes=10
    )
    version = await orders.create_requirements_version(session, order.id, REQUIREMENTS)
    await orders.confirm_requirements_version(session, order.id, version.id)

    with pytest.raises(MaterialsLockedError):
        await _store(session, storage, order.id)

    assert storage.save_calls == []


# --- Сбои хранилища и БД --------------------------------------------------------------


@pytest.mark.parametrize("write_before_failing", [False, True])
async def test_storage_failure_rolls_back_material(
    session: AsyncSession, tmp_path: Path, write_before_failing: bool
) -> None:
    root = tmp_path / "storage"
    good = LocalFileStorage(root)
    order = await _order(session)
    first = await _store(session, good, order.id, "first.pdf")
    failing = FailingStorage(root, write_before_failing=write_before_failing)

    with pytest.raises(materials.MaterialStorageError) as exc_info:
        await _store(session, failing, order.id, "second.pdf")

    assert exc_info.value.storage_key == f"orders/{order.id}/materials/2.pdf"
    # Строка второго файла откатилась, частично записанный файл удалён.
    assert await _count(session, order.id) == 1
    assert _stored_files(root) == [f"orders/{order.id}/materials/1.pdf"]
    # Ранее принятый материал не пострадал.
    assert await materials.get_material(session, first.id) == first
    assert await good.read(first.storage_key) == PDF_BYTES
    # После сбоя можно продолжать: номер 2 свободен.
    retry = await _store(session, good, order.id, "second.pdf")
    assert retry.upload_order == 2


async def test_rollback_after_save_leaves_orphan_file(
    session: AsyncSession, storage: SpyStorage, tmp_path: Path
) -> None:
    """Транзакция откатилась уже после записи файла (например, упал commit).

    Сервис не может отличить такой сбой от успешного commit и файл не удаляет:
    остаётся сирота. Строк без файла при этом не бывает.
    """
    root = tmp_path / "storage"
    order_id = (await _order(session)).id
    await _store(session, storage, order_id, "kept.pdf")
    await session.commit()

    orphan = await _store(session, storage, order_id, "lost.pdf", b"%PDF-orphan")
    orphan_key = orphan.storage_key
    await session.rollback()

    assert await _count(session, order_id) == 1
    assert await storage.read(orphan_key) == b"%PDF-orphan"
    # Следующий файл с тем же расширением получает тот же номер и перезаписывает сироту…
    same = await _store(session, storage, order_id, "next.pdf", PDF_BYTES)
    same_key, same_number = same.storage_key, same.upload_order
    assert same_key == orphan_key
    assert await storage.read(same_key) == PDF_BYTES
    # …а с другим расширением — нет: сирот на заказ может быть больше одной.
    await session.rollback()
    other = await _store(session, storage, order_id, "next.txt", TXT_BYTES, "text/plain")
    other_key, other_number = other.storage_key, other.upload_order
    await session.rollback()
    assert other_number == same_number
    assert other_key != same_key
    assert _stored_files(root) == sorted(
        [f"orders/{order_id}/materials/1.pdf", same_key, other_key]
    )


# --- Предварительная проверка и доступ -------------------------------------------------


async def test_precheck_applies_same_rules(session: AsyncSession, storage: SpyStorage) -> None:
    no_consent = await _order(session, tg_id=1, consent=False)
    with pytest.raises(ConsentNotGivenError):
        await materials.ensure_can_add_material(
            session, no_consent.id, filename="a.pdf", mime_type=None, size_bytes=10
        )

    order = await _order(session, tg_id=2)
    assert (
        await materials.ensure_can_add_material(
            session, order.id, filename="a.docx", mime_type=DOCX_MIME, size_bytes=None
        )
        == "docx"
    )
    for filename, size, reason in [
        ("a.png", 10, MaterialRejectReason.FORMAT),
        ("a.pdf", MAX_MATERIAL_SIZE_BYTES + 1, MaterialRejectReason.SIZE),
    ]:
        with pytest.raises(UnsupportedMaterialError) as exc_info:
            await materials.ensure_can_add_material(
                session, order.id, filename=filename, mime_type=None, size_bytes=size
            )
        assert exc_info.value.reason is reason

    version = await orders.create_requirements_version(session, order.id, REQUIREMENTS)
    await orders.confirm_requirements_version(session, order.id, version.id)
    with pytest.raises(MaterialsLockedError):
        await materials.ensure_can_add_material(
            session, order.id, filename="a.pdf", mime_type=None, size_bytes=10
        )

    cancelled = await _order(session, tg_id=3)
    await orders.change_status(session, cancelled.id, S.CANCELLED)
    with pytest.raises(MaterialsNotAcceptedError):
        await materials.ensure_can_add_material(
            session, cancelled.id, filename="a.pdf", mime_type=None, size_bytes=10
        )

    with pytest.raises(OrderNotFoundError):
        await materials.ensure_can_add_material(
            session, 10**9, filename="a.pdf", mime_type=None, size_bytes=10
        )


async def test_precheck_limit(session: AsyncSession, storage: SpyStorage) -> None:
    order = await _order(session)
    for i in range(MAX_MATERIALS_PER_ORDER):
        await _store(session, storage, order.id, f"{i}.pdf")

    with pytest.raises(MaterialLimitError):
        await materials.ensure_can_add_material(
            session, order.id, filename="a.pdf", mime_type=None, size_bytes=10
        )


async def test_get_user_order_hides_foreign_orders(session: AsyncSession) -> None:
    order_a = await _order(session, tg_id=1)
    order_b = await _order(session, tg_id=2)

    assert (await orders.get_user_order(session, order_a.user_id, order_a.id)).id == order_a.id
    with pytest.raises(OrderNotFoundError):
        await orders.get_user_order(session, order_a.user_id, order_b.id)
    with pytest.raises(OrderNotFoundError):
        await orders.get_user_order(session, order_a.user_id, 10**9)
