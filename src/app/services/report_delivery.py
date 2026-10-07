"""Доставка готового отчёта: COMPLETED → файл из FileStorage → документ пользователю.

Не зависит от aiogram: отправку выполняет ReportSender, переданный ботом; о сбое он
сообщает исключением DeliveryError с кодом и признаком временности.

1. Захват (T1) — короткая транзакция под блокировкой заказа: статус COMPLETED, строка
   файла действующего плана, строка доставки (создаётся при первой попытке), уже
   доставлено или ошибка, идущая или зависшая попытка, лимит; номер и начало попытки.
2. Вне транзакций и блокировок: чтение файла, сверка размера и SHA-256 со строкой
   файла, только затем отправка.
3. Фиксация (T3) — под блокировкой: результат принимается, только если строка
   доставки всё ещё в той же попытке (attempts, started_at).

Семантика at-least-once: отправка в Telegram не идемпотентна. Если документ принят,
а процесс упал до T3, попытка зависнет и через STEP_STALE_AFTER будет повторена —
пользователь получит тот же файл второй раз. Это осознанно: доставка не теряется.

Статус заказа доставка не меняет: сбой доставки не делает заказ FAILED, файл
по-прежнему можно скачать из «Мои заказы» (load_report_file; строку доставки
скачивание не трогает). В логи и error — только коды, без текста отчёта и tg_id.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import ColumnElement, ScalarSelect, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.db.models import Order, OrderPlanVersion, OrderReportDelivery, OrderReportFile, User
from app.domain.enums import OrderStatus
from app.services.orders import lock_order
from app.services.periodic import PeriodicPass
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

S = OrderStatus

MAX_DELIVERY_ATTEMPTS = 3
# Попытка дольше этого считается прерванной: чтение файла и отправка занимают секунды.
STEP_STALE_AFTER = timedelta(minutes=10)
RECOVERY_INTERVAL_SECONDS = 60.0
RECOVERY_BATCH = 5

NO_REPORT_FILE = "no_report_file"
FILE_MISSING = "file_missing"
FILE_CORRUPTED = "file_corrupted"
STORAGE_ERROR = "storage_error"
SEND_ERROR = "send_error"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"

# (chat_id, байты файла, имя файла)
ReportSender = Callable[[int, bytes, str], Awaitable[None]]


class DeliveryError(Exception):
    """Сбой отправителя: код (без текста отчёта) и можно ли повторить попытку."""

    def __init__(self, code: str, *, temporary: bool) -> None:
        super().__init__(code)
        self.code = code
        self.temporary = temporary


class ReportUnavailableError(Exception):
    """Файл отчёта нельзя выдать: нет строки файла, файла в хранилище или он изменён."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeliveryOutcomeKind(StrEnum):
    # Документ отправлен, delivered_at записан.
    DELIVERED = "delivered"
    # Временная ошибка: следующая попытка — в проходе восстановления.
    RETRY_LATER = "retry_later"
    # Постоянная ошибка или исчерпан лимит: код в error, повторов не будет.
    FAILED = "failed"
    # Доставка этого заказа уже идёт.
    BUSY = "busy"
    # Заказ не в COMPLETED, нет файла, уже доставлено или ошибка.
    SKIPPED = "skipped"
    # Результат устарел: попытку перехватили.
    DISCARDED = "discarded"


K = DeliveryOutcomeKind


@dataclass(frozen=True)
class DeliveryOutcome:
    kind: DeliveryOutcomeKind
    order_id: int
    error_code: str | None = None


@dataclass(frozen=True)
class ReportFile:
    data: bytes
    filename: str


@dataclass(frozen=True)
class _Claim:
    order_id: int
    delivery_id: int
    attempt: int
    # Вместе с номером однозначно определяет попытку.
    started_at: datetime
    chat_id: int
    storage_key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class _Failure:
    code: str
    temporary: bool


def report_filename(order_id: int) -> str:
    return f"Отчёт по практике №{order_id}.docx"


async def deliver_report(
    session: AsyncSession, storage: FileStorage, send: ReportSender, order_id: int
) -> DeliveryOutcome:
    """Отправляет пользователю готовый файл заказа в COMPLETED (не больше MAX попыток)."""
    claimed = await _claim(session, order_id)
    if isinstance(claimed, DeliveryOutcome):
        return claimed
    result = await _run_attempt(storage, send, claimed)
    return await _finish(session, claimed, result)


async def load_report_file(
    session: AsyncSession, storage: FileStorage, order_id: int
) -> ReportFile:
    """Файл действующего плана для скачивания: байты сверены с размером и SHA-256.

    Владельца и статус проверяет вызывающий. Состояние доставки не меняется. Транзакция
    закрывается до чтения файла.

    Raises ReportUnavailableError (NO_REPORT_FILE, FILE_MISSING, FILE_CORRUPTED); прочие
    ошибки хранилища и БД пробрасываются.
    """
    row = await _current_file(session, order_id)
    await session.commit()  # во время чтения файла транзакция не открыта
    if row is None:
        raise ReportUnavailableError(NO_REPORT_FILE)
    data = await _read_verified(storage, row.storage_key, row.size_bytes, row.sha256)
    return ReportFile(data, report_filename(order_id))


async def find_orders_for_delivery(session: AsyncSession, *, limit: int) -> list[int]:
    """COMPLETED-заказы с файлом действующего плана: не доставлено, без ошибки и без
    идущей попытки (строки доставки ещё нет или попытка зависла)."""
    stale_before = datetime.now(UTC) - STEP_STALE_AFTER
    delivery = OrderReportDelivery
    stmt = (
        select(Order.id)
        .join(OrderReportFile, OrderReportFile.order_id == Order.id)
        .join(OrderPlanVersion, OrderPlanVersion.id == OrderReportFile.plan_version_id)
        .outerjoin(delivery, delivery.report_file_id == OrderReportFile.id)
        .where(
            Order.status == S.COMPLETED,
            OrderPlanVersion.version == _latest_version(Order.id),
            # Без строки доставки все её колонки — NULL.
            delivery.delivered_at.is_(None),
            delivery.error.is_(None),
            or_(delivery.started_at.is_(None), delivery.started_at < stale_before),
        )
        .order_by(Order.updated_at, Order.id)
        .limit(limit)
    )
    order_ids = list(await session.scalars(stmt))
    await session.commit()
    return order_ids


# --- Попытка ------------------------------------------------------------------------


async def _claim(session: AsyncSession, order_id: int) -> _Claim | DeliveryOutcome:
    order = await lock_order(session, order_id)
    if order.status is not S.COMPLETED:
        await session.rollback()
        return DeliveryOutcome(K.SKIPPED, order_id)

    report_file = await _current_file(session, order_id)
    if report_file is None:
        # Строка файла и COMPLETED пишутся одним commit: нарушение целостности.
        logger.error("Completed order has no report file: order_id=%s", order_id)
        await session.rollback()
        return DeliveryOutcome(K.SKIPPED, order_id, error_code=NO_REPORT_FILE)

    delivery = await session.scalar(
        select(OrderReportDelivery)
        .where(OrderReportDelivery.report_file_id == report_file.id)
        .execution_options(populate_existing=True)
    )
    if delivery is None:
        # Под блокировкой заказа второй строки никто не создаст; UNIQUE — страховка.
        delivery = OrderReportDelivery(report_file_id=report_file.id, attempts=0)
        session.add(delivery)
        await session.flush()

    if delivery.delivered_at is not None or delivery.error is not None:
        await session.rollback()
        return DeliveryOutcome(K.SKIPPED, order_id)

    if delivery.started_at is not None:
        if delivery.started_at > datetime.now(UTC) - STEP_STALE_AFTER:
            await session.rollback()
            return DeliveryOutcome(K.BUSY, order_id)
        # Попытка уже засчитана. Документ мог уйти до сбоя: повтор — at-least-once.
        logger.warning(
            "Report delivery attempt interrupted: order_id=%s attempt=%s",
            order_id,
            delivery.attempts,
        )
        delivery.started_at = None

    if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
        return await _fail(session, delivery, order_id, ATTEMPTS_EXHAUSTED)

    chat_id = await session.scalar(select(User.tg_id).where(User.id == order.user_id))
    assert chat_id is not None  # FK orders.user_id
    started_at = datetime.now(UTC)
    delivery.attempts += 1
    delivery.started_at = started_at
    claim = _Claim(
        order_id=order_id,
        delivery_id=delivery.id,
        attempt=delivery.attempts,
        started_at=started_at,
        chat_id=chat_id,
        storage_key=report_file.storage_key,
        size_bytes=report_file.size_bytes,
        sha256=report_file.sha256,
    )
    await session.commit()
    return claim


async def _run_attempt(storage: FileStorage, send: ReportSender, claim: _Claim) -> _Failure | None:
    """Чтение, сверка и отправка — без сессии БД. None — документ отправлен."""
    try:
        data = await _read_verified(storage, claim.storage_key, claim.size_bytes, claim.sha256)
    except ReportUnavailableError as exc:
        return _Failure(exc.code, temporary=False)
    except Exception as exc:
        logger.warning(
            "Report file reading failed: order_id=%s error=%s", claim.order_id, type(exc).__name__
        )
        return _Failure(STORAGE_ERROR, temporary=True)

    try:
        await send(claim.chat_id, data, report_filename(claim.order_id))
    except DeliveryError as exc:
        return _Failure(exc.code, temporary=exc.temporary)
    except Exception as exc:
        logger.warning(
            "Report sending failed: order_id=%s error=%s", claim.order_id, type(exc).__name__
        )
        return _Failure(SEND_ERROR, temporary=True)
    return None


async def _finish(
    session: AsyncSession, claim: _Claim, failure: _Failure | None
) -> DeliveryOutcome:
    await lock_order(session, claim.order_id)
    delivery = await session.get(OrderReportDelivery, claim.delivery_id, populate_existing=True)
    if (
        delivery is None
        or delivery.attempts != claim.attempt
        or delivery.started_at != claim.started_at
    ):
        logger.warning(
            "Stale report delivery result discarded: order_id=%s attempt=%s current=%s",
            claim.order_id,
            claim.attempt,
            None if delivery is None else delivery.attempts,
        )
        await session.commit()
        return DeliveryOutcome(K.DISCARDED, claim.order_id)

    delivery.started_at = None
    if failure is None:
        delivery.delivered_at = datetime.now(UTC)
        await session.commit()
        return DeliveryOutcome(K.DELIVERED, claim.order_id)

    logger.warning(
        "Report delivery attempt failed: order_id=%s attempt=%s code=%s temporary=%s",
        claim.order_id,
        claim.attempt,
        failure.code,
        failure.temporary,
    )
    if not failure.temporary:
        return await _fail(session, delivery, claim.order_id, failure.code)
    if claim.attempt >= MAX_DELIVERY_ATTEMPTS:
        return await _fail(session, delivery, claim.order_id, ATTEMPTS_EXHAUSTED)
    await session.commit()
    return DeliveryOutcome(K.RETRY_LATER, claim.order_id, error_code=failure.code)


async def _fail(
    session: AsyncSession, delivery: OrderReportDelivery, order_id: int, code: str
) -> DeliveryOutcome:
    # Статус заказа не меняется: файл остаётся доступен для скачивания.
    delivery.started_at = None
    delivery.error = code
    await session.commit()
    return DeliveryOutcome(K.FAILED, order_id, error_code=code)


# --- Данные -------------------------------------------------------------------------


def _latest_version(order_id: int | ColumnElement[int]) -> ScalarSelect[int]:
    """Номер последней версии плана заказа (в запросе по заказам — коррелированно).

    Через алиас: внешний запрос сам соединён с order_plan_versions, и без него
    автокорреляция убрала бы из подзапроса все таблицы.
    """
    version = aliased(OrderPlanVersion)
    return select(func.max(version.version)).where(version.order_id == order_id).scalar_subquery()


async def _current_file(session: AsyncSession, order_id: int) -> OrderReportFile | None:
    """Файл действующего плана — последней версии плана заказа."""
    stmt = (
        select(OrderReportFile)
        .join(OrderPlanVersion, OrderPlanVersion.id == OrderReportFile.plan_version_id)
        .where(
            OrderReportFile.order_id == order_id,
            OrderPlanVersion.version == _latest_version(order_id),
        )
    )
    return await session.scalar(stmt)


async def _read_verified(storage: FileStorage, key: str, size_bytes: int, sha256: str) -> bytes:
    try:
        data = await storage.read(key)
    except FileNotFoundError:
        raise ReportUnavailableError(FILE_MISSING) from None
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        raise ReportUnavailableError(FILE_CORRUPTED)
    return data


# --- Восстановление -----------------------------------------------------------------


async def run_delivery_pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    send: ReportSender,
    *,
    batch: int = RECOVERY_BATCH,
) -> list[DeliveryOutcome]:
    """Один проход: доставка до ``batch`` готовых отчётов."""
    async with sessionmaker() as session:
        order_ids = await find_orders_for_delivery(session, limit=batch)

    outcomes: list[DeliveryOutcome] = []
    for order_id in order_ids:
        try:
            async with sessionmaker() as session:
                outcomes.append(await deliver_report(session, storage, send, order_id))
        except Exception as exc:
            # Без traceback: текст исключения БД может содержать параметры SQL.
            # Зависшую попытку подхватит следующий проход.
            logger.error(
                "Report delivery recovery failed: order_id=%s error=%s",
                order_id,
                type(exc).__name__,
            )
    return outcomes


class ReportDeliveryRecovery(PeriodicPass):
    """Периодический проход доставки отчётов: первый — сразу после start()."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        send: ReportSender,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        batch: int = RECOVERY_BATCH,
    ) -> None:
        super().__init__(name="report-delivery", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._send = send
        self._batch = batch

    async def run_pass(self) -> list[DeliveryOutcome]:
        return await run_delivery_pass(
            self._sessionmaker, self._storage, self._send, batch=self._batch
        )
