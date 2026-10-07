"""Сверка платежей Telegram Stars: зачисление, если successful_payment не обработан.

Сценарий: Telegram списал звёзды, но апдейт successful_payment потерян (процесс
упал во время обработки, ошибка БД). Счёт остаётся в PENDING. Проход находит такие
счета и ищет их оплату в getStarTransactions: StarTransaction.id совпадает с
telegram_payment_charge_id, source (TransactionPartnerUser) несёт invoice_payload
и плательщика. Найденная оплата подтверждается тем же confirm_topup, что и апдейт:
зачисление ровно один раз, кто бы ни пришёл первым.

Ограничения API и как они учтены:
- у getStarTransactions нет фильтра по payload или дате, только offset/limit
  (не больше 100 за запрос) в хронологическом порядке. Поэтому API вызывается, только
  если есть кандидаты (PENDING-счета старше RECONCILIATION_MIN_AGE и моложе
  RECONCILIATION_WINDOW), и не больше MAX_PAGES страниц за проход;
- чтобы не читать всю историю каждый раз, проход помнит курсор — число первых
  транзакций, которые старше окна (их уже не нужно читать), и id последней из них.
  Перед использованием курсор проверяется: транзакция на позиции курсора должна быть
  той же. Если порядок оказался не хронологическим (вставка в начало), курсор
  сбрасывается в 0. Курсор хранится в памяти: после рестарта первый проход читает
  историю с начала;
- брошенные счета (пользователь не стал платить) остаются PENDING и перестают быть
  кандидатами, когда выходят из окна. pre_checkout не принимает счёт старше
  INVOICE_TTL, а окно сверки шире на сутки — оплата у границы TTL тоже будет найдена.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Payment
from app.domain.billing import INVOICE_TTL
from app.domain.enums import PaymentProvider, PaymentStatus
from app.services.billing import TopupOutcome, confirm_topup, topup_payload
from app.services.payments.telegram_stars import (
    TransactionsFetcher,
    notification_from_transaction,
)
from app.services.periodic import PeriodicPass

logger = logging.getLogger(__name__)

RECONCILIATION_INTERVAL_SECONDS = 300.0
# Время на штатную обработку successful_payment: раньше счёт не сверяется.
RECONCILIATION_MIN_AGE = timedelta(minutes=2)
RECONCILIATION_WINDOW = INVOICE_TTL + timedelta(hours=24)
PAGE_SIZE = 100
MAX_PAGES = 20


@dataclass(frozen=True)
class Cursor:
    """Сколько первых транзакций пропустить и id последней пропущенной."""

    offset: int = 0
    boundary_id: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    outcomes: list[TopupOutcome] = field(default_factory=list)
    cursor: Cursor = Cursor()
    pages: int = 0


async def find_payments_for_reconciliation(session: AsyncSession, *, now: datetime) -> set[str]:
    """Payload'ы PENDING-счетов Stars, которые могли быть оплачены без подтверждения."""
    stmt = select(Payment.id).where(
        Payment.provider == PaymentProvider.TELEGRAM_STARS,
        Payment.status == PaymentStatus.PENDING,
        Payment.created_at >= now - RECONCILIATION_WINDOW,
        Payment.created_at <= now - RECONCILIATION_MIN_AGE,
    )
    payment_ids = list(await session.scalars(stmt))
    await session.commit()
    return {topup_payload(payment_id) for payment_id in payment_ids}


async def reconcile_star_payments(
    sessionmaker: async_sessionmaker[AsyncSession],
    fetch: TransactionsFetcher,
    *,
    cursor: Cursor | None = None,
    now: datetime | None = None,
) -> ReconciliationResult:
    """Один проход сверки. Без кандидатов API не вызывается."""
    cursor = cursor or Cursor()
    now = now or datetime.now(UTC)
    async with sessionmaker() as session:
        pending = await find_payments_for_reconciliation(session, now=now)
    if not pending:
        return ReconciliationResult(cursor=cursor)

    cursor = await _checked_cursor(fetch, cursor)
    window_start = now - RECONCILIATION_WINDOW
    new_cursor = cursor
    in_prefix = True
    previous: datetime | None = None
    outcomes: list[TopupOutcome] = []
    offset = cursor.offset
    pages = 0
    while pending and pages < MAX_PAGES:
        records = await fetch(offset, PAGE_SIZE)
        pages += 1
        for index, record in enumerate(records):
            # Курсор сдвигается только по непрерывному началу, старше окна и в
            # неубывающем порядке дат.
            if (
                in_prefix
                and record.date < window_start
                and (previous is None or record.date >= previous)
            ):
                new_cursor = Cursor(offset + index + 1, record.id)
            else:
                in_prefix = False
            previous = record.date

            notification = notification_from_transaction(record)
            if notification is None or notification.invoice_payload not in pending:
                continue
            pending.discard(notification.invoice_payload)
            try:
                async with sessionmaker() as session:
                    outcomes.append(await confirm_topup(session, notification))
            except Exception as exc:
                # Без traceback: текст исключения БД может содержать параметры SQL.
                # Счёт остаётся PENDING — следующий проход повторит.
                logger.error(
                    "Payment reconciliation failed: payload=%s error=%s",
                    notification.invoice_payload,
                    type(exc).__name__,
                )
        if len(records) < PAGE_SIZE:
            break
        offset += len(records)
    else:
        if pending:
            logger.warning(
                "Payment reconciliation page limit reached: pages=%s unmatched=%s",
                pages,
                len(pending),
            )
    return ReconciliationResult(outcomes, new_cursor, pages)


async def _checked_cursor(fetch: TransactionsFetcher, cursor: Cursor) -> Cursor:
    """Курсор годен, только если на его границе та же транзакция, что и раньше."""
    if cursor.offset == 0:
        return cursor
    records = await fetch(cursor.offset - 1, 1)
    if records and records[0].id == cursor.boundary_id:
        return cursor
    logger.warning("Payment reconciliation cursor reset: offset=%s", cursor.offset)
    return Cursor()


class PaymentReconciliation(PeriodicPass):
    """Периодическая сверка платежей Stars: первый проход — сразу после start()."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        fetch: TransactionsFetcher,
        *,
        interval_seconds: float = RECONCILIATION_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(name="payment-reconciliation", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._fetch = fetch
        self.cursor = Cursor()

    async def run_pass(self) -> list[TopupOutcome]:
        try:
            result = await reconcile_star_payments(
                self._sessionmaker, self._fetch, cursor=self.cursor
            )
        except Exception as exc:
            # Сбой API Telegram или БД: только тип, без текста ответа.
            logger.error("Payment reconciliation pass failed: error=%s", type(exc).__name__)
            return []
        self.cursor = result.cursor
        return result.outcomes
