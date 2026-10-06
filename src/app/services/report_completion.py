"""Завершение отчёта: GENERATING со всеми разделами → DOCX → FileStorage → COMPLETED.

Отдельный шаг после генерации разделов, со своей попыткой в полях шага заказа
(step_attempts, step_started_at): после последнего раздела генерация обнуляет
счётчик, поэтому попытки завершения считаются с нуля.

1. Захват (T1) — короткая транзакция под блокировкой заказа: статус, подтверждённый
   план, все разделы плана сохранены, идущая или зависшая попытка, лимит; номер и
   начало попытки.
2. Сборка (T2) — только чтение: assemble_document().
3. Вне транзакций и блокировок: проверка документа, рендер в asyncio.to_thread,
   SHA-256 и размер, storage.save() по детерминированному ключу.
4. Фиксация (T3) — под блокировкой: результат принимается, только если заказ всё
   ещё в той же попытке. Строка order_report_files и COMPLETED — один commit.

Ошибки: документ некорректен или неполон — сразу FAILED; сбой рендерера или
хранилища — повтор в проходе восстановления, всего MAX_COMPLETION_ATTEMPTS попыток.
В логи и order.error — только коды, без текста отчёта.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Order, OrderPlanVersion, OrderReportFile, OrderReportSection
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import VersionNotFoundError
from app.domain.report_document import InvalidDocumentError, validate_document_content
from app.rendering.base import DocumentRenderer
from app.rendering.docx import PracticeReportDocxRenderer
from app.services.orders import change_status, get_plan_versions, lock_order
from app.services.periodic import PeriodicPass
from app.services.report_generation import assemble_document
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

S = OrderStatus

STEP = "report_completion"
MAX_COMPLETION_ATTEMPTS = 3
# Попытка дольше этого считается прерванной: рендер и сохранение занимают секунды.
STEP_STALE_AFTER = timedelta(minutes=10)
RECOVERY_INTERVAL_SECONDS = 60.0
RECOVERY_BATCH = 5

INCOMPLETE_DOCUMENT = "incomplete_document"
RENDER_ERROR = "render_error"
STORAGE_ERROR = "storage_error"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"


class CompletionOutcomeKind(StrEnum):
    # Файл сохранён, заказ в COMPLETED.
    COMPLETED = "completed"
    # Сохранены не все разделы плана: завершать рано.
    NOT_READY = "not_ready"
    # Временная ошибка: следующая попытка — в проходе восстановления.
    RETRY_LATER = "retry_later"
    FAILED = "failed"
    # Завершение этого заказа уже идёт.
    BUSY = "busy"
    # Заказ не в GENERATING.
    SKIPPED = "skipped"
    # Результат устарел: попытку перехватили или заказ уже не в GENERATING.
    DISCARDED = "discarded"


K = CompletionOutcomeKind


@dataclass(frozen=True)
class CompletionOutcome:
    kind: CompletionOutcomeKind
    order_id: int
    error_code: str | None = None


@dataclass(frozen=True)
class _Claim:
    order_id: int
    attempt: int
    # Вместе с номером однозначно определяет попытку.
    started_at: datetime
    plan_version_id: int


@dataclass(frozen=True)
class _Failure:
    code: str
    temporary: bool


@dataclass(frozen=True)
class _Rendered:
    storage_key: str
    size_bytes: int
    sha256: str


def report_storage_key(order_id: int, plan_version_id: int) -> str:
    """Один ключ на версию плана: повтор после сбоя пишет тот же файл."""
    return f"orders/{order_id}/report/plan-{plan_version_id}.docx"


async def complete_report(
    session: AsyncSession, storage: FileStorage, renderer: DocumentRenderer, order_id: int
) -> CompletionOutcome:
    """Завершает заказ в GENERATING, у которого сохранены все разделы плана."""
    claimed = await _claim(session, order_id)
    if isinstance(claimed, CompletionOutcome):
        return claimed
    result = await _run_attempt(session, storage, renderer, claimed)
    return await _finish(session, claimed, result)


async def find_orders_for_completion(session: AsyncSession, *, limit: int) -> list[int]:
    """Заказы в GENERATING без идущей попытки: все разделы плана есть, файла ещё нет."""
    stale_before = datetime.now(UTC) - STEP_STALE_AFTER
    latest_version = (
        select(func.max(OrderPlanVersion.version))
        .where(OrderPlanVersion.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    saved = (
        select(func.count())
        .where(OrderReportSection.plan_version_id == OrderPlanVersion.id)
        .correlate(OrderPlanVersion)
        .scalar_subquery()
    )
    has_file = exists().where(OrderReportFile.plan_version_id == OrderPlanVersion.id)
    stmt = (
        select(Order.id)
        .join(OrderPlanVersion, OrderPlanVersion.order_id == Order.id)
        .where(
            Order.status == S.GENERATING,
            or_(Order.step_started_at.is_(None), Order.step_started_at < stale_before),
            OrderPlanVersion.version == latest_version,
            OrderPlanVersion.status == VersionStatus.CONFIRMED,
            saved == func.jsonb_array_length(OrderPlanVersion.content["sections"]),
            ~has_file,
        )
        .order_by(Order.updated_at, Order.id)
        .limit(limit)
    )
    order_ids = list(await session.scalars(stmt))
    await session.commit()
    return order_ids


# --- Попытка ------------------------------------------------------------------------


async def _claim(session: AsyncSession, order_id: int) -> _Claim | CompletionOutcome:
    order = await lock_order(session, order_id)
    if order.status is not S.GENERATING:
        await session.rollback()
        return CompletionOutcome(K.SKIPPED, order_id)

    plan = await _confirmed_plan(session, order_id)
    if plan is None:
        # После оплаты невозможно; защита от несогласованных данных.
        return await _fail(session, order_id, INCOMPLETE_DOCUMENT)
    if await _saved_count(session, plan.id) < len(plan.content["sections"]):
        # Поля шага принадлежат генерации разделов: их не трогаем.
        await session.rollback()
        return CompletionOutcome(K.NOT_READY, order_id)

    if order.step_started_at is not None:
        if order.step_started_at > datetime.now(UTC) - STEP_STALE_AFTER:
            await session.rollback()
            return CompletionOutcome(K.BUSY, order_id)
        # Попытка уже засчитана.
        logger.warning(
            "Report completion attempt interrupted: order_id=%s attempt=%s",
            order_id,
            order.step_attempts,
        )
        order.step_started_at = None

    if await _has_file(session, plan.id):
        # Строка и COMPLETED пишутся одним commit, так что штатно сюда не попасть;
        # защита: результат уже есть — второй не создаётся.
        await change_status(session, order_id, S.COMPLETED)
        await session.commit()
        return CompletionOutcome(K.COMPLETED, order_id)

    if order.step_attempts >= MAX_COMPLETION_ATTEMPTS:
        return await _fail(session, order_id, ATTEMPTS_EXHAUSTED)

    started_at = datetime.now(UTC)
    order.step_attempts += 1
    order.step_started_at = started_at
    claim = _Claim(
        order_id=order_id,
        attempt=order.step_attempts,
        started_at=started_at,
        plan_version_id=plan.id,
    )
    await session.commit()
    return claim


async def _run_attempt(
    session: AsyncSession, storage: FileStorage, renderer: DocumentRenderer, claim: _Claim
) -> _Rendered | _Failure:
    try:
        content = await assemble_document(session, claim.order_id)
    except VersionNotFoundError:
        await session.rollback()
        return _Failure(INCOMPLETE_DOCUMENT, temporary=False)
    await session.commit()  # во время рендера и сохранения транзакция не открыта

    try:
        validate_document_content(content)
        data = await asyncio.to_thread(renderer.render, content)
    except InvalidDocumentError as exc:
        return _Failure(f"invalid_document:{exc.code}", temporary=False)
    except Exception as exc:
        logger.warning(
            "Report rendering failed: order_id=%s error=%s", claim.order_id, type(exc).__name__
        )
        return _Failure(RENDER_ERROR, temporary=True)

    key = report_storage_key(claim.order_id, claim.plan_version_id)
    try:
        await storage.save(key, data)
    except Exception as exc:
        logger.warning(
            "Report file saving failed: order_id=%s error=%s", claim.order_id, type(exc).__name__
        )
        return _Failure(STORAGE_ERROR, temporary=True)
    return _Rendered(key, len(data), hashlib.sha256(data).hexdigest())


async def _finish(
    session: AsyncSession, claim: _Claim, result: _Rendered | _Failure
) -> CompletionOutcome:
    order = await lock_order(session, claim.order_id)
    if (
        order.status is not S.GENERATING
        or order.step_attempts != claim.attempt
        or order.step_started_at != claim.started_at
    ):
        logger.warning(
            "Stale report completion result discarded: order_id=%s attempt=%s current=%s/%s",
            claim.order_id,
            claim.attempt,
            order.status,
            order.step_attempts,
        )
        await session.commit()
        return CompletionOutcome(K.DISCARDED, claim.order_id)

    if isinstance(result, _Failure):
        logger.warning(
            "Report completion attempt failed: order_id=%s attempt=%s code=%s temporary=%s",
            claim.order_id,
            claim.attempt,
            result.code,
            result.temporary,
        )
        if not result.temporary:
            return await _fail(session, claim.order_id, result.code)
        if claim.attempt >= MAX_COMPLETION_ATTEMPTS:
            return await _fail(session, claim.order_id, ATTEMPTS_EXHAUSTED)
        order.step_started_at = None
        await session.commit()
        return CompletionOutcome(K.RETRY_LATER, claim.order_id, error_code=result.code)

    session.add(
        OrderReportFile(
            order_id=claim.order_id,
            plan_version_id=claim.plan_version_id,
            storage_key=result.storage_key,
            size_bytes=result.size_bytes,
            sha256=result.sha256,
        )
    )
    await session.flush()
    # Строка файла и COMPLETED — в одном commit.
    await change_status(session, claim.order_id, S.COMPLETED)
    await session.commit()
    return CompletionOutcome(K.COMPLETED, claim.order_id)


async def _fail(session: AsyncSession, order_id: int, code: str) -> CompletionOutcome:
    # В order.error — только код: без текста отчёта.
    await change_status(session, order_id, S.FAILED, error=f"{STEP}: {code}")
    await session.commit()
    return CompletionOutcome(K.FAILED, order_id, error_code=code)


# --- Данные -------------------------------------------------------------------------


async def _confirmed_plan(session: AsyncSession, order_id: int) -> OrderPlanVersion | None:
    """Действующий план — последняя версия, и она подтверждена."""
    versions = await get_plan_versions(session, order_id)
    if not versions or versions[-1].status is not VersionStatus.CONFIRMED:
        return None
    return versions[-1]


async def _saved_count(session: AsyncSession, plan_version_id: int) -> int:
    stmt = select(func.count()).where(OrderReportSection.plan_version_id == plan_version_id)
    return int(await session.scalar(stmt) or 0)


async def _has_file(session: AsyncSession, plan_version_id: int) -> bool:
    stmt = select(OrderReportFile.id).where(OrderReportFile.plan_version_id == plan_version_id)
    return await session.scalar(stmt) is not None


# --- Восстановление -----------------------------------------------------------------


async def run_completion_pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    renderer: DocumentRenderer,
    *,
    batch: int = RECOVERY_BATCH,
) -> list[CompletionOutcome]:
    """Один проход: завершение до ``batch`` заказов с готовыми разделами."""
    async with sessionmaker() as session:
        order_ids = await find_orders_for_completion(session, limit=batch)

    outcomes: list[CompletionOutcome] = []
    for order_id in order_ids:
        try:
            async with sessionmaker() as session:
                outcomes.append(await complete_report(session, storage, renderer, order_id))
        except Exception as exc:
            # Без traceback: текст исключения БД может содержать параметры SQL.
            # Зависшую попытку подхватит следующий проход.
            logger.error(
                "Report completion recovery failed: order_id=%s error=%s",
                order_id,
                type(exc).__name__,
            )
    return outcomes


class ReportCompletionRecovery(PeriodicPass):
    """Периодический проход завершения отчёта: первый — сразу после start()."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        renderer: DocumentRenderer | None = None,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        batch: int = RECOVERY_BATCH,
    ) -> None:
        super().__init__(name="report-completion", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._renderer = renderer or PracticeReportDocxRenderer()
        self._batch = batch

    async def run_pass(self) -> list[CompletionOutcome]:
        return await run_completion_pass(
            self._sessionmaker, self._storage, self._renderer, batch=self._batch
        )
