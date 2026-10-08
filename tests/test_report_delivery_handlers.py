"""Бот-слой доставки: «Мои заказы» с кнопками, «Скачать отчёт», send_report_document,
регистрация прохода доставки.

Message, CallbackQuery и Bot — моки (AsyncMock): реальный Telegram Bot API не
вызывается. БД — настоящая PostgreSQL, хранилище — LocalFileStorage во временном
каталоге; заказы доходят до COMPLETED настоящим путём 3.3–3.4.
"""

from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendDocument
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Update
from aiogram.types import User as TgUser
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import setup, texts
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.handlers.menu import download_report, my_orders
from app.bot.keyboards import ReportCallback, orders_keyboard
from app.bot.setup import create_dispatcher, send_report_document
from app.db.models import Order, User
from app.domain.enums import DocType, OrderStatus
from app.services.report_delivery import DeliveryError, report_filename
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from report_samples import generating_order
from test_plan_handlers import RecordingSession
from test_report_delivery import _completed_order, _delivery, _file

S = OrderStatus
SECRET = "Секретов Секрет Секретович"


async def _user(session: AsyncSession, tg_id: int) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


def _callback(tg_id: int) -> AsyncMock:
    callback = AsyncMock()
    callback.message = AsyncMock()
    callback.message.chat.id = tg_id
    callback.from_user = TgUser(id=tg_id, is_bot=False, first_name="U")
    return callback


def _alert(callback: AsyncMock) -> str:
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.kwargs == {"show_alert": True}
    return str(callback.answer.await_args.args[0])


def _report_buttons(markup: Any) -> list[tuple[str, int]]:
    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = []
    for row in markup.inline_keyboard:
        for button in row:
            assert button.callback_data is not None
            buttons.append((button.text, ReportCallback.unpack(button.callback_data).order_id))
    return buttons


async def _download(
    session: AsyncSession, storage: LocalFileStorage, user: User, order_id: int
) -> tuple[AsyncMock, AsyncMock]:
    callback = _callback(user.tg_id)
    bot = AsyncMock()
    await download_report(callback, ReportCallback(order_id=order_id), session, user, bot, storage)
    return callback, bot


def _sent_document(bot: AsyncMock) -> tuple[int, BufferedInputFile, str]:
    bot.send_document.assert_awaited_once()
    chat_id, document = bot.send_document.await_args.args
    assert isinstance(document, BufferedInputFile)
    return chat_id, document, bot.send_document.await_args.kwargs["caption"]


# --- «Мои заказы» ---------------------------------------------------------------------


async def test_my_orders_has_download_buttons_only_for_completed(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    completed = await _completed_order(session, storage, 1)
    user = await _user(session, 1)
    # Готовый заказ неактивен: можно оформить следующий.
    active = Order(user_id=user.id, doc_type=DocType.PRACTICE_REPORT)
    session.add(active)
    await session.commit()
    message = AsyncMock()

    await my_orders(message, session, user)

    text = message.answer.await_args.args[0]
    assert f"#{completed}" in text and f"#{active.id}" in text
    assert _report_buttons(message.answer.await_args.kwargs["reply_markup"]) == [
        (f"📄 Скачать отчёт #{completed}", completed)
    ]


async def test_my_orders_without_completed_has_no_keyboard(session: AsyncSession) -> None:
    await generating_order(session, tg_id=1)
    user = await _user(session, 1)
    message = AsyncMock()

    await my_orders(message, session, user)

    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs == {}


def test_orders_keyboard_lists_each_completed_order() -> None:
    orders = [
        Order(id=1, status=S.COMPLETED),
        Order(id=2, status=S.GENERATING),
        Order(id=3, status=S.COMPLETED),
        Order(id=4, status=S.FAILED),
    ]

    assert [order_id for _, order_id in _report_buttons(orders_keyboard(orders))] == [1, 3]
    assert orders_keyboard([Order(id=5, status=S.PAYMENT_PENDING)]) is None
    assert orders_keyboard([]) is None


# --- «Скачать отчёт» ------------------------------------------------------------------


async def test_download_sends_verified_report(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    user = await _user(session, 1)

    callback, bot = await _download(session, storage, user, order_id)

    # Файл читался и отправлялся без открытой транзакции.
    assert not session.in_transaction()
    callback.answer.assert_awaited_once_with()
    chat_id, document, caption = _sent_document(bot)
    assert chat_id == 1
    assert document.filename == report_filename(order_id)
    assert document.data == await storage.read((await _file(session, order_id)).storage_key)
    assert caption == texts.report_ready(order_id)


async def test_download_repeats_and_keeps_delivery_state(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    user = await _user(session, 1)

    for _ in range(3):
        _, bot = await _download(session, storage, user, order_id)
        _sent_document(bot)

    # Фоновую доставку скачивание не трогает: строки доставки нет.
    assert await _delivery(session, order_id) is None


async def test_download_foreign_or_missing_order(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)

    for target in (order_id, order_id + 10_000):
        # Как в боте: User загружается заново на каждый апдейт (rollback его истекает).
        stranger = await _user(session, 2)
        callback, bot = await _download(session, storage, stranger, target)
        assert _alert(callback) == texts.ORDER_NOT_FOUND
        bot.send_document.assert_not_awaited()


async def test_download_not_completed_order(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await generating_order(session, tg_id=1)
    user = await _user(session, 1)

    callback, bot = await _download(session, storage, user, order_id)

    assert _alert(callback) == texts.REPORT_NOT_READY
    bot.send_document.assert_not_awaited()


@pytest.mark.parametrize("damage", ["missing", "corrupted"])
async def test_download_unavailable_file(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture, damage: str
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    key = (await _file(session, order_id)).storage_key
    if damage == "missing":
        await storage.delete(key)
    else:
        await storage.save(key, b"tampered")
    user = await _user(session, 1)

    callback, bot = await _download(session, storage, user, order_id)

    assert _alert(callback) == texts.REPORT_UNAVAILABLE
    bot.send_document.assert_not_awaited()
    code = "file_missing" if damage == "missing" else "file_corrupted"
    assert f"order_id={order_id} code={code}" in caplog.text


async def test_download_database_error(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    user = await _user(session, 1)

    async def broken(*args: object) -> Order:
        raise OperationalError(f"SELECT '{SECRET}'", {}, Exception("connection lost"))

    monkeypatch.setattr(menu, "get_user_order", broken)

    callback, bot = await _download(session, storage, user, order_id)

    assert _alert(callback) == texts.REPORT_DOWNLOAD_FAILED
    bot.send_document.assert_not_awaited()
    assert "error=OperationalError" in caplog.text
    assert SECRET not in caplog.text


async def test_download_storage_error(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    user = await _user(session, 1)

    async def broken_read(key: str) -> bytes:
        raise PermissionError(f"denied: {key}")

    monkeypatch.setattr(storage, "read", broken_read)

    callback, bot = await _download(session, storage, user, order_id)

    assert _alert(callback) == texts.REPORT_DOWNLOAD_FAILED
    bot.send_document.assert_not_awaited()


async def test_download_telegram_error_is_logged(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    user = await _user(session, 1)
    callback = _callback(1)
    bot = AsyncMock()
    bot.send_document.side_effect = TelegramNetworkError(_method(), "timeout")

    await download_report(callback, ReportCallback(order_id=order_id), session, user, bot, storage)

    callback.answer.assert_awaited_once_with()
    assert f"order_id={order_id} error=TelegramNetworkError" in caplog.text


def _detach_routers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Роутеры — синглтоны модулей: подключены к диспетчеру из других тестов.
    for router in (
        start.router,
        menu.router,
        order_intake.router,
        plan.router,
        requirements_dialog.router,
    ):
        monkeypatch.setattr(router, "_parent_router", None)


async def test_download_callback_reaches_menu_handler(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Настоящий Dispatcher: кнопка доходит до menu.download_report с file_storage."""
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, 1)
    _detach_routers(monkeypatch)
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), storage)
    bot = Bot(token="42:TEST", session=RecordingSession())
    update = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="cb",
            from_user=TgUser(id=1, is_bot=False, first_name="U"),
            chat_instance="ci",
            data=ReportCallback(order_id=order_id).pack(),
        ),
    )

    await dispatcher.feed_update(bot, update)

    recorded = bot.session
    assert isinstance(recorded, RecordingSession)
    [answer] = [r for r in recorded.requests if isinstance(r, AnswerCallbackQuery)]
    assert (answer.text, answer.show_alert) == (None, None)
    [document] = [r for r in recorded.requests if isinstance(r, SendDocument)]
    assert document.chat_id == 1
    assert isinstance(document.document, BufferedInputFile)
    assert document.document.filename == report_filename(order_id)


# --- send_report_document -------------------------------------------------------------


def _method() -> SendDocument:
    return SendDocument(chat_id=1, document=BufferedInputFile(b"x", filename="f.docx"))


async def test_send_report_document() -> None:
    bot = AsyncMock()

    await send_report_document(bot, 42, b"docx bytes", "Отчёт по практике №7.docx")

    chat_id, document, caption = _sent_document(bot)
    assert (chat_id, document.data, document.filename) == (
        42,
        b"docx bytes",
        "Отчёт по практике №7.docx",
    )
    assert caption == texts.REPORT_DELIVERED


@pytest.mark.parametrize(
    ("error", "code", "temporary"),
    [
        (TelegramForbiddenError(_method(), f"blocked {SECRET}"), "forbidden", False),
        (TelegramBadRequest(_method(), f"chat not found {SECRET}"), "bad_request", False),
        (TelegramNotFound(_method(), "not found"), "bad_request", False),
        (TelegramEntityTooLarge(_method(), "too large"), "too_large", False),
        (TelegramRetryAfter(_method(), "flood", retry_after=5), "telegram_error", True),
        (TelegramNetworkError(_method(), "timeout"), "telegram_error", True),
        (TelegramServerError(_method(), "bad gateway"), "telegram_error", True),
    ],
)
async def test_send_report_document_maps_telegram_errors(
    error: Exception, code: str, temporary: bool
) -> None:
    bot = AsyncMock()
    bot.send_document.side_effect = error

    with pytest.raises(DeliveryError) as raised:
        await send_report_document(bot, 42, b"docx bytes", "report.docx")

    assert (raised.value.code, raised.value.temporary) == (code, temporary)
    # Только код: ни текста ответа Telegram, ни исходного исключения в цепочке.
    assert str(raised.value) == code
    assert SECRET not in str(raised.value)
    assert raised.value.__suppress_context__


async def test_send_report_document_passes_other_errors() -> None:
    bot = AsyncMock()
    bot.send_document.side_effect = RuntimeError("boom")

    # Сервис доставки считает такие сбои временными (send_error).
    with pytest.raises(RuntimeError):
        await send_report_document(bot, 42, b"docx bytes", "report.docx")


# --- Регистрация ----------------------------------------------------------------------


async def test_delivery_recovery_registered_without_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проход доставки регистрируется и без LLM-клиента и извлекателя; отправитель —
    send_report_document с ботом из startup."""
    _detach_routers(monkeypatch)
    created: list[tuple[object, object, Any]] = []
    started: list[str] = []

    class Fake:
        def __init__(self, sessionmaker: object, storage: object, *args: Any) -> None:
            created.append((sessionmaker, storage, args))

        def start(self) -> None:
            started.append(type(self).__name__)

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(setup, "ReportCompletionRecovery", Fake)
    monkeypatch.setattr(setup, "ReportDeliveryRecovery", Fake)
    # Сверка платежей (Stage 3.6) — тоже startup-проход; здесь не запускается.
    monkeypatch.setattr(setup, "PaymentReconciliation", Fake)
    # Возврат кредитов (Stage 3.7) — тоже startup-проход; здесь не запускается.
    monkeypatch.setattr(setup, "RefundRecovery", Fake)
    file_storage = LocalFileStorage(tmp_path)
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), file_storage)
    bot = AsyncMock()

    for handler in dispatcher.startup.handlers:
        await handler.callback(bot)
    for handler in dispatcher.shutdown.handlers:
        await handler.callback()

    assert len(created) == 4 == len(started)
    completion, delivery, *_ = created
    assert completion == (sessionmaker, file_storage, ())
    sessions, storage, (send,) = delivery
    assert (sessions, storage) == (sessionmaker, file_storage)
    assert isinstance(send, partial)
    assert (send.func, send.args) == (send_report_document, (bot,))
