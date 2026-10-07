"""Завершение отчёта (сервис): DOCX → хранилище → строка файла → COMPLETED, повторы, восстановление.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_report_completion_concurrency.py. Разделы генерируются
FakeLLMClient через generate_next_section; хранилище — LocalFileStorage во временном
каталоге.
"""

import asyncio
import hashlib
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import setup
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.setup import create_dispatcher
from app.db.models import Order, OrderPlanVersion, OrderReportFile
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import VersionNotFoundError
from app.domain.report_document import DocumentErrorCode, InvalidDocumentError
from app.domain.report_generation import ReportOutcomeKind
from app.generation.schemas import DocumentContent
from app.rendering.docx import DOCX_MIME_TYPE, PracticeReportDocxRenderer
from app.services import orders
from app.services import report_completion as service
from app.services.report_completion import (
    CompletionOutcome,
    CompletionOutcomeKind,
    ReportCompletionRecovery,
    complete_report,
    find_orders_for_completion,
    report_storage_key,
    run_completion_pass,
)
from app.services.report_generation import generate_next_section
from app.services.storage.local import LocalFileStorage
from fake_llm import FakeLLMClient
from plan_samples import PERSONAL_VALUES
from report_samples import ALL_GOOD, generating_order
from test_docx_renderer import extract_lines
from test_report_rendering_integration import _expected_from_db
from test_storage_local import tmp_files

S = OrderStatus
K = CompletionOutcomeKind


class RecordingRenderer:
    """Настоящий DOCX-рендерер с учётом вызовов, потока и подменой исхода."""

    file_extension = "docx"
    mime_type = DOCX_MIME_TYPE

    def __init__(
        self,
        error: Exception | None = None,
        on_render: Callable[[], None] | None = None,
    ) -> None:
        self.error = error
        self.on_render = on_render
        self.calls = 0
        self.threads: list[int] = []
        self._real = PracticeReportDocxRenderer()

    def render(self, content: DocumentContent) -> bytes:
        self.calls += 1
        self.threads.append(threading.get_ident())
        if self.on_render is not None:
            self.on_render()
        if self.error is not None:
            raise self.error
        return self._real.render(content)


class RecordingStorage(LocalFileStorage):
    """LocalFileStorage с учётом вызовов save: ``failures`` первых падают OSError."""

    def __init__(
        self,
        root: Path,
        *,
        failures: int = 0,
        on_save: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(root)
        self.failures = failures
        self.on_save = on_save
        self.saves = 0

    async def save(self, key: str, data: bytes) -> None:
        self.saves += 1
        if self.on_save is not None:
            await self.on_save()
        if self.failures:
            self.failures -= 1
            raise OSError("disk full")
        await super().save(key, data)


async def _generated_order(session: AsyncSession, *, tg_id: int = 1, sections: int = 4) -> int:
    """Заказ в GENERATING с ``sections`` первыми сгенерированными разделами (всего 4)."""
    order_id = await generating_order(session, tg_id=tg_id)
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in range(sections):
        outcome = await generate_next_section(session, llm, order_id)
        assert outcome.kind is ReportOutcomeKind.SAVED
    return order_id


async def _fresh(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _files(session: AsyncSession, order_id: int) -> list[OrderReportFile]:
    stmt = select(OrderReportFile).where(OrderReportFile.order_id == order_id)
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _plan_id(session: AsyncSession, order_id: int) -> int:
    plan_id = await session.scalar(
        select(OrderPlanVersion.id).where(
            OrderPlanVersion.order_id == order_id,
            OrderPlanVersion.status == VersionStatus.CONFIRMED,
        )
    )
    assert plan_id is not None
    return plan_id


async def _set(session: AsyncSession, order_id: int, **values: object) -> None:
    await session.execute(update(Order).where(Order.id == order_id).values(**values))
    await session.commit()


async def _stale(session: AsyncSession, order_id: int) -> None:
    await _set(session, order_id, step_started_at=datetime.now(UTC) - timedelta(minutes=11))


def _stored_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


async def _assert_file_matches_row(
    session: AsyncSession, storage: LocalFileStorage, order_id: int
) -> bytes:
    [row] = await _files(session, order_id)
    data = await storage.read(row.storage_key)
    assert (row.size_bytes, row.sha256) == (len(data), hashlib.sha256(data).hexdigest())
    return data


# --- Успех ----------------------------------------------------------------------------


async def test_completes_generated_report(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    storage = LocalFileStorage(tmp_path)

    outcome = await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)

    assert outcome == CompletionOutcome(K.COMPLETED, order_id)
    order = await _fresh(session, order_id)
    assert order.status is S.COMPLETED
    assert order.completed_at is not None
    assert (order.step_started_at, order.error) == (None, None)
    plan_id = await _plan_id(session, order_id)
    [row] = await _files(session, order_id)
    assert row.plan_version_id == plan_id
    assert row.storage_key == f"orders/{order_id}/report/plan-{plan_id}.docx"
    assert row.storage_key == report_storage_key(order_id, plan_id)
    data = await _assert_file_matches_row(session, storage, order_id)
    # Текст файла сверяется с планом и строками разделов, а не с assemble_document.
    assert extract_lines(data) == await _expected_from_db(session, order_id)
    assert tmp_files(tmp_path) == []


async def test_completed_file_is_the_rendered_document(
    session: AsyncSession, tmp_path: Path
) -> None:
    order_id = await _generated_order(session)
    storage = LocalFileStorage(tmp_path)
    await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)

    data = await _assert_file_matches_row(session, storage, order_id)

    # Рендерер детерминирован: повторный рендер тех же разделов — те же байты.
    rendered = PracticeReportDocxRenderer().render(
        await service.assemble_document(session, order_id)
    )
    assert data == rendered


# --- Не готово, пропуск, занято -------------------------------------------------------


async def test_incomplete_sections_not_completed(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session, sections=3)
    renderer = RecordingRenderer()

    assert order_id not in await find_orders_for_completion(session, limit=10)
    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert outcome == CompletionOutcome(K.NOT_READY, order_id)
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (S.GENERATING, 0, None)
    assert renderer.calls == 0
    assert await _files(session, order_id) == []
    assert _stored_files(tmp_path) == []


async def test_incomplete_sections_keep_generation_attempt(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Зависшая попытка генерации раздела — не дело завершения: поля шага не меняются."""
    order_id = await _generated_order(session, sections=3)
    started_at = datetime.now(UTC) - timedelta(minutes=11)
    await _set(session, order_id, step_attempts=2, step_started_at=started_at)

    outcome = await complete_report(
        session, LocalFileStorage(tmp_path), RecordingRenderer(), order_id
    )

    assert outcome.kind is K.NOT_READY
    order = await _fresh(session, order_id)
    assert (order.step_attempts, order.step_started_at) == (2, started_at)


@pytest.mark.parametrize("status", [S.FAILED])
async def test_order_not_generating_is_skipped(
    session: AsyncSession, tmp_path: Path, status: S
) -> None:
    order_id = await _generated_order(session)
    await orders.change_status(session, order_id, status, error="manual")
    await session.commit()
    renderer = RecordingRenderer()

    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert outcome == CompletionOutcome(K.SKIPPED, order_id)
    assert renderer.calls == 0


async def test_running_attempt_makes_second_start_busy(
    session: AsyncSession, tmp_path: Path
) -> None:
    order_id = await _generated_order(session)
    await _set(session, order_id, step_attempts=1, step_started_at=datetime.now(UTC))
    renderer = RecordingRenderer()

    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert outcome == CompletionOutcome(K.BUSY, order_id)
    assert renderer.calls == 0
    assert (await _fresh(session, order_id)).step_attempts == 1


async def test_stale_attempt_is_counted_and_retried(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    await _set(session, order_id, step_attempts=1)
    await _stale(session, order_id)

    outcome = await complete_report(
        session, LocalFileStorage(tmp_path), PracticeReportDocxRenderer(), order_id
    )

    assert outcome.kind is K.COMPLETED
    assert (await _fresh(session, order_id)).step_attempts == 2


async def test_stale_third_attempt_fails_without_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    order_id = await _generated_order(session)
    await _set(session, order_id, step_attempts=3)
    await _stale(session, order_id)
    renderer = RecordingRenderer()

    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "attempts_exhausted")
    assert renderer.calls == 0
    order = await _fresh(session, order_id)
    assert (order.status, order.error) == (S.FAILED, "report_completion: attempts_exhausted")


# --- Ошибки ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["validate", "render"])
async def test_invalid_document_fails(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    order_id = await _generated_order(session)
    if stage == "validate":
        code = DocumentErrorCode.EMPTY_PARAGRAPH

        def invalid(content: DocumentContent) -> None:
            raise InvalidDocumentError(code, "sections[0].paragraphs[0]")

        monkeypatch.setattr(service, "validate_document_content", invalid)
        renderer = RecordingRenderer()
    else:
        code = DocumentErrorCode.TITLE_PAGE_UNSUPPORTED
        renderer = RecordingRenderer(error=InvalidDocumentError(code, "title_page"))

    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, f"invalid_document:{code}")
    order = await _fresh(session, order_id)
    assert order.status is S.FAILED
    assert order.error == f"report_completion: invalid_document:{code}"
    assert renderer.calls == (0 if stage == "validate" else 1)
    assert await _files(session, order_id) == []
    assert _stored_files(tmp_path) == []


async def test_incomplete_document_fails(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """assemble_document не нашёл раздел или план (несогласованные данные) — FAILED."""
    order_id = await _generated_order(session)

    async def missing(session: AsyncSession, order_id: int) -> DocumentContent:
        raise VersionNotFoundError(f"Order {order_id}: section 0 is not generated")

    monkeypatch.setattr(service, "assemble_document", missing)
    renderer = RecordingRenderer()

    outcome = await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "incomplete_document")
    assert (await _fresh(session, order_id)).error == "report_completion: incomplete_document"
    assert renderer.calls == 0
    assert await _files(session, order_id) == []


@pytest.mark.parametrize("source", ["render", "storage"])
async def test_temporary_error_retried_then_exhausted(
    session: AsyncSession, tmp_path: Path, source: str
) -> None:
    order_id = await _generated_order(session)
    if source == "render":
        renderer = RecordingRenderer(error=RuntimeError("boom"))
        storage = RecordingStorage(tmp_path)
        code = "render_error"
    else:
        renderer = RecordingRenderer()
        storage = RecordingStorage(tmp_path, failures=10)
        code = "storage_error"

    outcomes = [await complete_report(session, storage, renderer, order_id) for _ in range(3)]

    assert [(o.kind, o.error_code) for o in outcomes] == [
        (K.RETRY_LATER, code),
        (K.RETRY_LATER, code),
        (K.FAILED, "attempts_exhausted"),
    ]
    order = await _fresh(session, order_id)
    assert (order.status, order.error) == (S.FAILED, "report_completion: attempts_exhausted")
    assert order.step_attempts == 3
    assert await _files(session, order_id) == []
    assert _stored_files(tmp_path) == []


async def test_storage_error_is_retried_later(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    storage = RecordingStorage(tmp_path, failures=1)
    renderer = RecordingRenderer()

    first = await complete_report(session, storage, renderer, order_id)

    assert (first.kind, first.error_code) == (K.RETRY_LATER, "storage_error")
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (S.GENERATING, 1, None)
    assert await _files(session, order_id) == []
    # Следующий проход восстановления берёт заказ сразу.
    assert order_id in await find_orders_for_completion(session, limit=10)

    second = await complete_report(session, storage, renderer, order_id)

    assert second.kind is K.COMPLETED
    await _assert_file_matches_row(session, storage, order_id)
    assert storage.saves == 2


async def test_unexpected_error_leaves_attempt_to_recovery(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await _generated_order(session)

    async def broken(session: AsyncSession, order_id: int) -> DocumentContent:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(service, "assemble_document", broken)

    with pytest.raises(RuntimeError):
        await complete_report(session, LocalFileStorage(tmp_path), RecordingRenderer(), order_id)
    await session.rollback()

    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts) == (S.GENERATING, 1)
    assert order.step_started_at is not None


# --- Повтор и частичный сбой ----------------------------------------------------------


async def test_rerun_after_completed_is_noop(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    storage = RecordingStorage(tmp_path)
    renderer = RecordingRenderer()
    await complete_report(session, storage, renderer, order_id)
    before = await _assert_file_matches_row(session, storage, order_id)

    outcome = await complete_report(session, storage, renderer, order_id)

    assert outcome == CompletionOutcome(K.SKIPPED, order_id)
    assert (renderer.calls, storage.saves) == (1, 1)
    assert await _assert_file_matches_row(session, storage, order_id) == before


async def test_partial_failure_storage_ok_db_failed(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файл сохранён, T3 не зафиксирован: повтор после 10 минут — один результат."""
    order_id = await _generated_order(session)
    storage = LocalFileStorage(tmp_path)
    real_change_status = service.change_status

    async def broken(session: AsyncSession, order_id: int, target: S, **kwargs: Any) -> Order:
        if target is S.COMPLETED:
            raise RuntimeError("connection lost")
        return await real_change_status(session, order_id, target, **kwargs)

    monkeypatch.setattr(service, "change_status", broken)
    with pytest.raises(RuntimeError):
        await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)
    await session.rollback()

    key = report_storage_key(order_id, await _plan_id(session, order_id))
    first = await storage.read(key)
    assert await _files(session, order_id) == []
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts) == (S.GENERATING, 1)
    # Попытка ещё не зависла: повтор — только после порога.
    renderer = RecordingRenderer()
    assert (await complete_report(session, storage, renderer, order_id)).kind is K.BUSY
    assert order_id not in await find_orders_for_completion(session, limit=10)

    monkeypatch.setattr(service, "change_status", real_change_status)
    await _stale(session, order_id)
    assert order_id in await find_orders_for_completion(session, limit=10)
    outcome = await complete_report(session, storage, renderer, order_id)

    assert outcome.kind is K.COMPLETED
    data = await _assert_file_matches_row(session, storage, order_id)
    assert data == first  # тот же ключ, те же байты
    assert [path.name for path in _stored_files(tmp_path)] == [Path(key).name]


async def test_existing_result_row_completes_without_render(
    session: AsyncSession, tmp_path: Path
) -> None:
    order_id = await _generated_order(session)
    plan_id = await _plan_id(session, order_id)
    session.add(
        OrderReportFile(
            order_id=order_id,
            plan_version_id=plan_id,
            storage_key=report_storage_key(order_id, plan_id),
            size_bytes=1,
            sha256="0" * 64,
        )
    )
    await session.commit()
    storage = RecordingStorage(tmp_path)
    renderer = RecordingRenderer()

    outcome = await complete_report(session, storage, renderer, order_id)

    assert outcome == CompletionOutcome(K.COMPLETED, order_id)
    assert (await _fresh(session, order_id)).status is S.COMPLETED
    assert (renderer.calls, storage.saves) == (0, 0)
    assert len(await _files(session, order_id)) == 1


async def test_result_of_taken_over_attempt_is_discarded(
    session: AsyncSession, tmp_path: Path
) -> None:
    order_id = await _generated_order(session)

    async def taken_over() -> None:
        await _set(
            session,
            order_id,
            step_attempts=Order.step_attempts + 1,
            step_started_at=datetime.now(UTC),
        )

    storage = RecordingStorage(tmp_path, on_save=taken_over)

    outcome = await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)

    assert outcome == CompletionOutcome(K.DISCARDED, order_id)
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts) == (S.GENERATING, 2)
    assert await _files(session, order_id) == []


# --- Цикл событий и транзакции --------------------------------------------------------


async def test_render_runs_off_event_loop(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    renderer = RecordingRenderer()

    await complete_report(session, LocalFileStorage(tmp_path), renderer, order_id)

    assert renderer.threads and renderer.threads[0] != threading.get_ident()


async def test_no_transaction_during_render_and_save(session: AsyncSession, tmp_path: Path) -> None:
    order_id = await _generated_order(session)
    seen: dict[str, bool] = {}

    def on_render() -> None:
        seen["render"] = session.in_transaction()

    async def on_save() -> None:
        seen["save"] = session.in_transaction()

    outcome = await complete_report(
        session,
        RecordingStorage(tmp_path, on_save=on_save),
        RecordingRenderer(on_render=on_render),
        order_id,
    )

    assert outcome.kind is K.COMPLETED
    assert seen == {"render": False, "save": False}


# --- Восстановление -------------------------------------------------------------------


async def test_find_orders_for_completion(session: AsyncSession, tmp_path: Path) -> None:
    ready = await _generated_order(session, tg_id=1)
    running = await _generated_order(session, tg_id=2)
    stale = await _generated_order(session, tg_id=3)
    incomplete = await _generated_order(session, tg_id=4, sections=3)
    completed = await _generated_order(session, tg_id=5)
    with_file = await _generated_order(session, tg_id=6)
    draft_plan = await _generated_order(session, tg_id=7)
    await _set(session, running, step_attempts=1, step_started_at=datetime.now(UTC))
    await _set(session, stale, step_attempts=1)
    await _stale(session, stale)
    await complete_report(
        session, LocalFileStorage(tmp_path), PracticeReportDocxRenderer(), completed
    )
    plan_id = await _plan_id(session, with_file)
    session.add(
        OrderReportFile(
            order_id=with_file,
            plan_version_id=plan_id,
            storage_key=report_storage_key(with_file, plan_id),
            size_bytes=1,
            sha256="0" * 64,
        )
    )
    # Последняя версия плана — черновик: действующего подтверждённого плана нет.
    confirmed = await session.get(OrderPlanVersion, await _plan_id(session, draft_plan))
    assert confirmed is not None
    session.add(
        OrderPlanVersion(
            order_id=draft_plan,
            version=2,
            status=VersionStatus.DRAFT,
            content=confirmed.content,
            requirements_version_id=confirmed.requirements_version_id,
        )
    )
    await session.commit()

    found = await find_orders_for_completion(session, limit=10)

    assert set(found) == {ready, stale}
    assert not {running, incomplete, completed, with_file, draft_plan} & set(found)
    assert len(await find_orders_for_completion(session, limit=1)) == 1


async def test_pass_completes_and_continues_after_order_exception(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = await _generated_order(session, tg_id=21)
    ok = await _generated_order(session, tg_id=22)
    real_assemble = service.assemble_document

    async def assemble(session: AsyncSession, order_id: int) -> DocumentContent:
        if order_id == broken:
            raise RuntimeError("secret details")
        return await real_assemble(session, order_id)

    monkeypatch.setattr(service, "assemble_document", assemble)
    storage = LocalFileStorage(tmp_path)

    outcomes = await run_completion_pass(sessionmaker, storage, PracticeReportDocxRenderer())

    assert [(o.order_id, o.kind) for o in outcomes] == [(ok, K.COMPLETED)]
    assert f"order_id={broken} error=RuntimeError" in caplog.text
    assert "secret details" not in caplog.text
    assert (await _fresh(session, broken)).status is S.GENERATING


async def test_recovery_runs_first_pass_at_start(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    order_id = await _generated_order(session)
    passes: list[list[Any]] = []

    class Recording(ReportCompletionRecovery):
        async def run_pass(self) -> list[Any]:
            outcomes = await super().run_pass()
            passes.append(outcomes)
            return outcomes

    recovery = Recording(sessionmaker, LocalFileStorage(tmp_path), interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(250):
            if passes:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert [(o.order_id, o.kind) for o in passes[0]] == [(order_id, K.COMPLETED)]
    assert (await _fresh(session, order_id)).status is S.COMPLETED
    assert not recovery.running


async def test_completion_recovery_registered_without_llm(
    sessionmaker: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OD-3: проход завершения регистрируется и без LLM-клиента и извлекателя."""
    # Роутеры — синглтоны модулей: подключены к диспетчеру из других тестов.
    for router in (
        start.router,
        menu.router,
        order_intake.router,
        plan.router,
        requirements_dialog.router,
    ):
        monkeypatch.setattr(router, "_parent_router", None)
    created: list[tuple[object, object]] = []

    class Fake:
        def __init__(self, sessionmaker: object, storage: object) -> None:
            created.append((sessionmaker, storage))

        def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(setup, "ReportCompletionRecovery", Fake)
    file_storage = LocalFileStorage(tmp_path)
    dispatcher = create_dispatcher(sessionmaker, MemoryStorage(), file_storage)

    startup, *_ = dispatcher.startup.handlers
    await startup.callback(None)

    assert created == [(sessionmaker, file_storage)]


async def test_errors_and_logs_have_no_report_text(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    failed = await _generated_order(session, tg_id=31)
    retried = await _generated_order(session, tg_id=32)

    await complete_report(
        session,
        LocalFileStorage(tmp_path),
        RecordingRenderer(error=InvalidDocumentError(DocumentErrorCode.TOO_LARGE)),
        failed,
    )
    await complete_report(
        session, RecordingStorage(tmp_path, failures=1), RecordingRenderer(), retried
    )

    assert (await _fresh(session, failed)).error == "report_completion: invalid_document:too_large"
    assert (await _fresh(session, retried)).error is None
    text = caplog.text
    assert "аааааааааа" not in text  # текст разделов в report_samples — из букв «а»
    for value in PERSONAL_VALUES:
        assert value not in text


def test_recovery_constants() -> None:
    assert service.MAX_COMPLETION_ATTEMPTS == 3
    assert timedelta(minutes=10) == service.STEP_STALE_AFTER
    assert service.RECOVERY_INTERVAL_SECONDS == 60.0
    assert service.RECOVERY_BATCH == 5
    assert report_storage_key(42, 7) == "orders/42/report/plan-7.docx"
