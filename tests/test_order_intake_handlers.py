"""Хендлеры оформления заказа и приёма файлов.

Message, CallbackQuery и Bot — моки: проверяется наша логика поверх aiogram,
реальный Telegram Bot API не вызывается. БД — настоящая PostgreSQL-сессия,
хранилище — LocalFileStorage во временном каталоге.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup, GetFile
from aiogram.types import Chat, Document, File, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, requirements_dialog, start
from app.bot.handlers.order_intake import (
    cancel_order,
    finish_order_materials,
    give_consent,
    new_report,
    receive_document,
    retry_analysis,
)
from app.bot.keyboards import OrderAction, OrderCallback
from app.bot.setup import create_dispatcher, notify_analysis_outcome, notify_material_outcome
from app.db.models import LLMCall, Order, OrderMaterial, OrderMaterialText, User
from app.domain.enums import LLMCallStatus, MaterialFailureReason, OrderStatus
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
from app.domain.requirements import MAX_ANALYSIS_LLM_CALLS
from app.extraction.base import ExtractionResult
from app.llm.base import LLMPermanentError
from app.services import order_materials as materials
from app.services import orders
from app.services.material_processing import OutcomeKind, ProcessingOutcome
from app.services.requirements_analysis import (
    LLM_STEP,
    AnalysisOutcome,
    AnalysisOutcomeKind,
    analyze_requirements,
)
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from fake_llm import FakeLLMClient, response_json
from fake_llm import result as llm_result
from material_samples import DOCX_BYTES, DOCX_MIME, PDF_BYTES, PNG_BYTES, TXT_BYTES, make_zip

S = OrderStatus
REQUIREMENTS = {"topic": "Анализ бизнес-процессов"}
BOT_TOKEN = "123456:SECRET-TOKEN"


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def file_storage(storage_root: Path) -> LocalFileStorage:
    return LocalFileStorage(storage_root)


async def _user(session: AsyncSession, tg_id: int = 1) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


async def _order(session: AsyncSession, user: User, *, consent: bool = True) -> Order:
    order = await orders.create_order(session, user_id=user.id)
    order = await orders.change_status(session, order.id, S.WAITING_FOR_CONSENT)
    if consent:
        order = await orders.change_status(session, order.id, S.WAITING_FOR_FILES)
    return order


def _message(
    filename: str | None = "task.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int | None = 100,
) -> AsyncMock:
    message = AsyncMock()
    message.document = Document(
        file_id="file-id",
        file_unique_id="file-uid",
        file_name=filename,
        mime_type=mime_type,
        file_size=file_size,
    )
    return message


def _bot(data: bytes | None = None, *, chunks: list[bytes] | None = None) -> AsyncMock:
    """Bot, у которого get_file/download_file отдают ``data`` (или ``chunks``)."""
    bot = AsyncMock()
    bot.get_file.return_value = File(
        file_id="file-id",
        file_unique_id="file-uid",
        file_size=None if data is None else len(data),
        file_path="documents/file_1",
    )
    bot.download_file.side_effect = _streams(chunks if chunks is not None else [data or b""])
    return bot


def _streams(chunks: list[bytes], before: Callable[[], Any] | None = None) -> Callable[..., Any]:
    async def download_file(file_path: str, destination: BinaryIO, **kwargs: object) -> BinaryIO:
        if before is not None:
            await before()
        for chunk in chunks:
            destination.write(chunk)
        return destination

    return download_file


def _callback(tg_id: int = 1) -> AsyncMock:
    callback = AsyncMock()
    callback.message = None
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


def _reply(message: AsyncMock) -> str:
    message.answer.assert_awaited_once()
    return message.answer.await_args.args[0]


async def _materials(session: AsyncSession, order_id: int) -> list[OrderMaterial]:
    return await materials.get_order_materials(session, order_id)


def _files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


async def _receive(
    session: AsyncSession,
    user: User,
    bot: AsyncMock,
    file_storage: LocalFileStorage,
    message: AsyncMock,
) -> str:
    await receive_document(message, session, user, bot, file_storage)
    return _reply(message)


# --- Новый отчёт, согласие, отмена ---------------------------------------------------


async def test_new_report_creates_order_waiting_for_consent(session: AsyncSession) -> None:
    user = await _user(session)
    message = AsyncMock()

    await new_report(message, session, user)

    order = await orders.get_active_order(session, user.id)
    assert order is not None
    assert order.status is S.WAITING_FOR_CONSENT
    assert _reply(message) == texts.CONSENT_REQUEST
    markup: InlineKeyboardMarkup = message.answer.await_args.kwargs["reply_markup"]
    actions = [OrderCallback.unpack(b.callback_data) for row in markup.inline_keyboard for b in row]
    assert actions == [
        OrderCallback(action=OrderAction.CONSENT, order_id=order.id),
        OrderCallback(action=OrderAction.CANCEL, order_id=order.id),
    ]


async def test_new_report_twice_does_not_create_second_order(session: AsyncSession) -> None:
    user = await _user(session)
    await new_report(AsyncMock(), session, user)
    message = AsyncMock()

    await new_report(message, session, user)

    count = await session.scalar(select(func.count()).where(Order.user_id == user.id))
    assert count == 1
    assert _reply(message) == texts.CONSENT_REQUEST


async def test_new_report_reminds_about_order_waiting_for_files(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    message = AsyncMock()

    await new_report(message, session, user)

    assert f"Заказ #{order.id} ждёт материалы" in _reply(message)


async def test_consent_moves_order_to_waiting_for_files(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user, consent=False)
    callback, bot = _callback(), AsyncMock()

    await give_consent(
        callback,
        OrderCallback(action=OrderAction.CONSENT, order_id=order.id),
        session,
        user,
        bot,
    )

    await session.refresh(order)
    assert order.status is S.WAITING_FOR_FILES
    assert order.consent_given_at is not None
    callback.answer.assert_awaited_once_with()
    assert bot.send_message.await_args.args == (user.tg_id, texts.FILES_REQUEST)


async def test_repeated_consent_is_rejected_without_change(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    consent_at = order.consent_given_at
    callback, bot = _callback(), AsyncMock()

    await give_consent(
        callback, OrderCallback(action=OrderAction.CONSENT, order_id=order.id), session, user, bot
    )

    await session.refresh(order)
    assert order.status is S.WAITING_FOR_FILES
    assert order.consent_given_at == consent_at
    text = callback.answer.await_args.args[0]
    assert text == texts.action_outdated(order)
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("action", [OrderAction.CONSENT, OrderAction.CANCEL])
async def test_buttons_of_foreign_order_do_nothing(
    session: AsyncSession, action: OrderAction
) -> None:
    owner = await _user(session, tg_id=1)
    stranger = await _user(session, tg_id=2)
    order = await _order(session, owner, consent=False)
    callback, bot = _callback(tg_id=2), AsyncMock()
    handler = give_consent if action is OrderAction.CONSENT else cancel_order

    await handler(callback, OrderCallback(action=action, order_id=order.id), session, stranger, bot)

    await session.refresh(order)
    assert order.status is S.WAITING_FOR_CONSENT
    callback.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    bot.send_message.assert_not_awaited()


async def test_cancel_order(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    callback, bot = _callback(), AsyncMock()

    await cancel_order(
        callback, OrderCallback(action=OrderAction.CANCEL, order_id=order.id), session, user, bot
    )

    await session.refresh(order)
    assert order.status is S.CANCELLED
    assert order.cancelled_at is not None
    assert bot.send_message.await_args.args == (user.tg_id, texts.order_cancelled(order))
    # После отмены можно начать новый заказ.
    assert await orders.get_active_order(session, user.id) is None


async def test_repeated_cancel_is_rejected(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    data = OrderCallback(action=OrderAction.CANCEL, order_id=order.id)
    await cancel_order(_callback(), data, session, user, AsyncMock())
    callback, bot = _callback(), AsyncMock()

    await cancel_order(callback, data, session, user, bot)

    await session.refresh(order)
    assert order.status is S.CANCELLED
    assert callback.answer.await_args.args[0] == texts.action_outdated(order)
    bot.send_message.assert_not_awaited()


async def test_consent_after_cancel_is_rejected(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user, consent=False)
    cancel = OrderCallback(action=OrderAction.CANCEL, order_id=order.id)
    await cancel_order(_callback(), cancel, session, user, AsyncMock())
    callback = _callback()

    await give_consent(
        callback,
        OrderCallback(action=OrderAction.CONSENT, order_id=order.id),
        session,
        user,
        AsyncMock(),
    )

    await session.refresh(order)
    assert order.status is S.CANCELLED
    assert order.consent_given_at is None
    assert "отменён" in callback.answer.await_args.args[0]


# --- Приём документов: успех ----------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime_type", "data", "extension"),
    [
        ("Задание.pdf", "application/pdf", PDF_BYTES, "pdf"),
        ("Методичка.docx", DOCX_MIME, DOCX_BYTES, "docx"),
        ("notes.txt", "text/plain", TXT_BYTES, "txt"),
        ("unknown_mime.pdf", None, PDF_BYTES, "pdf"),
    ],
)
async def test_document_is_accepted(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    filename: str,
    mime_type: str | None,
    data: bytes,
    extension: str,
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    message = _message(filename, mime_type, len(data))

    reply = await _receive(session, user, _bot(data), file_storage, message)

    assert reply == texts.material_accepted(filename, 1)
    [material] = await _materials(session, order.id)
    assert material.storage_key == f"orders/{order.id}/materials/1.{extension}"
    assert await file_storage.read(material.storage_key) == data


async def test_three_files_in_a_row(session: AsyncSession, file_storage: LocalFileStorage) -> None:
    user = await _user(session)
    order = await _order(session, user)
    replies = []
    for name, mime, data in [
        ("a.pdf", "application/pdf", PDF_BYTES),
        ("b.docx", DOCX_MIME, DOCX_BYTES),
        ("c.txt", "text/plain", TXT_BYTES),
    ]:
        replies.append(
            await _receive(session, user, _bot(data), file_storage, _message(name, mime, len(data)))
        )

    assert replies == [
        texts.material_accepted("a.pdf", 1),
        texts.material_accepted("b.docx", 2),
        texts.material_accepted("c.txt", 3),
    ]
    assert [m.upload_order for m in await _materials(session, order.id)] == [1, 2, 3]


async def test_filename_is_html_escaped(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    name = '<b>x</b> & "y".pdf'

    reply = await _receive(session, user, _bot(PDF_BYTES), file_storage, _message(name))

    assert "&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;.pdf" in reply
    assert "<b>x</b>" not in reply
    [material] = await _materials(session, order.id)
    # В БД хранится исходное имя, в путь хранения оно не попадает.
    assert material.original_filename == name
    assert "<" not in material.storage_key


async def test_long_filename_is_shortened_in_reply(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user = await _user(session)
    await _order(session, user)
    name = "&" * 200 + ".pdf"

    reply = await _receive(session, user, _bot(PDF_BYTES), file_storage, _message(name))

    assert "&amp;" * (texts.FILENAME_PREVIEW_LENGTH - 1) + "…" in reply
    # Не разрезали HTML-сущность.
    assert "&amp…" not in reply and "&a…" not in reply


# --- Приём документов: отказ без скачивания -------------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime_type", "file_size", "expected"),
    [
        ("photo.png", "image/png", 100, texts.MATERIAL_UNKNOWN_FORMAT),
        ("archive.zip", "application/zip", 100, texts.MATERIAL_UNKNOWN_FORMAT),
        (None, "application/pdf", 100, texts.MATERIAL_BAD_FILENAME),
        ("fake.pdf", "image/jpeg", 100, texts.MATERIAL_MIME_MISMATCH),
        ("big.pdf", "application/pdf", MAX_MATERIAL_SIZE_BYTES + 1, texts.MATERIAL_TOO_LARGE),
        ("empty.pdf", "application/pdf", 0, texts.MATERIAL_EMPTY),
    ],
)
async def test_rejected_before_download(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    storage_root: Path,
    filename: str | None,
    mime_type: str,
    file_size: int,
    expected: str,
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    bot = _bot(PDF_BYTES)

    reply = await _receive(
        session, user, bot, file_storage, _message(filename, mime_type, file_size)
    )

    assert reply == texts.material_failed(texts.file_label(filename), expected)
    bot.get_file.assert_not_awaited()
    bot.download_file.assert_not_awaited()
    assert await _materials(session, order.id) == []
    assert _files(storage_root) == []


async def test_no_consent(session: AsyncSession, file_storage: LocalFileStorage) -> None:
    user = await _user(session)
    order = await _order(session, user, consent=False)
    bot, message = _bot(PDF_BYTES), _message()

    reply = await _receive(session, user, bot, file_storage, message)

    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_NO_CONSENT)
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert OrderCallback.unpack(markup.inline_keyboard[0][0].callback_data) == OrderCallback(
        action=OrderAction.CONSENT, order_id=order.id
    )
    bot.get_file.assert_not_awaited()
    assert await _materials(session, order.id) == []


async def test_requirements_confirmed(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    version = await orders.create_requirements_version(session, order.id, REQUIREMENTS)
    await orders.confirm_requirements_version(session, order.id, version.id)
    bot = _bot(PDF_BYTES)

    reply = await _receive(session, user, bot, file_storage, _message())

    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_LOCKED)
    bot.get_file.assert_not_awaited()


async def test_material_limit(session: AsyncSession, file_storage: LocalFileStorage) -> None:
    user = await _user(session)
    order = await _order(session, user)
    for i in range(MAX_MATERIALS_PER_ORDER):
        await materials.store_material(
            session, file_storage, order.id, filename=f"{i}.pdf", mime_type=None, data=PDF_BYTES
        )
    bot = _bot(PDF_BYTES)

    reply = await _receive(session, user, bot, file_storage, _message())

    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_LIMIT)
    bot.get_file.assert_not_awaited()
    assert len(await _materials(session, order.id)) == MAX_MATERIALS_PER_ORDER


async def test_no_active_order(session: AsyncSession, file_storage: LocalFileStorage) -> None:
    user = await _user(session)
    bot = _bot(PDF_BYTES)

    reply = await _receive(session, user, bot, file_storage, _message())

    assert reply == texts.NO_ACTIVE_ORDER
    bot.get_file.assert_not_awaited()


async def test_document_never_goes_to_foreign_order(
    session: AsyncSession, file_storage: LocalFileStorage, storage_root: Path
) -> None:
    owner = await _user(session, tg_id=1)
    stranger = await _user(session, tg_id=2)
    order = await _order(session, owner)
    bot = _bot(PDF_BYTES)

    reply = await _receive(session, stranger, bot, file_storage, _message())

    assert reply == texts.NO_ACTIVE_ORDER
    assert await _materials(session, order.id) == []
    assert _files(storage_root) == []


# --- Приём документов: отказ после скачивания -----------------------------------------


@pytest.mark.parametrize(
    ("filename", "mime_type", "data"),
    [
        ("fake.pdf", "application/pdf", PNG_BYTES),
        ("fake.docx", DOCX_MIME, PDF_BYTES),
        # ZIP-сигнатура есть, обязательных частей DOCX нет.
        ("zip.docx", DOCX_MIME, make_zip({"hello.txt": "hi"})),
        ("binary.txt", "text/plain", PNG_BYTES),
    ],
)
async def test_content_mismatch(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    storage_root: Path,
    filename: str,
    mime_type: str,
    data: bytes,
) -> None:
    user = await _user(session)
    order = await _order(session, user)

    reply = await _receive(
        session, user, _bot(data), file_storage, _message(filename, mime_type, len(data))
    )

    assert reply == texts.material_failed(filename, texts.MATERIAL_BAD_CONTENT)
    assert await _materials(session, order.id) == []
    assert _files(storage_root) == []


@pytest.mark.parametrize("declared_size", [None, 100])
async def test_actual_size_over_limit(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    storage_root: Path,
    declared_size: int | None,
) -> None:
    # Telegram не сообщил размер или сообщил неверный: обрываем поток по байтам.
    user = await _user(session)
    order = await _order(session, user)
    chunk = b"x" * (1024 * 1024)
    bot = _bot(chunks=[PDF_BYTES] + [chunk] * 21)
    bot.get_file.return_value = File(
        file_id="file-id", file_unique_id="u", file_size=declared_size, file_path="p"
    )

    reply = await _receive(session, user, bot, file_storage, _message(file_size=declared_size))

    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_TOO_LARGE)
    assert await _materials(session, order.id) == []
    assert _files(storage_root) == []


async def test_final_check_after_download_cancelled_order(
    session: AsyncSession, file_storage: LocalFileStorage, storage_root: Path
) -> None:
    """Предварительная проверка прошла, но пока файл скачивался, заказ отменили."""
    user = await _user(session)
    order = await _order(session, user)
    bot = _bot(PDF_BYTES)

    async def cancel() -> None:
        await orders.change_status(session, order.id, S.CANCELLED)

    bot.download_file.side_effect = _streams([PDF_BYTES], before=cancel)

    reply = await _receive(session, user, bot, file_storage, _message())

    assert "сейчас файлы не принимаются" in reply
    assert "отменён" in reply
    assert await _materials(session, order.id) == []
    assert _files(storage_root) == []


async def test_final_check_after_download_limit_reached(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    """Пока файл скачивался, параллельный файл занял последнее место."""
    user = await _user(session)
    order = await _order(session, user)
    for i in range(MAX_MATERIALS_PER_ORDER - 1):
        await materials.store_material(
            session, file_storage, order.id, filename=f"{i}.pdf", mime_type=None, data=PDF_BYTES
        )
    bot = _bot(PDF_BYTES)

    async def fill_last_slot() -> None:
        await materials.store_material(
            session, file_storage, order.id, filename="race.pdf", mime_type=None, data=PDF_BYTES
        )

    bot.download_file.side_effect = _streams([PDF_BYTES], before=fill_last_slot)

    reply = await _receive(session, user, bot, file_storage, _message())

    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_LIMIT)
    assert len(await _materials(session, order.id)) == MAX_MATERIALS_PER_ORDER


# --- Технические сбои -----------------------------------------------------------------


async def test_download_failure_keeps_previous_materials(
    session: AsyncSession, file_storage: LocalFileStorage, caplog: pytest.LogCaptureFixture
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    await _receive(session, user, _bot(PDF_BYTES), file_storage, _message("first.pdf"))
    bot = _bot(PDF_BYTES)
    bot.get_file.side_effect = TelegramNetworkError(
        method=GetFile(file_id="file-id"),
        message=f"https://api.telegram.org/file/bot{BOT_TOKEN}/documents/file_1",
    )

    with caplog.at_level(logging.WARNING):
        reply = await _receive(session, user, bot, file_storage, _message("second.pdf"))

    assert reply == texts.material_failed("second.pdf", texts.MATERIAL_DOWNLOAD_FAILED)
    [material] = await _materials(session, order.id)
    assert material.original_filename == "first.pdf"
    assert await file_storage.read(material.storage_key) == PDF_BYTES
    # Токен бота из текста исключения не попадает ни в лог, ни пользователю.
    assert BOT_TOKEN not in caplog.text
    assert BOT_TOKEN not in reply


class _BrokenStorage(LocalFileStorage):
    async def save(self, key: str, data: bytes) -> None:
        raise OSError(f"[Errno 28] No space left on device: 'C:\\\\srv\\\\storage\\\\{key}'")


async def test_storage_failure(
    session: AsyncSession, file_storage: LocalFileStorage, storage_root: Path
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    await _receive(session, user, _bot(PDF_BYTES), file_storage, _message("first.pdf"))
    broken = _BrokenStorage(storage_root)

    reply = await _receive(session, user, _bot(PDF_BYTES), broken, _message("second.pdf"))

    assert reply == texts.material_failed("second.pdf", texts.MATERIAL_STORAGE_FAILED)
    assert "No space" not in reply and "srv" not in reply
    assert [m.original_filename for m in await _materials(session, order.id)] == ["first.pdf"]
    assert _files(storage_root) == [f"orders/{order.id}/materials/1.pdf"]


async def test_database_failure(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id = order.id
    await _receive(session, user, _bot(PDF_BYTES), file_storage, _message("first.pdf"))

    async def broken_store(*args: object, **kwargs: object) -> None:
        raise OperationalError("INSERT ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr(order_intake, "store_material", broken_store)

    reply = await _receive(session, user, _bot(PDF_BYTES), file_storage, _message("second.pdf"))

    assert reply == texts.material_failed("second.pdf", texts.MATERIAL_DB_FAILED)
    assert "INSERT" not in reply and "server closed" not in reply
    assert [m.original_filename for m in await _materials(session, order_id)] == ["first.pdf"]


@pytest.mark.parametrize("committed", [True, False])
async def test_commit_outcome_unknown_keeps_file(
    session: AsyncSession,
    file_storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    committed: bool,
) -> None:
    """Финальный commit выбросил ошибку; на сервере он мог пройти или не пройти.

    Моделируется подменой session.commit: для committed=True commit выполняется,
    затем выбрасывается ошибка (обрыв соединения после фиксации).
    """
    user = await _user(session)
    order = await _order(session, user)
    order_id, user_id = order.id, user.id
    real_commit = session.commit
    calls = 0

    async def flaky_commit() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:  # commit после предварительной проверки
            await real_commit()
            return
        if committed:
            await real_commit()
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(session, "commit", flaky_commit)

    with caplog.at_level(logging.ERROR):
        reply = await _receive(session, user, _bot(PDF_BYTES), file_storage, _message())

    storage_key = f"orders/{order_id}/materials/1.pdf"
    assert reply == texts.material_failed("task.pdf", texts.MATERIAL_COMMIT_UNKNOWN)
    assert "COMMIT" not in reply and "connection lost" not in reply
    # Файл не удалён ни в одном из исходов.
    assert await file_storage.read(storage_key) == PDF_BYTES
    materials_now = await _materials(session, order_id)
    assert len(materials_now) == (1 if committed else 0)
    # В логе достаточно контекста для разбора.
    [record] = [r for r in caplog.records if "commit outcome unknown" in r.getMessage()]
    for value in (f"order_id={order_id}", f"user_id={user_id}", storage_key, "upload_order=1"):
        assert value in record.getMessage()
    assert record.exc_info is not None


async def test_replies_have_no_internal_details(
    session: AsyncSession, file_storage: LocalFileStorage, storage_root: Path
) -> None:
    user = await _user(session)
    await _order(session, user)
    replies = [
        await _receive(session, user, _bot(PDF_BYTES), file_storage, _message()),
        await _receive(session, user, _bot(PNG_BYTES), file_storage, _message("x.pdf")),
    ]

    for reply in replies:
        assert "Traceback" not in reply
        assert "orders/" not in reply
        assert str(storage_root) not in reply


# --- Тексты и подключение ------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.FORMAT), "формат"),
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.FILENAME), "имя"),
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.MIME), "тип файла"),
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.EMPTY), "пустой"),
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.SIZE), "большой"),
        (UnsupportedMaterialError("x", reason=MaterialRejectReason.CONTENT), "содержимое"),
        (MaterialLimitError(1, 20), "лимит"),
        (ConsentNotGivenError(1), "согласие"),
        (MaterialsLockedError(1), "подтверждены"),
        (MaterialsNotAcceptedError(S.PAID), "оплачен"),
        (OrderNotFoundError(1), "не принят"),
    ],
)
def test_rejection_texts(error: Exception, expected: str) -> None:
    text = texts.material_rejection(error)  # type: ignore[arg-type]
    assert expected in text
    assert str(error) not in text


def test_dispatcher_provides_file_storage(
    sessionmaker: async_sessionmaker[AsyncSession], file_storage: LocalFileStorage
) -> None:
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), file_storage)

    assert dispatcher["file_storage"] is file_storage
    assert order_intake.router in dispatcher.sub_routers


# --- Завершение приёма материалов -----------------------------------------------------


def _finish_data(order_id: int) -> OrderCallback:
    return OrderCallback(action=OrderAction.FINISH_MATERIALS, order_id=order_id)


def _actions(markup: InlineKeyboardMarkup) -> list[OrderAction]:
    return [
        OrderCallback.unpack(b.callback_data).action for row in markup.inline_keyboard for b in row
    ]


async def _material(
    session: AsyncSession,
    order_id: int,
    filename: str,
    *,
    text: str | None = "Задание на практику",
    failure: MaterialFailureReason | None = None,
) -> None:
    """Материал в окончательном состоянии: с текстом, без текста или с ошибкой."""
    material = await materials.add_material(
        session, order_id, filename=filename, mime_type=None, size_bytes=10
    )
    await materials.mark_processing(session, material.id)
    if failure is not None:
        await materials.mark_failed(session, material.id, "test failure", reason=failure)
        return
    await materials.mark_processed(session, material.id)
    if text is not None:
        session.add(
            OrderMaterialText(
                material_id=material.id, text=text, char_count=len(text), extractor="test"
            )
        )
        await session.flush()


async def _order_status(session: AsyncSession, order_id: int) -> OrderStatus:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order.status


class _CallLog:
    """Порядок обращений к БД и к Telegram в хендлере."""

    def __init__(self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name in ("commit", "rollback"):
            original = getattr(session, name)
            monkeypatch.setattr(session, name, self._wrap(name, original))

    def _wrap(self, name: str, original: Callable[[], Any]) -> Callable[[], Any]:
        async def wrapper() -> None:
            self.calls.append(name)
            await original()

        return wrapper

    def telegram(self, mock: AsyncMock, name: str) -> None:
        mock.side_effect = lambda *args, **kwargs: self.calls.append(name)


async def test_finish_materials_moves_order_to_analysis(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id, chat_id = order.id, user.tg_id
    await _material(session, order_id, "task.pdf")
    await _material(session, order_id, "<b>scan</b>.pdf", text="")
    await _material(session, order_id, "locked.pdf", failure=MaterialFailureReason.ENCRYPTED)
    await session.commit()
    callback, bot = _callback(), AsyncMock()
    log = _CallLog(session, monkeypatch)
    log.telegram(callback.answer, "answer")
    log.telegram(bot.send_message, "send")

    await finish_order_materials(callback, _finish_data(order_id), session, user, bot)

    assert await _order_status(session, order_id) is S.ANALYZING_MATERIALS
    # Переход зафиксирован до любых обращений к Telegram.
    assert log.calls == ["commit", "answer", "send"]
    callback.answer.assert_awaited_once_with()
    chat, text = bot.send_message.await_args.args
    assert chat == chat_id
    assert f"Материалы по заказу #{order_id} приняты." in text
    assert "Файлов: 3 · текст извлечён: 1 · без текста: 1 · с ошибкой: 1" in text
    assert f"«&lt;b&gt;scan&lt;/b&gt;.pdf» — {texts.TEXT_NOT_FOUND}" in text
    locked_reason = texts.MATERIAL_FAILURE_TEXTS[MaterialFailureReason.ENCRYPTED]
    assert f"«locked.pdf» — {locked_reason}" in text
    assert text.endswith("Следующий шаг — анализ материалов. Сообщим, когда он будет готов.")
    assert "encrypted" not in text and "test failure" not in text
    # Сообщение недоступно (callback.message is None): снимать кнопки не с чего.
    bot.edit_message_reply_markup.assert_not_awaited()


def _pressed_message() -> Message:
    return Message(
        message_id=77,
        date=datetime(2026, 10, 1, tzinfo=UTC),
        chat=Chat(id=1, type="private"),
    )


async def test_finish_materials_removes_keyboard(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    await _material(session, order.id, "task.pdf")
    await session.commit()
    callback, bot = _callback(), AsyncMock()
    callback.message = _pressed_message()

    await finish_order_materials(callback, _finish_data(order.id), session, user, bot)

    bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=1, message_id=77, reply_markup=None
    )
    bot.send_message.assert_awaited_once()


async def test_keyboard_removal_failure_keeps_transition(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id = order.id
    await _material(session, order_id, "task.pdf")
    await session.commit()
    callback, bot = _callback(), AsyncMock()
    callback.message = _pressed_message()
    bot.edit_message_reply_markup.side_effect = TelegramBadRequest(
        method=EditMessageReplyMarkup(chat_id=1, message_id=77),
        message="Bad Request: message is not modified",
    )

    await finish_order_materials(callback, _finish_data(order_id), session, user, bot)

    assert await _order_status(session, order_id) is S.ANALYZING_MATERIALS
    bot.send_message.assert_awaited_once()


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", texts.MATERIALS_NONE),
        ("pending", "⏳ Ещё обрабатываются файлы: 1."),
        ("no_text", texts.MATERIALS_NO_TEXT),
    ],
)
async def test_materials_not_ready_is_refused_with_rollback_first(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, setup: str, expected: str
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id = order.id
    if setup == "pending":
        await _material(session, order_id, "task.pdf")
        await materials.add_material(
            session, order_id, filename="new.pdf", mime_type=None, size_bytes=10
        )
    elif setup == "no_text":
        await _material(session, order_id, "bad.pdf", failure=MaterialFailureReason.CORRUPTED)
    await session.commit()
    callback, bot = _callback(), AsyncMock()
    log = _CallLog(session, monkeypatch)
    log.telegram(callback.answer, "answer")

    await finish_order_materials(callback, _finish_data(order_id), session, user, bot)

    assert log.calls == ["rollback", "answer"]
    text = callback.answer.await_args.args[0]
    assert text.startswith(expected)
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    assert await _order_status(session, order_id) is S.WAITING_FOR_FILES
    bot.send_message.assert_not_awaited()


async def test_repeated_finish_is_reported_as_already_done(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id = order.id
    await _material(session, order_id, "task.pdf")
    await session.commit()
    await finish_order_materials(_callback(), _finish_data(order_id), session, user, AsyncMock())
    callback, bot = _callback(), AsyncMock()
    log = _CallLog(session, monkeypatch)
    log.telegram(callback.answer, "answer")

    await finish_order_materials(callback, _finish_data(order_id), session, user, bot)

    assert log.calls == ["rollback", "answer"]
    assert callback.answer.await_args.args[0] == f"Материалы по заказу #{order_id} уже приняты."
    assert await _order_status(session, order_id) is S.ANALYZING_MATERIALS
    bot.send_message.assert_not_awaited()


async def test_finish_button_of_cancelled_order_is_outdated(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user)
    order_id = order.id
    await _material(session, order_id, "task.pdf")
    order = await orders.change_status(session, order_id, S.CANCELLED)
    expected = texts.action_outdated(order)
    await session.commit()
    callback = _callback()

    await finish_order_materials(callback, _finish_data(order_id), session, user, AsyncMock())

    assert callback.answer.await_args.args[0] == expected
    assert await _order_status(session, order_id) is S.CANCELLED


async def test_finish_button_of_foreign_order_does_nothing(session: AsyncSession) -> None:
    owner = await _user(session, tg_id=1)
    stranger = await _user(session, tg_id=2)
    order = await _order(session, owner)
    order_id = order.id
    await _material(session, order_id, "task.pdf")
    await session.commit()
    callback, bot = _callback(tg_id=2), AsyncMock()

    await finish_order_materials(callback, _finish_data(order_id), session, stranger, bot)

    callback.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    assert await _order_status(session, order_id) is S.WAITING_FOR_FILES
    bot.send_message.assert_not_awaited()


async def test_accepted_file_reply_has_finish_button(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    message = _message("task.pdf", "application/pdf", len(PDF_BYTES))

    await _receive(session, user, _bot(PDF_BYTES), file_storage, message)

    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _actions(markup) == [OrderAction.FINISH_MATERIALS, OrderAction.CANCEL]
    assert OrderCallback.unpack(markup.inline_keyboard[0][0].callback_data).order_id == order.id


async def test_reminder_has_finish_button(session: AsyncSession) -> None:
    user = await _user(session)
    await _order(session, user)
    message = AsyncMock()

    await new_report(message, session, user)

    markup = message.answer.await_args.kwargs["reply_markup"]
    assert _actions(markup) == [OrderAction.FINISH_MATERIALS, OrderAction.CANCEL]


async def test_files_request_after_consent_has_only_cancel(session: AsyncSession) -> None:
    user = await _user(session)
    order = await _order(session, user, consent=False)
    bot = AsyncMock()
    data = OrderCallback(action=OrderAction.CONSENT, order_id=order.id)

    await give_consent(_callback(), data, session, user, bot)

    assert _actions(bot.send_message.await_args.kwargs["reply_markup"]) == [OrderAction.CANCEL]


async def test_background_notification_has_no_buttons() -> None:
    bot = AsyncMock()
    outcome = ProcessingOutcome(
        kind=OutcomeKind.PROCESSED,
        material_id=1,
        upload_order=1,
        original_filename="task.pdf",
        result=ExtractionResult(text="t", extractor="x"),
    )

    await notify_material_outcome(bot, 1, outcome)

    assert bot.send_message.await_args.kwargs == {}


async def test_file_after_finish_is_rejected(
    session: AsyncSession, file_storage: LocalFileStorage
) -> None:
    user = await _user(session)
    order = await _order(session, user)
    await _material(session, order.id, "task.pdf")
    await materials.finish_materials(session, order.id)
    message = _message("late.pdf", "application/pdf", len(PDF_BYTES))

    reply = await _receive(session, user, _bot(PDF_BYTES), file_storage, message)

    assert reply == texts.material_failed(
        "late.pdf", "сейчас файлы не принимаются: заказ на шаге «анализ материалов»."
    )
    assert len(await _materials(session, order.id)) == 1


# --- Анализ материалов --------------------------------------------------------------

ANALYSIS_MATERIAL = "Тема: Анализ бизнес-процессов отдела продаж. Место прохождения: ООО «Ёлка»."
ANALYSIS_GOOD = llm_result(
    response_json(
        topic=(
            "Анализ бизнес-процессов отдела продаж",
            "Тема: Анализ бизнес-процессов отдела продаж.",
        ),
        practice_place=("ООО «Ёлка»", "Место прохождения: ООО «Ёлка»"),
    )
)


async def _ready_order(session: AsyncSession, user: User) -> int:
    order = await _order(session, user)
    order_id = order.id
    await _material(session, order_id, "task.pdf", text=ANALYSIS_MATERIAL)
    await session.commit()
    return order_id


async def _failed_analysis(session: AsyncSession, user: User) -> int:
    order_id = await _ready_order(session, user)
    await materials.finish_materials(session, order_id)
    await session.commit()
    await analyze_requirements(session, FakeLLMClient(LLMPermanentError("http_400")), order_id)
    return order_id


def _sent(bot: AsyncMock) -> list[str]:
    return [call.args[1] for call in bot.send_message.await_args_list]


async def test_finish_materials_runs_analysis(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _ready_order(session, user)
    bot = AsyncMock()
    llm = FakeLLMClient(ANALYSIS_GOOD)

    await finish_order_materials(_callback(), _finish_data(order_id), session, user, bot, llm)

    sent = _sent(bot)
    assert sent[0].startswith(f"📥 Материалы по заказу #{order_id} приняты.")
    assert sent[1] == texts.ANALYSIS_STARTED
    assert sent[2].startswith(f"📋 Материалы по заказу #{order_id} проанализированы.")
    assert "Найдено в материалах: тема практики, место практики." in sent[2]
    assert "Нужно уточнить: вид практики, дата начала практики" in sent[2]
    assert await _order_status(session, order_id) is S.WAITING_FOR_MISSING_DATA
    assert len(llm.calls) == 1


async def test_failed_analysis_offers_retry_and_cancel(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _ready_order(session, user)
    bot = AsyncMock()
    llm = FakeLLMClient(LLMPermanentError("http_401"))

    await finish_order_materials(_callback(), _finish_data(order_id), session, user, bot, llm)

    text = bot.send_message.await_args.args[1]
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert text == texts.analysis_failed(order_id, can_retry=True)
    assert "http_401" not in text
    assert _actions(markup) == [OrderAction.RETRY_ANALYSIS, OrderAction.CANCEL]
    assert await _order_status(session, order_id) is S.FAILED


async def test_retry_analysis_restarts_and_analyzes(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_analysis(session, user)
    callback, bot = _callback(), AsyncMock()
    llm = FakeLLMClient(ANALYSIS_GOOD)

    await retry_analysis(callback, _retry_data(order_id), session, user, bot, llm)

    callback.answer.assert_awaited_once_with()
    assert _sent(bot)[0] == texts.ANALYSIS_STARTED
    assert _sent(bot)[1].startswith(f"📋 Материалы по заказу #{order_id} проанализированы.")
    assert await _order_status(session, order_id) is S.WAITING_FOR_MISSING_DATA


def _retry_data(order_id: int) -> OrderCallback:
    return OrderCallback(action=OrderAction.RETRY_ANALYSIS, order_id=order_id)


async def test_retry_refusals_roll_back_before_answer(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _user(session)
    order_id = await _failed_analysis(session, user)
    session.add_all(
        LLMCall(
            order_id=order_id,
            step=LLM_STEP,
            provider="fake",
            model="m",
            status=LLMCallStatus.ERROR,
            error="http_503",
        )
        for _ in range(MAX_ANALYSIS_LLM_CALLS - 1)  # плюс один уже сделанный — лимит
    )
    await session.commit()
    callback, bot = _callback(), AsyncMock()
    log = _CallLog(session, monkeypatch)
    log.telegram(callback.answer, "answer")
    llm = FakeLLMClient(ANALYSIS_GOOD)

    await retry_analysis(callback, _retry_data(order_id), session, user, bot, llm)

    assert log.calls == ["rollback", "answer"]
    assert callback.answer.await_args.args[0] == texts.ANALYSIS_LIMIT_REACHED
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    assert await _order_status(session, order_id) is S.FAILED
    assert llm.calls == []
    bot.send_message.assert_not_awaited()


async def test_retry_button_after_analysis_is_outdated(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_analysis(session, user)
    await retry_analysis(
        _callback(), _retry_data(order_id), session, user, AsyncMock(), FakeLLMClient(ANALYSIS_GOOD)
    )
    callback = _callback()
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    expected = texts.action_outdated(order)

    await retry_analysis(
        callback, _retry_data(order_id), session, user, AsyncMock(), FakeLLMClient(ANALYSIS_GOOD)
    )

    assert callback.answer.await_args.args[0] == expected
    assert await _order_status(session, order_id) is S.WAITING_FOR_MISSING_DATA


async def test_retry_of_foreign_order_does_nothing(session: AsyncSession) -> None:
    owner = await _user(session, tg_id=1)
    stranger = await _user(session, tg_id=2)
    order_id = await _failed_analysis(session, owner)
    callback = _callback(tg_id=2)

    await retry_analysis(
        callback, _retry_data(order_id), session, stranger, AsyncMock(), FakeLLMClient()
    )

    callback.answer.assert_awaited_once_with(texts.ORDER_NOT_FOUND, show_alert=True)
    assert await _order_status(session, order_id) is S.FAILED


async def test_retry_without_llm_is_unavailable(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _failed_analysis(session, user)
    callback = _callback()

    await retry_analysis(callback, _retry_data(order_id), session, user, AsyncMock(), None)

    callback.answer.assert_awaited_once_with(texts.ANALYSIS_UNAVAILABLE, show_alert=True)
    assert await _order_status(session, order_id) is S.FAILED


async def test_reminder_during_analysis_has_cancel(session: AsyncSession) -> None:
    user = await _user(session)
    order_id = await _ready_order(session, user)
    await materials.finish_materials(session, order_id)
    await session.commit()
    message = AsyncMock()

    await new_report(message, session, user)

    assert message.answer.await_args.args[0].startswith(f"Заказ #{order_id}: идёт анализ")
    assert _actions(message.answer.await_args.kwargs["reply_markup"]) == [OrderAction.CANCEL]


@pytest.mark.parametrize("at_limit", [False, True])
async def test_reminder_after_failed_analysis(session: AsyncSession, at_limit: bool) -> None:
    user = await _user(session)
    order_id = await _failed_analysis(session, user)
    if at_limit:
        session.add_all(
            LLMCall(
                order_id=order_id,
                step=LLM_STEP,
                provider="f",
                model="m",
                status=LLMCallStatus.ERROR,
                error="x",
            )
            for _ in range(MAX_ANALYSIS_LLM_CALLS - 1)
        )
        await session.commit()
    message = AsyncMock()

    await new_report(message, session, user)

    actions = _actions(message.answer.await_args.kwargs["reply_markup"])
    if at_limit:
        assert actions == [OrderAction.CANCEL]
        assert texts.ANALYSIS_LIMIT_REACHED in message.answer.await_args.args[0]
    else:
        assert actions == [OrderAction.RETRY_ANALYSIS, OrderAction.CANCEL]


@pytest.mark.parametrize("failed", [False, True], ids=["analyzing", "failed"])
async def test_cancel_during_or_after_analysis(session: AsyncSession, failed: bool) -> None:
    user = await _user(session)
    if failed:
        order_id = await _failed_analysis(session, user)
    else:
        order_id = await _ready_order(session, user)
        await materials.finish_materials(session, order_id)
        await session.commit()

    await cancel_order(
        _callback(),
        OrderCallback(action=OrderAction.CANCEL, order_id=order_id),
        session,
        user,
        AsyncMock(),
    )

    assert await _order_status(session, order_id) is S.CANCELLED


async def test_background_analysis_notification_keeps_keyboard() -> None:
    bot = AsyncMock()
    failed = AnalysisOutcome(AnalysisOutcomeKind.FAILED, 5, can_retry=False)
    busy = AnalysisOutcome(AnalysisOutcomeKind.BUSY, 5)

    await notify_analysis_outcome(bot, 42, failed)
    await notify_analysis_outcome(bot, 42, busy)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args == (42, texts.analysis_failed(5, can_retry=False))
    assert _actions(bot.send_message.await_args.kwargs["reply_markup"]) == [OrderAction.CANCEL]


def test_truncation_is_reported_to_user() -> None:
    outcome = AnalysisOutcome(
        AnalysisOutcomeKind.COMPLETED,
        5,
        status=S.WAITING_FOR_MISSING_DATA,
        found=("topic",),
        missing=("university",),
        truncated_material=3,
        omitted_materials=(4, 5),
    )

    text = texts.analysis_completed(outcome)

    assert "учтены первые 400 000 символов (файл №3 — частично; не вошли: №4, №5)" in text


def test_dispatcher_registers_analysis_recovery_with_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
    file_storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def detach() -> None:
        for router in (start.router, menu.router, order_intake.router, requirements_dialog.router):
            monkeypatch.setattr(router, "_parent_router", None)

    detach()
    without = create_dispatcher(sessionmaker, MemoryStorage(), file_storage)
    detach()
    llm = FakeLLMClient()
    with_llm = create_dispatcher(sessionmaker, MemoryStorage(), file_storage, None, llm)

    assert with_llm["llm_client"] is llm
    assert len(with_llm.startup.handlers) == len(without.startup.handlers) + 1
    assert len(with_llm.shutdown.handlers) == len(without.shutdown.handlers) + 1
