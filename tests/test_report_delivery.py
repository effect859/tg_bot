"""Доставка отчёта (сервис): COMPLETED → файл из хранилища → отправитель, повторы, восстановление.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_report_delivery_concurrency.py. Заказы доходят до
COMPLETED настоящим путём 3.3–3.4 (generate_next_section + complete_report);
хранилище — LocalFileStorage во временном каталоге; отправитель — подмена
ReportSender, реальный Telegram не вызывается.
"""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Order, OrderReportDelivery, OrderReportFile
from app.domain.enums import OrderStatus
from app.rendering.docx import PracticeReportDocxRenderer
from app.services import orders
from app.services import report_delivery as service
from app.services.report_completion import CompletionOutcomeKind, complete_report
from app.services.report_delivery import (
    DeliveryError,
    DeliveryOutcome,
    DeliveryOutcomeKind,
    ReportDeliveryRecovery,
    ReportFile,
    ReportUnavailableError,
    deliver_report,
    find_orders_for_delivery,
    load_report_file,
    report_filename,
    run_delivery_pass,
)
from app.services.storage.local import LocalFileStorage
from plan_samples import PERSONAL_VALUES
from report_samples import generating_order
from test_docx_renderer import extract_lines
from test_report_completion import _fresh, _generated_order
from test_report_rendering_integration import _expected_from_db

S = OrderStatus
K = DeliveryOutcomeKind
TG_ID = 777_123_456


class RecordingSender:
    """ReportSender с учётом вызовов: ``errors`` по очереди на каждый вызов (None — успех)."""

    def __init__(
        self,
        *errors: BaseException | None,
        on_send: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.errors = list(errors)
        self.on_send = on_send
        self.calls: list[tuple[int, bytes, str]] = []

    async def __call__(self, chat_id: int, data: bytes, filename: str) -> None:
        self.calls.append((chat_id, data, filename))
        if self.on_send is not None:
            await self.on_send()
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error


class ReadingStorage(LocalFileStorage):
    """LocalFileStorage с подменой чтения: ``on_read`` перед чтением, ``error`` вместо него."""

    def __init__(
        self,
        root: Path,
        *,
        error: Exception | None = None,
        on_read: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(root)
        self.error = error
        self.on_read = on_read
        self.reads = 0

    async def read(self, key: str) -> bytes:
        self.reads += 1
        if self.on_read is not None:
            await self.on_read()
        if self.error is not None:
            raise self.error
        return await super().read(key)


TEMPORARY = DeliveryError("telegram_error", temporary=True)
FORBIDDEN = DeliveryError("forbidden", temporary=False)


async def _completed_order(session: AsyncSession, storage: LocalFileStorage, tg_id: int) -> int:
    """Заказ в COMPLETED: все разделы сгенерированы, DOCX сохранён (зафиксировано)."""
    order_id = await _generated_order(session, tg_id=tg_id)
    outcome = await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)
    assert outcome.kind is CompletionOutcomeKind.COMPLETED
    return order_id


async def _file(session: AsyncSession, order_id: int) -> OrderReportFile:
    stmt = select(OrderReportFile).where(OrderReportFile.order_id == order_id)
    row = await session.scalar(stmt.execution_options(populate_existing=True))
    assert row is not None
    return row


async def _delivery(session: AsyncSession, order_id: int) -> OrderReportDelivery | None:
    stmt = (
        select(OrderReportDelivery)
        .join(OrderReportFile, OrderReportFile.id == OrderReportDelivery.report_file_id)
        .where(OrderReportFile.order_id == order_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(stmt)


async def _add_delivery(session: AsyncSession, order_id: int, **values: Any) -> None:
    row = await _file(session, order_id)
    session.add(OrderReportDelivery(report_file_id=row.id, **values))
    await session.commit()


async def _set_delivery(session: AsyncSession, order_id: int, **values: Any) -> None:
    row = await _file(session, order_id)
    await session.execute(
        update(OrderReportDelivery)
        .where(OrderReportDelivery.report_file_id == row.id)
        .values(**values)
    )
    await session.commit()


def _stale_time() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=11)


async def _stored(session: AsyncSession, storage: LocalFileStorage, order_id: int) -> bytes:
    return await storage.read((await _file(session, order_id)).storage_key)


async def _state(session: AsyncSession, order_id: int) -> tuple[int, bool, bool, str | None]:
    """(attempts, идёт попытка, доставлено, error) строки доставки."""
    delivery = await _delivery(session, order_id)
    assert delivery is not None
    return (
        delivery.attempts,
        delivery.started_at is not None,
        delivery.delivered_at is not None,
        delivery.error,
    )


# --- Успех ----------------------------------------------------------------------------


async def test_delivers_completed_report(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.DELIVERED, order_id)
    stored = await _stored(session, storage, order_id)
    assert sender.calls == [(TG_ID, stored, f"Отчёт по практике №{order_id}.docx")]
    assert await _state(session, order_id) == (1, False, True, None)
    assert (await _fresh(session, order_id)).status is S.COMPLETED


async def test_generation_to_delivery_end_to_end(session: AsyncSession, tmp_path: Path) -> None:
    """generating → разделы → complete_report → deliver_report: у отправителя байты хранилища."""
    storage = LocalFileStorage(tmp_path)
    order_id = await _generated_order(session, tg_id=TG_ID)
    await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)
    sender = RecordingSender()

    assert (await deliver_report(session, storage, sender, order_id)).kind is K.DELIVERED

    [(_, sent, _)] = sender.calls
    row = await _file(session, order_id)
    assert sent == await storage.read(row.storage_key)
    assert (len(sent), hashlib.sha256(sent).hexdigest()) == (row.size_bytes, row.sha256)
    assert extract_lines(sent) == await _expected_from_db(session, order_id)


async def test_delivered_report_is_not_sent_again(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender()
    await deliver_report(session, storage, sender, order_id)

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.SKIPPED, order_id)
    assert len(sender.calls) == 1
    assert await _state(session, order_id) == (1, False, True, None)


# --- Не отправляется ------------------------------------------------------------------


async def test_order_not_completed_is_skipped(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    generating = await generating_order(session, tg_id=1)
    revision = await _completed_order(session, storage, 2)
    await orders.change_status(session, revision, S.REVISION_REQUESTED)
    await session.commit()
    sender = RecordingSender()

    for order_id in (generating, revision):
        outcome = await deliver_report(session, storage, sender, order_id)
        assert outcome == DeliveryOutcome(K.SKIPPED, order_id)
    assert sender.calls == []
    assert await _delivery(session, revision) is None


async def test_running_attempt_makes_second_start_busy(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    await _add_delivery(session, order_id, attempts=1, started_at=datetime.now(UTC))
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.BUSY, order_id)
    assert sender.calls == []
    assert await _state(session, order_id) == (1, True, False, None)


async def test_stale_attempt_is_counted_and_retried(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    await _add_delivery(session, order_id, attempts=1, started_at=_stale_time())
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome.kind is K.DELIVERED
    assert len(sender.calls) == 1
    assert await _state(session, order_id) == (2, False, True, None)
    assert f"interrupted: order_id={order_id} attempt=1" in caplog.text


async def test_stale_third_attempt_fails_without_sending(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = ReadingStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    await _add_delivery(session, order_id, attempts=3, started_at=_stale_time())
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.FAILED, order_id, error_code="attempts_exhausted")
    assert (sender.calls, storage.reads) == ([], 0)
    assert await _state(session, order_id) == (3, False, False, "attempts_exhausted")
    assert (await _fresh(session, order_id)).status is S.COMPLETED


async def test_missing_file_fails_without_sending(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    await storage.delete((await _file(session, order_id)).storage_key)
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.FAILED, order_id, error_code="file_missing")
    assert sender.calls == []
    assert await _state(session, order_id) == (1, False, False, "file_missing")
    assert (await _fresh(session, order_id)).status is S.COMPLETED


@pytest.mark.parametrize("same_size", [True, False])
async def test_corrupted_file_fails_without_sending(
    session: AsyncSession, tmp_path: Path, same_size: bool
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    row = await _file(session, order_id)
    data = await storage.read(row.storage_key)
    # Та же длина — расхождение находит только SHA-256.
    await storage.save(row.storage_key, b"x" * len(data) if same_size else data + b"x")
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.FAILED, order_id, error_code="file_corrupted")
    assert sender.calls == []
    assert await _state(session, order_id) == (1, False, False, "file_corrupted")


async def test_storage_read_error_is_retried_later(session: AsyncSession, tmp_path: Path) -> None:
    storage = ReadingStorage(tmp_path, error=OSError("disk failure"))
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender()

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.RETRY_LATER, order_id, error_code="storage_error")
    assert sender.calls == []
    assert await _state(session, order_id) == (1, False, False, None)


# --- Ошибки отправителя ---------------------------------------------------------------


async def test_temporary_sender_error_then_retry_succeeds(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender(TEMPORARY, None)

    first = await deliver_report(session, storage, sender, order_id)
    assert first == DeliveryOutcome(K.RETRY_LATER, order_id, error_code="telegram_error")
    assert await _state(session, order_id) == (1, False, False, None)
    assert order_id in await find_orders_for_delivery(session, limit=10)

    second = await deliver_report(session, storage, sender, order_id)

    assert second == DeliveryOutcome(K.DELIVERED, order_id)
    assert len(sender.calls) == 2
    assert await _state(session, order_id) == (2, False, True, None)


async def test_temporary_errors_exhaust_attempts(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender(TEMPORARY, TEMPORARY, TEMPORARY, None)

    kinds = [(await deliver_report(session, storage, sender, order_id)).kind for _ in range(4)]

    assert kinds == [K.RETRY_LATER, K.RETRY_LATER, K.FAILED, K.SKIPPED]
    assert len(sender.calls) == 3
    assert await _state(session, order_id) == (3, False, False, "attempts_exhausted")
    assert (await _fresh(session, order_id)).status is S.COMPLETED


async def test_unexpected_sender_error_is_temporary(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender(RuntimeError("boom"))

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.RETRY_LATER, order_id, error_code="send_error")
    assert await _state(session, order_id) == (1, False, False, None)


async def test_forbidden_is_permanent(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    sender = RecordingSender(FORBIDDEN)

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.FAILED, order_id, error_code="forbidden")
    assert await _state(session, order_id) == (1, False, False, "forbidden")
    order = await _fresh(session, order_id)
    assert (order.status, order.error) == (S.COMPLETED, None)
    assert order_id not in await find_orders_for_delivery(session, limit=10)
    assert (await deliver_report(session, storage, sender, order_id)).kind is K.SKIPPED
    assert len(sender.calls) == 1
    # Файл по-прежнему можно скачать.
    report = await load_report_file(session, storage, order_id)
    assert report.data == await _stored(session, storage, order_id)


async def test_commit_failure_after_send_redelivers_once(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Документ принят, T3 не зафиксирован: повтор после 10 минут — at-least-once."""
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    real_lock = service.lock_order
    locks = 0

    async def broken_on_finish(session: AsyncSession, order_id: int) -> Order:
        nonlocal locks
        locks += 1
        if locks == 2:  # T1 — первый вызов, T3 — второй
            raise RuntimeError("connection lost")
        return await real_lock(session, order_id)

    sender = RecordingSender()
    monkeypatch.setattr(service, "lock_order", broken_on_finish)
    with pytest.raises(RuntimeError):
        await deliver_report(session, storage, sender, order_id)
    await session.rollback()
    monkeypatch.setattr(service, "lock_order", real_lock)

    assert len(sender.calls) == 1
    assert await _state(session, order_id) == (1, True, False, None)
    # Попытка ещё не зависла: повтора нет.
    assert (await deliver_report(session, storage, sender, order_id)).kind is K.BUSY
    assert order_id not in await find_orders_for_delivery(session, limit=10)

    await _set_delivery(session, order_id, started_at=_stale_time())
    assert order_id in await find_orders_for_delivery(session, limit=10)
    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome.kind is K.DELIVERED
    assert len(sender.calls) == 2  # тот же файл второй раз
    assert sender.calls[0] == sender.calls[1]
    assert await _state(session, order_id) == (2, False, True, None)


async def test_result_of_taken_over_attempt_is_discarded(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)

    async def taken_over() -> None:
        await _set_delivery(
            session,
            order_id,
            attempts=OrderReportDelivery.attempts + 1,
            started_at=datetime.now(UTC),
        )

    sender = RecordingSender(on_send=taken_over)

    outcome = await deliver_report(session, storage, sender, order_id)

    assert outcome == DeliveryOutcome(K.DISCARDED, order_id)
    # Попытка принадлежит перехватившему: ни delivered_at, ни сброса started_at.
    assert await _state(session, order_id) == (2, True, False, None)


async def test_no_transaction_during_read_and_send(session: AsyncSession, tmp_path: Path) -> None:
    seen: dict[str, bool] = {}

    async def on_read() -> None:
        seen["read"] = session.in_transaction()

    async def on_send() -> None:
        seen["send"] = session.in_transaction()

    storage = ReadingStorage(tmp_path, on_read=on_read)
    order_id = await _completed_order(session, storage, TG_ID)

    outcome = await deliver_report(session, storage, RecordingSender(on_send=on_send), order_id)

    assert outcome.kind is K.DELIVERED
    assert seen == {"read": False, "send": False}


# --- Восстановление -------------------------------------------------------------------


async def test_find_orders_for_delivery(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    ready = await _completed_order(session, storage, 1)
    retry = await _completed_order(session, storage, 2)
    running = await _completed_order(session, storage, 3)
    stale = await _completed_order(session, storage, 4)
    delivered = await _completed_order(session, storage, 5)
    failed = await _completed_order(session, storage, 6)
    revision = await _completed_order(session, storage, 7)
    generating = await generating_order(session, tg_id=8)
    await _add_delivery(session, retry, attempts=1)
    await _add_delivery(session, running, attempts=1, started_at=datetime.now(UTC))
    await _add_delivery(session, stale, attempts=1, started_at=_stale_time())
    await _add_delivery(session, delivered, attempts=1, delivered_at=datetime.now(UTC))
    await _add_delivery(session, failed, attempts=1, error="forbidden")
    await orders.change_status(session, revision, S.REVISION_REQUESTED)
    await session.commit()

    found = await find_orders_for_delivery(session, limit=10)

    assert set(found) == {ready, retry, stale}
    assert not {running, delivered, failed, revision, generating} & set(found)
    assert len(await find_orders_for_delivery(session, limit=1)) == 1
    assert not session.in_transaction()


async def test_pass_delivers_and_continues_after_order_exception(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = LocalFileStorage(tmp_path)
    broken = await _completed_order(session, storage, 21)
    ok = await _completed_order(session, storage, 22)
    real_lock = service.lock_order

    async def lock(session: AsyncSession, order_id: int) -> Order:
        if order_id == broken:
            raise RuntimeError("secret details")
        return await real_lock(session, order_id)

    monkeypatch.setattr(service, "lock_order", lock)
    sender = RecordingSender()

    outcomes = await run_delivery_pass(sessionmaker, storage, sender)

    assert [(o.order_id, o.kind) for o in outcomes] == [(ok, K.DELIVERED)]
    assert len(sender.calls) == 1
    assert f"order_id={broken} error=RuntimeError" in caplog.text
    assert "secret details" not in caplog.text


async def test_pass_respects_batch(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    for tg_id in (31, 32, 33):
        await _completed_order(session, storage, tg_id)
    sender = RecordingSender()

    first = await run_delivery_pass(sessionmaker, storage, sender, batch=2)
    second = await run_delivery_pass(sessionmaker, storage, sender, batch=2)
    third = await run_delivery_pass(sessionmaker, storage, sender, batch=2)

    assert [len(first), len(second), len(third)] == [2, 1, 0]
    assert len(sender.calls) == 3


async def test_recovery_runs_first_pass_at_start(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    storage = LocalFileStorage(tmp_path)
    order_id = await _completed_order(session, storage, TG_ID)
    passes: list[list[Any]] = []
    sender = RecordingSender()

    class Recording(ReportDeliveryRecovery):
        async def run_pass(self) -> list[Any]:
            outcomes = await super().run_pass()
            passes.append(outcomes)
            return outcomes

    recovery = Recording(sessionmaker, storage, sender, interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(250):
            if passes:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert [(o.order_id, o.kind) for o in passes[0]] == [(order_id, K.DELIVERED)]
    assert len(sender.calls) == 1
    assert not recovery.running


async def test_errors_and_logs_have_no_report_text_or_personal_data(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    storage = LocalFileStorage(tmp_path)
    forbidden = await _completed_order(session, storage, TG_ID)
    unexpected = await _completed_order(session, storage, TG_ID + 1)
    missing = await _completed_order(session, storage, TG_ID + 2)
    await storage.delete((await _file(session, missing)).storage_key)

    await deliver_report(session, storage, RecordingSender(FORBIDDEN), forbidden)
    await deliver_report(
        session, storage, RecordingSender(RuntimeError("аааааааааа secret")), unexpected
    )
    await deliver_report(session, storage, RecordingSender(), missing)

    errors = [(await _state(session, o))[3] for o in (forbidden, unexpected, missing)]
    assert errors == ["forbidden", None, "file_missing"]
    text = caplog.text
    assert "аааааааааа" not in text  # текст разделов в report_samples — из букв «а»
    assert "secret" not in text
    assert str(TG_ID) not in text
    for value in PERSONAL_VALUES:
        assert value not in text


# --- Скачивание -----------------------------------------------------------------------


async def test_load_report_file(session: AsyncSession, tmp_path: Path) -> None:
    seen: list[bool] = []

    async def on_read() -> None:
        seen.append(session.in_transaction())

    storage = ReadingStorage(tmp_path, on_read=on_read)
    order_id = await _completed_order(session, storage, TG_ID)
    seen.clear()

    report = await load_report_file(session, storage, order_id)

    assert seen == [False]
    assert report == ReportFile(
        await _stored(session, storage, order_id), report_filename(order_id)
    )
    # Скачивание не создаёт и не меняет строку доставки.
    assert await _delivery(session, order_id) is None


async def test_load_report_file_errors(session: AsyncSession, tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    missing = await _completed_order(session, storage, 1)
    corrupted = await _completed_order(session, storage, 2)
    without_file = await generating_order(session, tg_id=3)
    await storage.delete((await _file(session, missing)).storage_key)
    await storage.save((await _file(session, corrupted)).storage_key, b"not a report")

    for order_id, code in (
        (missing, "file_missing"),
        (corrupted, "file_corrupted"),
        (without_file, "no_report_file"),
    ):
        with pytest.raises(ReportUnavailableError) as error:
            await load_report_file(session, storage, order_id)
        assert error.value.code == code


def test_delivery_constants() -> None:
    assert service.MAX_DELIVERY_ATTEMPTS == 3
    assert timedelta(minutes=10) == service.STEP_STALE_AFTER
    assert service.RECOVERY_INTERVAL_SECONDS == 60.0
    assert service.RECOVERY_BATCH == 5
    assert report_filename(42) == "Отчёт по практике №42.docx"
