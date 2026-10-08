"""Возврат кредитов за сбой генерации после оплаты (FAILED с failed_from = GENERATING).

Генерация разделов и завершение отчёта переводят заказ в FAILED и больше его не
трогают. Проход восстановления находит такие заказы и вызывает
services.billing.refund_failed_order: ORDER_REFUND на сумму фактического списания,
баланс и FAILED → CANCELLED — одним commit. После commit — уведомление пользователю,
best-effort: его сбой (или падение процесса после commit) возврат не отменяет.

Идемпотентность: статус под блокировкой заказа и UNIQUE(order_id, reason) — второго
возврата не будет, сколько бы проходов ни запускалось. Заказы без списания (OD-5)
не отменяются: refund_failed_order пишет logger.error и возвращает SKIPPED; в выборке
они идут после заказов со списанием, чтобы не занимать пакет.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import Exists, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BalanceTransaction, Order
from app.domain.enums import OrderStatus, TransactionReason
from app.services.billing import RefundOutcome, RefundOutcomeKind, refund_failed_order
from app.services.periodic import PeriodicPass

logger = logging.getLogger(__name__)

S = OrderStatus

RECOVERY_INTERVAL_SECONDS = 60.0
RECOVERY_BATCH = 5

# (chat_id, исход) → сообщение пользователю
RefundNotifier = Callable[[int, RefundOutcome], Awaitable[None]]


def _transaction_exists(reason: TransactionReason) -> Exists:
    """Есть ли у заказа (коррелированно с Order) проводка с причиной reason."""
    return exists().where(
        BalanceTransaction.order_id == Order.id, BalanceTransaction.reason == reason
    )


async def find_orders_for_refund(session: AsyncSession, *, limit: int) -> list[int]:
    """FAILED(GENERATING) без возврата: сначала со списанием (их и возвращаем)."""
    charged = _transaction_exists(TransactionReason.ORDER_CHARGE)
    stmt = (
        select(Order.id)
        .where(
            Order.status == S.FAILED,
            Order.failed_from_status == S.GENERATING,
            ~_transaction_exists(TransactionReason.ORDER_REFUND),
        )
        .order_by(charged.desc(), Order.updated_at, Order.id)
        .limit(limit)
    )
    order_ids = list(await session.scalars(stmt))
    await session.commit()
    return order_ids


async def run_refund_pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    notify: RefundNotifier,
    *,
    batch: int = RECOVERY_BATCH,
) -> list[RefundOutcome]:
    """Один проход: возврат до ``batch`` заказов, затем уведомления (после commit)."""
    async with sessionmaker() as session:
        order_ids = await find_orders_for_refund(session, limit=batch)

    outcomes: list[RefundOutcome] = []
    for order_id in order_ids:
        try:
            async with sessionmaker() as session:
                outcome = await refund_failed_order(session, order_id)
        except Exception as exc:
            # Без traceback: текст исключения БД может содержать параметры SQL.
            # Транзакция откатилась целиком — следующий проход повторит.
            logger.error("Order refund failed: order_id=%s error=%s", order_id, type(exc).__name__)
            continue
        outcomes.append(outcome)
        if outcome.kind is RefundOutcomeKind.REFUNDED and outcome.chat_id is not None:
            try:
                await notify(outcome.chat_id, outcome)
            except Exception as exc:
                # Возврат уже зафиксирован: потеря уведомления допустима (OD-3).
                logger.warning(
                    "Refund notification failed: order_id=%s error=%s",
                    order_id,
                    type(exc).__name__,
                )
    return outcomes


class RefundRecovery(PeriodicPass):
    """Периодический проход возвратов: первый — сразу после start(), дальше раз в 60 с."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        notify: RefundNotifier,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        batch: int = RECOVERY_BATCH,
    ) -> None:
        super().__init__(name="order-refunds", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._notify = notify
        self._batch = batch

    async def run_pass(self) -> list[RefundOutcome]:
        return await run_refund_pass(self._sessionmaker, self._notify, batch=self._batch)
