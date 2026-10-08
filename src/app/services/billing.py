"""Кредиты: покупка за реальные деньги и оплата заказа кредитами.

Две разные операции:

1. Пополнение — финансовая операция с провайдером. create_topup создаёт счёт
   (payments, PENDING, с зафиксированными amount и credits); confirm_topup по
   подтверждению провайдера в одной транзакции переводит платёж в SUCCEEDED и
   зачисляет кредиты (TOPUP в журнале + users.balance). Блокировки: платёж → пользователь.
2. Оплата заказа — внутренняя операция. pay_order в одной транзакции списывает цену
   заказа (ORDER_CHARGE + users.balance) и переводит заказ
   PAYMENT_PENDING → PAID → GENERATING. Блокировки: пользователь → заказ.
3. Возврат за сбой генерации после оплаты. refund_failed_order в одной транзакции
   возвращает фактическое списание (ORDER_REFUND + users.balance) и переводит заказ
   FAILED → CANCELLED. Блокировки: пользователь → заказ.

Единый порядок блокировок: платёж → пользователь → заказ. Пользователь раньше заказа —
как у любого апдейта бота: UserMiddleware в начале транзакции обновляет строку
пользователя (и держит её блокировку до commit), а хендлер затем блокирует заказ
(lock_order). Обратный порядок у оплаты давал взаимную блокировку с «Отменить»,
устаревшими кнопками плана и ТЗ и т. п. Платёж раньше пользователя: подтверждение
оплаты фиксирует транзакцию UserMiddleware до confirm_topup, а сверка работает без
неё. Внешних вызовов внутри этих транзакций нет: счёт отправляется после commit
create_topup.

Инвариант: users.balance == SUM(balance_transactions.amount) пользователя. Баланс
меняется только здесь и только вместе с записью в журнал.

Идемпотентность: повторное или параллельное подтверждение одного платежа упирается в
блокировку строки платежа и её статус (а в крайнем случае — в уникальность payment_id
в журнале и (provider, provider_payment_id)); повторная оплата заказа — в
lock_order и статус (а в крайнем случае — в уникальность (order_id, reason)).

В логах — только идентификаторы и коды, без Telegram id и содержимого заказа.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BalanceTransaction, Order, Payment, User
from app.domain.billing import INVOICE_TTL, CreditPackage
from app.domain.enums import OrderStatus, PaymentProvider, PaymentStatus, TransactionReason
from app.domain.errors import OrderNotFoundError
from app.services.orders import change_status, get_active_order, lock_order
from app.services.payments.base import PaymentNotification
from app.services.payments.telegram_stars import STARS_CURRENCY

logger = logging.getLogger(__name__)

S = OrderStatus

TOPUP_PAYLOAD_PREFIX = "topup:"
_TOPUP_PAYLOAD = re.compile(r"topup:([1-9][0-9]{0,17})")


class PreCheckoutRefusal(StrEnum):
    UNKNOWN_INVOICE = "unknown_invoice"
    FOREIGN_INVOICE = "foreign_invoice"
    ALREADY_PAID = "already_paid"
    EXPIRED = "expired"
    AMOUNT_MISMATCH = "amount_mismatch"


class TopupOutcomeKind(StrEnum):
    # Платёж подтверждён, кредиты зачислены.
    CREDITED = "credited"
    # Этот платёж уже зачислен: повтор подтверждения ничего не меняет.
    ALREADY_CREDITED = "already_credited"
    # Подтверждение не принято: кредиты не зачислены, нужен ручной разбор.
    REJECTED = "rejected"


class TopupRejection(StrEnum):
    UNKNOWN_INVOICE = "unknown_invoice"
    NOT_PENDING = "not_pending"
    # Счёт уже оплачен другим платежом провайдера.
    DUPLICATE_CHARGE = "duplicate_charge"
    FOREIGN_PAYER = "foreign_payer"
    AMOUNT_MISMATCH = "amount_mismatch"
    NOT_SUCCEEDED = "not_succeeded"


@dataclass(frozen=True)
class TopupOutcome:
    kind: TopupOutcomeKind
    payment_id: int | None
    credits: int = 0
    balance: int | None = None
    error_code: TopupRejection | None = None


class ChargeOutcomeKind(StrEnum):
    # Кредиты списаны, заказ в GENERATING.
    PAID = "paid"
    # Кредитов не хватает: ничего не изменилось.
    INSUFFICIENT = "insufficient"
    # Заказ уже оплачен (есть списание).
    ALREADY_PAID = "already_paid"
    # Заказ в статусе, где оплата невозможна (отменён, ещё не подтверждён план…).
    NOT_PAYABLE = "not_payable"


@dataclass(frozen=True)
class ChargeOutcome:
    kind: ChargeOutcomeKind
    order_id: int
    price: int | None = None
    balance: int | None = None


def topup_payload(payment_id: int) -> str:
    """Payload счёта Telegram: связь со строкой payments (до 128 байт)."""
    return f"{TOPUP_PAYLOAD_PREFIX}{payment_id}"


def parse_topup_payload(payload: str) -> int | None:
    match = _TOPUP_PAYLOAD.fullmatch(payload)
    return int(match.group(1)) if match else None


# --- Пополнение -----------------------------------------------------------------------


async def create_topup(session: AsyncSession, user_id: int, package: CreditPackage) -> Payment:
    """Счёт на пакет кредитов: строка payments в PENDING (commit до отправки счёта)."""
    payment = Payment(
        user_id=user_id,
        provider=PaymentProvider.TELEGRAM_STARS,
        amount=package.stars,
        currency=STARS_CURRENCY,
        credits=package.credits,
        status=PaymentStatus.PENDING,
    )
    session.add(payment)
    await session.flush()
    await session.commit()
    return payment


async def check_pre_checkout(
    session: AsyncSession, *, payload: str, payer_tg_id: int, amount: int, currency: str
) -> PreCheckoutRefusal | None:
    """Можно ли принять оплату счёта (pre_checkout_query). Только чтение, без блокировок.

    Окончательная проверка — в confirm_topup: к моменту подтверждения деньги уже
    списаны, поэтому там счёт не отвергается по возрасту.
    """
    payment_id = parse_topup_payload(payload)
    if payment_id is None:
        return PreCheckoutRefusal.UNKNOWN_INVOICE
    row = (
        await session.execute(
            select(Payment, User.tg_id)
            .join(User, User.id == Payment.user_id)
            .where(Payment.id == payment_id)
        )
    ).first()
    if row is None:
        return PreCheckoutRefusal.UNKNOWN_INVOICE
    payment, tg_id = row
    if tg_id != payer_tg_id:
        return PreCheckoutRefusal.FOREIGN_INVOICE
    if payment.status is not PaymentStatus.PENDING:
        return PreCheckoutRefusal.ALREADY_PAID
    if payment.created_at < datetime.now(UTC) - INVOICE_TTL:
        return PreCheckoutRefusal.EXPIRED
    if (payment.amount, payment.currency) != (amount, currency):
        return PreCheckoutRefusal.AMOUNT_MISMATCH
    return None


async def confirm_topup(session: AsyncSession, notification: PaymentNotification) -> TopupOutcome:
    """Подтверждение платежа: SUCCEEDED + TOPUP + баланс — один commit, ровно один раз."""
    payment_id = parse_topup_payload(notification.invoice_payload)
    if payment_id is None:
        return _rejected(None, TopupRejection.UNKNOWN_INVOICE)
    payment = await session.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        await session.rollback()
        return _rejected(payment_id, TopupRejection.UNKNOWN_INVOICE)

    if payment.status is PaymentStatus.SUCCEEDED:
        same = payment.provider_payment_id == notification.provider_payment_id
        credits = payment.credits
        await session.rollback()
        if same:
            return TopupOutcome(TopupOutcomeKind.ALREADY_CREDITED, payment_id, credits)
        # Второй платёж провайдера за уже оплаченный счёт: деньги получены дважды.
        return _rejected(payment_id, TopupRejection.DUPLICATE_CHARGE)
    if payment.status is not PaymentStatus.PENDING:
        await session.rollback()
        return _rejected(payment_id, TopupRejection.NOT_PENDING)

    tg_id = await session.scalar(select(User.tg_id).where(User.id == payment.user_id))
    rejection = _check_notification(payment, tg_id, notification)
    if rejection is not None:
        await session.rollback()
        return _rejected(payment_id, rejection)

    payment.status = PaymentStatus.SUCCEEDED
    payment.provider_payment_id = notification.provider_payment_id
    payment.raw = notification.raw
    user = await _lock_user(session, payment.user_id)
    session.add(
        BalanceTransaction(
            user_id=user.id,
            amount=payment.credits,
            reason=TransactionReason.TOPUP,
            payment_id=payment.id,
        )
    )
    user.balance += payment.credits
    outcome = TopupOutcome(
        TopupOutcomeKind.CREDITED, payment_id, payment.credits, balance=user.balance
    )
    await session.commit()
    logger.info("Credits topped up: payment_id=%s credits=%s", payment_id, outcome.credits)
    return outcome


def _check_notification(
    payment: Payment, tg_id: int | None, notification: PaymentNotification
) -> TopupRejection | None:
    if notification.status is not PaymentStatus.SUCCEEDED:
        return TopupRejection.NOT_SUCCEEDED
    if tg_id != notification.payer_tg_id:
        return TopupRejection.FOREIGN_PAYER
    if (payment.amount, payment.currency) != (notification.amount, notification.currency):
        return TopupRejection.AMOUNT_MISMATCH
    return None


def _rejected(payment_id: int | None, code: TopupRejection) -> TopupOutcome:
    # Деньги могли быть уже списаны провайдером: только ручной разбор (возвраты — 3.7).
    logger.error("Payment confirmation rejected: payment_id=%s code=%s", payment_id, code)
    return TopupOutcome(TopupOutcomeKind.REJECTED, payment_id, error_code=code)


# --- Оплата заказа --------------------------------------------------------------------


async def pay_order(session: AsyncSession, user_id: int, order_id: int) -> ChargeOutcome:
    """Оплата заказа кредитами: ORDER_CHARGE + баланс + PAYMENT_PENDING → PAID → GENERATING.

    Один commit. При нехватке кредитов ничего не меняется.

    Блокировки — пользователь, затем заказ (см. docstring модуля): тот же порядок, что
    у любого хендлера после UserMiddleware.

    Raises OrderNotFoundError (нет заказа или он чужой).
    """
    user = await _lock_user(session, user_id)
    order = await lock_order(session, order_id)
    if order.user_id != user_id:
        await session.rollback()
        raise OrderNotFoundError(order_id)
    price = order.price
    if order.status is not S.PAYMENT_PENDING:
        charged = await _has_charge(session, order_id)
        await session.rollback()
        kind = ChargeOutcomeKind.ALREADY_PAID if charged else ChargeOutcomeKind.NOT_PAYABLE
        return ChargeOutcome(kind, order_id, price)
    if price is None:
        # Цена фиксируется при входе в PAYMENT_PENDING: нарушение целостности.
        logger.error("Payment pending order has no price: order_id=%s", order_id)
        await session.rollback()
        return ChargeOutcome(ChargeOutcomeKind.NOT_PAYABLE, order_id)

    balance = user.balance
    if balance < price:
        await session.rollback()
        return ChargeOutcome(ChargeOutcomeKind.INSUFFICIENT, order_id, price, balance)

    session.add(
        BalanceTransaction(
            user_id=user_id,
            amount=-price,
            reason=TransactionReason.ORDER_CHARGE,
            order_id=order_id,
        )
    )
    user.balance = balance - price
    await session.flush()
    # change_status(PAID) проверяет, что списание есть (OD-4).
    await change_status(session, order_id, S.PAID)
    await change_status(session, order_id, S.GENERATING)
    await session.commit()
    logger.info("Order paid with credits: order_id=%s price=%s", order_id, price)
    return ChargeOutcome(ChargeOutcomeKind.PAID, order_id, price, balance - price)


# --- Возврат за неудавшийся заказ -----------------------------------------------------


class RefundOutcomeKind(StrEnum):
    # Кредиты возвращены, заказ отменён.
    REFUNDED = "refunded"
    # Возврат по заказу уже есть: повтор ничего не меняет.
    ALREADY_REFUNDED = "already_refunded"
    # Не сбой генерации после оплаты, нет списания или заказа нет.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RefundOutcome:
    kind: RefundOutcomeKind
    order_id: int
    amount: int = 0
    balance: int | None = None
    # Кому сообщить о возврате (Telegram id владельца заказа); только для REFUNDED.
    chat_id: int | None = None


async def refund_failed_order(session: AsyncSession, order_id: int) -> RefundOutcome:
    """Сбой генерации после оплаты: ORDER_REFUND + баланс + FAILED → CANCELLED, один commit.

    Сумма возврата — фактическое списание (ORDER_CHARGE), а не orders.price.
    Блокировки — пользователь, затем заказ (как у pay_order). Без списания заказ не
    отменяется и ничего не возвращается: только logger.error.
    """
    user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
    if user_id is None:
        await session.rollback()
        return RefundOutcome(RefundOutcomeKind.SKIPPED, order_id)
    user = await _lock_user(session, user_id)
    order = await lock_order(session, order_id)
    if order.status is not S.FAILED or order.failed_from_status is not S.GENERATING:
        await session.rollback()
        return RefundOutcome(RefundOutcomeKind.SKIPPED, order_id)
    if await _transaction(session, order_id, TransactionReason.ORDER_REFUND) is not None:
        # Возврат и CANCELLED пишутся одним commit: штатно сюда не попасть.
        logger.error("Failed order already refunded but not cancelled: order_id=%s", order_id)
        await session.rollback()
        return RefundOutcome(RefundOutcomeKind.ALREADY_REFUNDED, order_id)
    charge = await _transaction(session, order_id, TransactionReason.ORDER_CHARGE)
    if charge is None:
        logger.error("Failed paid order has no credit charge: order_id=%s", order_id)
        await session.rollback()
        return RefundOutcome(RefundOutcomeKind.SKIPPED, order_id)

    amount = -charge.amount
    session.add(
        BalanceTransaction(
            user_id=user_id,
            amount=amount,
            reason=TransactionReason.ORDER_REFUND,
            order_id=order_id,
        )
    )
    user.balance += amount
    outcome = RefundOutcome(
        RefundOutcomeKind.REFUNDED, order_id, amount, balance=user.balance, chat_id=user.tg_id
    )
    await session.flush()
    # change_status(CANCELLED) проверяет, что возврат есть.
    await change_status(session, order_id, S.CANCELLED)
    await session.commit()
    logger.info("Failed order refunded: order_id=%s amount=%s", order_id, amount)
    return outcome


# --- Чтение ---------------------------------------------------------------------------


async def get_balance(session: AsyncSession, user_id: int) -> int:
    balance = await session.scalar(select(User.balance).where(User.id == user_id))
    return int(balance or 0)


async def ledger_balance(session: AsyncSession, user_id: int) -> int:
    """Сумма журнала пользователя — источник истины для users.balance."""
    stmt = select(func.coalesce(func.sum(BalanceTransaction.amount), 0)).where(
        BalanceTransaction.user_id == user_id
    )
    return int(await session.scalar(stmt) or 0)


async def payable_order(session: AsyncSession, user_id: int) -> Order | None:
    """Активный заказ пользователя, ждущий оплаты, — для кнопки после пополнения."""
    order = await get_active_order(session, user_id)
    return order if order is not None and order.status is S.PAYMENT_PENDING else None


async def _lock_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:  # FK payments.user_id / orders.user_id: невозможно
        raise LookupError(f"User {user_id} not found")
    return user


async def _transaction(
    session: AsyncSession, order_id: int, reason: TransactionReason
) -> BalanceTransaction | None:
    """Проводка заказа с причиной reason: их не больше одной (UNIQUE(order_id, reason))."""
    stmt = select(BalanceTransaction).where(
        BalanceTransaction.order_id == order_id, BalanceTransaction.reason == reason
    )
    return await session.scalar(stmt)


async def _has_charge(session: AsyncSession, order_id: int) -> bool:
    stmt = select(BalanceTransaction.id).where(
        BalanceTransaction.order_id == order_id,
        BalanceTransaction.reason == TransactionReason.ORDER_CHARGE,
    )
    return await session.scalar(stmt) is not None
