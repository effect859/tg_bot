"""Восстановление обработки материалов: проход по всем заказам и его жизненный цикл.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_material_concurrency.py. Извлечение — подмена, кроме
теста остановки: там настоящий дочерний процесс. Telegram не вызывается: уведомления
перехватываются, Bot — мок.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.setup import notify_material_outcome
from app.db.models import OrderMaterial
from app.domain.enums import MaterialFailureReason, MaterialProcessingStatus, OrderStatus
from app.domain.materials import MAX_PROCESSING_ATTEMPTS
from app.extraction.base import ExtractionResult
from app.extraction.runner import SubprocessTextExtractor
from app.services import material_recovery, orders
from app.services import order_materials as materials
from app.services.material_processing import (
    OutcomeKind,
    ProcessingOutcome,
    get_material_text,
    process_material,
)
from app.services.material_recovery import MaterialRecovery, run_recovery_pass
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from fake_extractor import FakeExtractor
from material_samples import TXT_BYTES
from test_extraction_runner import _process_exists

S = OrderStatus
M = MaterialProcessingStatus
R = MaterialFailureReason


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "storage")


class Notifications:
    """Подмена отправки сообщений: запоминает (chat_id, material_id, kind)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[int, int, OutcomeKind]] = []
        self.error = error

    async def __call__(self, chat_id: int, outcome: ProcessingOutcome) -> None:
        self.sent.append((chat_id, outcome.material_id, outcome.kind))
        if self.error is not None:
            raise self.error


async def _order(session: AsyncSession, tg_id: int = 1) -> int:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
        order = await orders.change_status(session, order.id, status)
    await session.commit()
    return order.id


async def _material(
    session: AsyncSession,
    storage: LocalFileStorage,
    order_id: int,
    *,
    age: timedelta = timedelta(minutes=10),
) -> int:
    """Материал в RECEIVED, созданный ``age`` назад (по часам БД)."""
    material = await materials.store_material(
        session, storage, order_id, filename="task.txt", mime_type=None, data=TXT_BYTES
    )
    material_id = material.id
    await _set(session, material_id, created_at=func.now() - age, updated_at=func.now() - age)
    return material_id


async def _set(session: AsyncSession, material_id: int, **values: object) -> None:
    await session.execute(
        update(OrderMaterial).where(OrderMaterial.id == material_id).values(**values)
    )
    await session.commit()


async def _processing(session: AsyncSession, material_id: int, *, minutes_ago: int) -> None:
    await materials.mark_processing(session, material_id)
    await _set(
        session,
        material_id,
        processing_started_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


async def _failed(
    session: AsyncSession, material_id: int, reason: MaterialFailureReason, attempts: int = 1
) -> None:
    await materials.mark_processing(session, material_id)
    await materials.mark_failed(session, material_id, "test failure", reason=reason)
    await _set(session, material_id, processing_attempts=attempts)


async def _fresh(session: AsyncSession, material_id: int) -> OrderMaterial:
    material = await session.get(OrderMaterial, material_id, populate_existing=True)
    assert material is not None
    return material


async def _pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    extractor: FakeExtractor,
    notify: Notifications | None = None,
    **kwargs: object,
) -> list[ProcessingOutcome]:
    return await run_recovery_pass(
        sessionmaker,
        storage,
        extractor,
        notify or Notifications(),
        **kwargs,  # type: ignore[arg-type]
    )


# --- Что берёт проход ----------------------------------------------------------------------


async def test_old_received_material_is_processed_and_reported(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session, tg_id=111)
    material_id = await _material(session, storage, order_id)
    notify = Notifications()

    outcomes = await _pass(sessionmaker, storage, FakeExtractor(), notify)

    assert [o.kind for o in outcomes] == [OutcomeKind.PROCESSED]
    material = await _fresh(session, material_id)
    assert (material.processing_status, material.processing_attempts) == (M.PROCESSED, 1)
    assert await get_material_text(session, material_id) is not None
    assert notify.sent == [(111, material_id, OutcomeKind.PROCESSED)]


async def test_fresh_received_material_is_left_to_its_update(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id, age=timedelta(seconds=30))
    extractor = FakeExtractor()

    assert await _pass(sessionmaker, storage, extractor) == []
    assert extractor.calls == []
    assert (await _fresh(session, material_id)).processing_status is M.RECEIVED


async def test_stale_processing_is_recovered(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    await _processing(session, material_id, minutes_ago=10)

    outcomes = await _pass(sessionmaker, storage, FakeExtractor())

    assert [o.kind for o in outcomes] == [OutcomeKind.PROCESSED]
    material = await _fresh(session, material_id)
    assert (material.processing_status, material.processing_attempts) == (M.PROCESSED, 2)


async def test_fresh_processing_is_not_touched(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    await _processing(session, material_id, minutes_ago=1)
    extractor = FakeExtractor()

    assert await _pass(sessionmaker, storage, extractor) == []
    assert extractor.calls == []
    material = await _fresh(session, material_id)
    assert (material.processing_status, material.processing_attempts) == (M.PROCESSING, 1)


@pytest.mark.parametrize(
    "reason", [R.STORAGE_ERROR, R.EXTRACTOR_UNAVAILABLE, R.INTERRUPTED, R.DATABASE_ERROR]
)
async def test_retryable_failure_is_retried(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    reason: MaterialFailureReason,
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    await _failed(session, material_id, reason)

    outcomes = await _pass(sessionmaker, storage, FakeExtractor())

    assert [o.kind for o in outcomes] == [OutcomeKind.PROCESSED]
    material = await _fresh(session, material_id)
    assert (material.processing_status, material.processing_attempts) == (M.PROCESSED, 2)


@pytest.mark.parametrize("reason", [R.CORRUPTED, R.TIMEOUT, R.EXTRACTOR_CRASH, R.RESULT_REJECTED])
async def test_terminal_failure_is_not_retried(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    reason: MaterialFailureReason,
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    await _failed(session, material_id, reason)
    extractor = FakeExtractor()

    assert await _pass(sessionmaker, storage, extractor) == []
    assert extractor.calls == []
    assert (await _fresh(session, material_id)).failure_reason is reason


async def test_exhausted_attempts_are_not_retried(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    await _failed(session, material_id, R.STORAGE_ERROR, attempts=MAX_PROCESSING_ATTEMPTS)
    extractor = FakeExtractor()

    assert await _pass(sessionmaker, storage, extractor) == []
    assert extractor.calls == []


async def test_last_retry_failure_is_reported_as_final(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    """Временный сбой с оставшимися попытками не сообщается, исчерпание — сообщается."""
    order_id = await _order(session, tg_id=5)
    retrying = await _material(session, storage, order_id, age=timedelta(minutes=20))
    last = await _material(session, storage, order_id, age=timedelta(minutes=10))
    await _failed(session, retrying, R.STORAGE_ERROR, attempts=1)
    await _failed(session, last, R.STORAGE_ERROR, attempts=MAX_PROCESSING_ATTEMPTS - 1)
    notify = Notifications()
    storage_down = FakeExtractor()

    async def broken_read(key: str) -> bytes:
        raise OSError("disk unavailable")

    storage.read = broken_read  # type: ignore[method-assign]
    outcomes = await _pass(sessionmaker, storage, storage_down, notify)

    assert [(o.kind, o.failure_reason, o.will_retry) for o in outcomes] == [
        (OutcomeKind.FAILED, R.STORAGE_ERROR, True),
        (OutcomeKind.FAILED, R.STORAGE_ERROR, False),
    ]
    assert notify.sent == [(5, last, OutcomeKind.FAILED)]


# --- Порядок, партии, несколько заказов ------------------------------------------------------


async def test_batch_takes_oldest_first_and_rest_waits_for_next_pass(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    newest = await _material(session, storage, order_id, age=timedelta(minutes=5))
    oldest = await _material(session, storage, order_id, age=timedelta(minutes=30))
    middle = await _material(session, storage, order_id, age=timedelta(minutes=15))

    first = await _pass(sessionmaker, storage, FakeExtractor(), batch=2)
    second = await _pass(sessionmaker, storage, FakeExtractor(), batch=2)

    assert [o.material_id for o in first] == [oldest, middle]
    assert [o.material_id for o in second] == [newest]


async def test_materials_of_several_orders(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    first_order = await _order(session, tg_id=1)
    second_order = await _order(session, tg_id=2)
    cancelled_order = await _order(session, tg_id=3)
    a = await _material(session, storage, first_order, age=timedelta(minutes=20))
    b = await _material(session, storage, second_order, age=timedelta(minutes=10))
    c = await _material(session, storage, cancelled_order, age=timedelta(minutes=30))
    await orders.change_status(session, cancelled_order, S.CANCELLED)
    await session.commit()
    notify = Notifications()

    outcomes = await _pass(sessionmaker, storage, FakeExtractor(), notify)

    assert [(o.material_id, o.kind) for o in outcomes] == [
        (a, OutcomeKind.PROCESSED),
        (b, OutcomeKind.PROCESSED),
    ]
    assert notify.sent == [(1, a, OutcomeKind.PROCESSED), (2, b, OutcomeKind.PROCESSED)]
    # Заказ отменён: материал не берётся.
    assert (await _fresh(session, c)).processing_status is M.RECEIVED


async def test_busy_queue_ends_the_pass_without_using_attempts(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    first = await _material(session, storage, order_id, age=timedelta(minutes=20))
    second = await _material(session, storage, order_id, age=timedelta(minutes=10))
    extractor = FakeExtractor()
    extractor.busy = True
    notify = Notifications()

    outcomes = await _pass(sessionmaker, storage, extractor, notify)

    assert [(o.material_id, o.kind) for o in outcomes] == [(first, OutcomeKind.BUSY)]
    for material_id in (first, second):
        material = await _fresh(session, material_id)
        assert (material.processing_status, material.processing_attempts) == (M.RECEIVED, 0)
    assert notify.sent == []


async def test_second_pass_does_nothing(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    order_id = await _order(session)
    await _material(session, storage, order_id)
    extractor = FakeExtractor()
    notify = Notifications()

    first = await _pass(sessionmaker, storage, extractor, notify)
    second = await _pass(sessionmaker, storage, extractor, notify)

    assert len(first) == 1 and second == []
    assert len(extractor.calls) == 1
    assert len(notify.sent) == 1


async def test_stale_attempt_result_is_discarded_after_recovery(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    """Попытку 1 признали прерванной и восстановили; её поздний результат отброшен."""
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    old = FakeExtractor(ExtractionResult(text="старый", extractor="old"))
    recovered: list[ProcessingOutcome] = []

    async def recovery_takes_over() -> None:
        await _set(
            session,
            material_id,
            processing_started_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        new = FakeExtractor(ExtractionResult(text="новый", extractor="new"))
        recovered.extend(await _pass(sessionmaker, storage, new))

    old.on_extract = recovery_takes_over
    outcome = await process_material(session, storage, old, material_id)

    assert [o.kind for o in recovered] == [OutcomeKind.PROCESSED]
    assert outcome.kind is OutcomeKind.DISCARDED
    stored = await get_material_text(session, material_id)
    assert stored is not None and stored.text == "новый"
    assert (await _fresh(session, material_id)).processing_attempts == 2


# --- Уведомления -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        TelegramForbiddenError(
            method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked"
        ),
        TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout"),
    ],
    ids=["forbidden", "network"],
)
async def test_notification_failure_does_not_affect_processing(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    error: Exception,
) -> None:
    order_id = await _order(session, tg_id=42)
    first = await _material(session, storage, order_id, age=timedelta(minutes=20))
    second = await _material(session, storage, order_id, age=timedelta(minutes=10))
    bot = AsyncMock()
    bot.send_message.side_effect = error
    extractor = FakeExtractor()

    outcomes = await _pass(sessionmaker, storage, extractor, partial(notify_material_outcome, bot))

    assert [o.kind for o in outcomes] == [OutcomeKind.PROCESSED, OutcomeKind.PROCESSED]
    for material_id in (first, second):
        assert (await _fresh(session, material_id)).processing_status is M.PROCESSED
    # Одна попытка отправки на материал, без повторов; обработка не повторяется.
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].args == (
        42,
        texts.material_processing_result(outcomes[0]),
    )
    assert len(extractor.calls) == 2
    assert await _pass(sessionmaker, storage, extractor) == []


# --- Жизненный цикл ---------------------------------------------------------------------------


async def _wait_for(condition, limit_seconds: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + limit_seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def test_first_pass_runs_at_start_after_restart(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    """Состояние, оставленное прошлым процессом, разбирает первый проход нового."""
    order_id = await _order(session)
    received = await _material(session, storage, order_id, age=timedelta(minutes=40))
    failed = await _material(session, storage, order_id, age=timedelta(minutes=30))
    stale = await _material(session, storage, order_id, age=timedelta(minutes=20))
    fresh = await _material(session, storage, order_id, age=timedelta(minutes=10))
    await _failed(session, failed, R.STORAGE_ERROR)
    await _processing(session, stale, minutes_ago=10)
    await _processing(session, fresh, minutes_ago=1)
    extractor = FakeExtractor()
    notify = Notifications()
    recovery = MaterialRecovery(sessionmaker, storage, extractor, notify, interval_seconds=3600)

    recovery.start()
    try:
        await _wait_for(lambda: len(notify.sent) == 3)
    finally:
        await recovery.stop()

    assert {material_id for _, material_id, _ in notify.sent} == {received, failed, stale}
    assert (await _fresh(session, fresh)).processing_status is M.PROCESSING
    assert not recovery.running


async def test_stop_cancels_running_extraction_and_kills_child(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    tmp_path: Path,
) -> None:
    order_id = await _order(session)
    material_id = await _material(session, storage, order_id)
    pid_file = tmp_path / "worker.pid"
    code = (
        f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    )
    extractor = SubprocessTextExtractor(worker_command=[sys.executable, "-I", "-c", code])
    recovery = MaterialRecovery(
        sessionmaker, storage, extractor, Notifications(), interval_seconds=3600
    )

    recovery.start()
    await _wait_for(lambda: pid_file.exists() and pid_file.read_text() != "")
    pid = int(pid_file.read_text())
    await recovery.stop()

    assert not recovery.running
    assert not _process_exists(pid)
    # Попытка израсходована, материал будет признан прерванным и восстановлен.
    material = await _fresh(session, material_id)
    assert (material.processing_status, material.processing_attempts) == (M.PROCESSING, 1)


async def test_failing_pass_does_not_stop_the_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def failing_pass(*args: object, **kwargs: object) -> list[ProcessingOutcome]:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(material_recovery, "run_recovery_pass", failing_pass)
    recovery = MaterialRecovery(
        sessionmaker, storage, FakeExtractor(), Notifications(), interval_seconds=0.01
    )

    recovery.start()
    try:
        await _wait_for(lambda: calls >= 3)
        assert recovery.running
    finally:
        await recovery.stop()
    assert not recovery.running


async def test_stop_without_start_and_double_start(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def idle(*args: object, **kwargs: object) -> list[ProcessingOutcome]:
        return []

    monkeypatch.setattr(material_recovery, "run_recovery_pass", idle)
    recovery = MaterialRecovery(sessionmaker, storage, FakeExtractor(), Notifications())
    await recovery.stop()

    recovery.start()
    try:
        with pytest.raises(RuntimeError):
            recovery.start()
    finally:
        await recovery.stop()
    assert not recovery.running
