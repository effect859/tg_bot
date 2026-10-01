"""Извлечение текста после приёма файла в боте.

Message и Bot — моки, реальный Telegram Bot API не вызывается. БД — PostgreSQL,
хранилище — LocalFileStorage во временном каталоге. Часть тестов запускает
настоящий дочерний процесс извлечения.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Document, File
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, start
from app.bot.handlers.order_intake import receive_document
from app.bot.setup import create_dispatcher
from app.db.models import User
from app.domain.enums import (
    MaterialFailureReason,
    MaterialProcessingStatus,
    OrderStatus,
    TextTruncationReason,
)
from app.extraction.base import (
    ExtractionFailure,
    ExtractionResult,
    ExtractionWarning,
    TextExtractor,
    WarningCode,
)
from app.extraction.runner import SubprocessTextExtractor
from app.services import material_processing, orders
from app.services import order_materials as materials
from app.services.material_processing import OutcomeKind, ProcessingOutcome
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from fake_extractor import FakeExtractor
from pdf_samples import fixture

S = OrderStatus
M = MaterialProcessingStatus
R = MaterialFailureReason


@pytest.fixture
def file_storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "storage")


async def _user_with_order(session: AsyncSession) -> tuple[User, int]:
    user = await register_or_update_user(
        session, tg_id=1, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
        order = await orders.change_status(session, order.id, status)
    return user, order.id


def _message(filename: str, mime_type: str | None, size: int) -> AsyncMock:
    message = AsyncMock()
    message.document = Document(
        file_id="file-id",
        file_unique_id="file-uid",
        file_name=filename,
        mime_type=mime_type,
        file_size=size,
    )
    return message


def _bot(data: bytes) -> AsyncMock:
    bot = AsyncMock()
    bot.get_file.return_value = File(
        file_id="file-id", file_unique_id="file-uid", file_size=len(data), file_path="docs/1"
    )

    async def download_file(file_path: str, destination: object, **kwargs: object) -> object:
        destination.write(data)  # type: ignore[attr-defined]
        return destination

    bot.download_file.side_effect = download_file
    return bot


async def _send(
    session: AsyncSession,
    user: User,
    storage: LocalFileStorage,
    extractor: TextExtractor | None,
    filename: str,
    data: bytes,
    mime_type: str | None = None,
) -> list[str]:
    message = _message(filename, mime_type, len(data))
    await receive_document(message, session, user, _bot(data), storage, extractor)
    return [call.args[0] for call in message.answer.await_args_list]


# --- С настоящим дочерним процессом ---------------------------------------------------------


async def test_txt_is_accepted_then_extracted(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, order_id = await _user_with_order(session)
    data = "Тема: анализ процессов".encode()

    replies = await _send(session, user, file_storage, SubprocessTextExtractor(), "t.txt", data)

    assert replies == [
        texts.material_accepted("t.txt", 1),
        "📄 Файл «t.txt»: текст извлечён.",
    ]
    [material] = await materials.get_order_materials(session, order_id)
    assert material.processing_status is M.PROCESSED
    saved = await material_processing.get_material_text(session, material.id)
    assert saved is not None and saved.text == "Тема: анализ процессов"
    assert await file_storage.read(material.storage_key) == data


async def test_scanned_pdf_gets_honest_hint(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, order_id = await _user_with_order(session)

    replies = await _send(
        session, user, file_storage, SubprocessTextExtractor(), "scan.pdf",
        fixture("pdf/image_only.pdf"), "application/pdf",
    )  # fmt: skip

    assert replies[1].startswith("⚠️ Файл «scan.pdf»: текст не найден.")
    assert "скан" in replies[1]
    [material] = await materials.get_order_materials(session, order_id)
    # Отсутствие текста — не сбой: материал обработан.
    assert material.processing_status is M.PROCESSED


async def test_encrypted_pdf_failure_keeps_material_and_file(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, order_id = await _user_with_order(session)
    data = fixture("pdf/enc_user_aes256.pdf")

    replies = await _send(
        session, user, file_storage, SubprocessTextExtractor(), "secret.pdf", data,
        "application/pdf",
    )  # fmt: skip

    assert replies[0] == texts.material_accepted("secret.pdf", 1)
    assert replies[1] == texts.material_failed(
        "secret.pdf", texts.MATERIAL_FAILURE_TEXTS[R.ENCRYPTED]
    )
    assert "err_code" not in replies[1]
    [material] = await materials.get_order_materials(session, order_id)
    assert material.processing_status is M.FAILED
    assert material.failure_reason is R.ENCRYPTED
    assert await file_storage.read(material.storage_key) == data


# --- С подменой извлечения ------------------------------------------------------------------


async def test_without_extractor_files_are_only_accepted(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, order_id = await _user_with_order(session)

    replies = await _send(session, user, file_storage, None, "t.txt", b"text")

    assert replies == [texts.material_accepted("t.txt", 1)]
    [material] = await materials.get_order_materials(session, order_id)
    assert material.processing_status is M.RECEIVED


async def test_temporary_failure_is_retried_with_next_file(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, order_id = await _user_with_order(session)
    extractor = FakeExtractor(
        ExtractionFailure(R.EXTRACTOR_UNAVAILABLE, "spawn"),
        ExtractionResult(text="первый", extractor="fake"),
        ExtractionResult(text="второй", extractor="fake"),
    )

    first = await _send(session, user, file_storage, extractor, "a.txt", b"a")
    second = await _send(session, user, file_storage, extractor, "b.txt", b"b")

    assert first[1] == texts.material_failed("a.txt", texts.MATERIAL_RETRY_LATER)
    assert second == [
        texts.material_accepted("b.txt", 2),
        "📄 Файл «a.txt»: текст извлечён.",
        "📄 Файл «b.txt»: текст извлечён.",
    ]
    statuses = [m.processing_status for m in await materials.get_order_materials(session, order_id)]
    assert statuses == [M.PROCESSED, M.PROCESSED]


async def test_database_error_during_processing_keeps_accepted_file(
    session: AsyncSession, file_storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, order_id = await _user_with_order(session)

    async def broken(*args: object, **kwargs: object) -> object:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    monkeypatch.setattr("app.bot.handlers.order_intake.process_pending_materials", broken)

    replies = await _send(session, user, file_storage, FakeExtractor(), "t.txt", b"text")

    assert replies == [texts.material_accepted("t.txt", 1), texts.MATERIAL_PROCESSING_DEFERRED]
    [material] = await materials.get_order_materials(session, order_id)
    assert await file_storage.read(material.storage_key) == b"text"


async def test_unexpected_error_during_processing_is_reported(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, _ = await _user_with_order(session)
    extractor = FakeExtractor()

    async def boom() -> None:
        raise RuntimeError("unexpected")

    extractor.on_extract = boom

    replies = await _send(session, user, file_storage, extractor, "t.txt", b"text")

    assert replies == [texts.material_accepted("t.txt", 1), texts.MATERIAL_PROCESSING_DEFERRED]


async def test_busy_queue_message(session: AsyncSession, file_storage: LocalFileStorage) -> None:
    user, _ = await _user_with_order(session)
    extractor = FakeExtractor()
    extractor.busy = True

    replies = await _send(session, user, file_storage, extractor, "t.txt", b"text")

    assert replies[1].startswith("⏳ Файл «t.txt» ждёт обработки")


async def test_filename_is_escaped_in_result(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user, _ = await _user_with_order(session)

    replies = await _send(session, user, file_storage, FakeExtractor(), "<b>x</b>.txt", b"t")

    assert replies[1] == "📄 Файл «&lt;b&gt;x&lt;/b&gt;.txt»: текст извлечён."


def test_dispatcher_provides_text_extractor(
    sessionmaker: async_sessionmaker[AsyncSession],
    file_storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Роутеры — глобальные объекты и подключаются только к одному диспетчеру;
    # другие тесты уже могли создать свой. Связь восстанавливается после теста.
    for router in (start.router, menu.router, order_intake.router):
        monkeypatch.setattr(router, "_parent_router", None)
    extractor = FakeExtractor()
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), file_storage, extractor)

    assert dispatcher["text_extractor"] is extractor


# --- Тексты сообщений -----------------------------------------------------------------------


def _outcome(**overrides: object) -> ProcessingOutcome:
    values: dict[str, object] = {
        "kind": OutcomeKind.PROCESSED,
        "material_id": 1,
        "upload_order": 1,
        "original_filename": "doc.pdf",
        "result": ExtractionResult(text="t", extractor="x", page_count=5, pages_processed=5),
    }
    values.update(overrides)
    return ProcessingOutcome(**values)  # type: ignore[arg-type]


def test_result_messages_for_partial_documents() -> None:
    result = ExtractionResult(
        text="t",
        extractor="x",
        truncation=TextTruncationReason.PAGE_LIMIT,
        page_count=350,
        pages_processed=300,
        warnings=(
            ExtractionWarning(WarningCode.PAGES_WITHOUT_TEXT, (2, 3, 4, 9)),
            ExtractionWarning(WarningCode.IMAGE_ONLY_PAGES, (2,)),
            ExtractionWarning(WarningCode.LOW_QUALITY_TEXT, (5,)),
        ),
    )
    text = texts.material_processing_result(_outcome(result=result))

    assert text is not None
    assert text.startswith("📄 Файл «doc.pdf»: текст извлечён (350 стр.).")
    assert "На страницах 2–4, 9 текста нет — возможно, это сканы." in text
    assert texts.TEXT_LOW_QUALITY in text
    assert "Обработаны только первые 300 страниц из 350." in text


def test_blank_pages_are_not_called_scans_in_message() -> None:
    result = ExtractionResult(
        text="",
        extractor="x",
        page_count=2,
        pages_processed=2,
        warnings=(ExtractionWarning(WarningCode.NO_TEXT),),
    )
    text = texts.material_processing_result(_outcome(result=result))

    assert text is not None
    assert "скан" not in text


@pytest.mark.parametrize("reason", list(MaterialFailureReason))
@pytest.mark.parametrize("will_retry", [False, True])
def test_failure_messages_have_no_internals(
    reason: MaterialFailureReason, will_retry: bool
) -> None:
    outcome = _outcome(
        kind=OutcomeKind.FAILED, failure_reason=reason, will_retry=will_retry, result=None
    )
    text = texts.material_processing_result(outcome)

    assert text is not None and text.startswith("❌ Файл «doc.pdf»: ")
    assert reason.value not in text
    if will_retry:
        assert texts.MATERIAL_RETRY_LATER in text


@pytest.mark.parametrize("kind", [OutcomeKind.SKIPPED, OutcomeKind.DISCARDED])
def test_nothing_to_report(kind: OutcomeKind) -> None:
    assert texts.material_processing_result(_outcome(kind=kind)) is None


def test_inactive_order_is_not_notified() -> None:
    assert texts.material_processing_result(_outcome(order_active=False)) is None
