"""Кредиты (сервис): пополнение Stars, pre_checkout, оплата заказа, цена, журнал.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_billing_concurrency.py. Подтверждения провайдера —
PaymentNotification в том виде, в каком их дают successful_payment и сверка.
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BalanceTransaction, Order, Payment, User
from app.domain import billing as billing_domain
from app.domain.billing import CREDIT_PACKAGES, CreditPackage, order_price
from app.domain.enums import (
    DocType,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    TransactionReason,
)
from app.domain.errors import InvalidStatusTransitionError, OrderNotFoundError, OrderNotPaidError
from app.domain.report_generation import ReportOutcomeKind
from app.rendering.docx import PracticeReportDocxRenderer
from app.services import billing as service
from app.services import orders
from app.services.billing import (
    ChargeOutcome,
    ChargeOutcomeKind,
    PreCheckoutRefusal,
    TopupOutcomeKind,
    TopupRejection,
    check_pre_checkout,
    confirm_topup,
    create_topup,
    ledger_balance,
    pay_order,
    payable_order,
    topup_payload,
)
from app.services.payments.base import PaymentNotification
from app.services.report_completion import CompletionOutcomeKind, complete_report
from app.services.report_delivery import DeliveryOutcomeKind, deliver_report
from app.services.report_generation import find_orders_for_generation, generate_next_section
from app.services.storage.local import LocalFileStorage
from app.services.users import register_or_update_user
from billing_samples import grant_credits
from fake_llm import FakeLLMClient
from report_samples import ALL_GOOD, payment_pending_order
from test_report_delivery import RecordingSender

S = OrderStatus
TG_ID = 777_555_111
PACKAGE = CREDIT_PACKAGES[1]
PRICE = order_price(DocType.PRACTICE_REPORT)


async def _user_id(session: AsyncSession, order_id: int) -> int:
    """id пользователя заказа: целым числом — объекты сессии устаревают после rollback."""
    user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
    assert user_id is not None
    return user_id


async def _new_user_id(session: AsyncSession, tg_id: int = TG_ID) -> int:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    return user.id


async def _topup(session: AsyncSession, user_id: int, package: CreditPackage = PACKAGE) -> Payment:
    """Счёт (зафиксирован); объект отсоединён от сессии — не устаревает после rollback."""
    payment = await create_topup(session, user_id, package)
    session.expunge(payment)
    return payment


def _notification(
    payment: Payment,
    *,
    charge: str = "charge-1",
    amount: int | None = None,
    currency: str = "XTR",
    payer: int = TG_ID,
    payload: str | None = None,
    status: PaymentStatus = PaymentStatus.SUCCEEDED,
) -> PaymentNotification:
    return PaymentNotification(
        provider_payment_id=charge,
        status=status,
        amount=payment.amount if amount is None else amount,
        currency=currency,
        invoice_payload=topup_payload(payment.id) if payload is None else payload,
        payer_tg_id=payer,
        raw={"source": "test", "telegram_payment_charge_id": charge},
    )


async def _fresh_payment(session: AsyncSession, payment_id: int) -> Payment:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert payment is not None
    return payment


async def _fresh_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _transactions(session: AsyncSession, user_id: int) -> list[tuple[int, str]]:
    stmt = (
        select(BalanceTransaction.amount, BalanceTransaction.reason)
        .where(BalanceTransaction.user_id == user_id)
        .order_by(BalanceTransaction.id)
    )
    return [(amount, reason.value) for amount, reason in await session.execute(stmt)]


async def _assert_ledger(session: AsyncSession, user_id: int) -> int:
    """users.balance == SUM(balance_transactions.amount); возвращает баланс."""
    balance = (await _user_by_id(session, user_id)).balance
    assert balance == await ledger_balance(session, user_id)
    assert balance >= 0
    return balance


async def _user_by_id(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


# --- Пополнение -----------------------------------------------------------------------


async def test_create_topup_fixes_terms_of_invoice(session: AsyncSession) -> None:
    user_id = await _new_user_id(session)

    payment = await _topup(session, user_id)
    assert not session.in_transaction()  # commit до отправки счёта

    payment = await _fresh_payment(session, payment.id)
    assert payment.provider is PaymentProvider.TELEGRAM_STARS
    assert (payment.amount, payment.currency, payment.credits) == (
        PACKAGE.stars,
        "XTR",
        PACKAGE.credits,
    )
    assert (payment.status, payment.provider_payment_id) == (PaymentStatus.PENDING, None)
    assert await _transactions(session, user_id) == []


async def test_confirm_topup_credits_once(session: AsyncSession) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)

    outcome = await confirm_topup(session, _notification(payment))

    assert outcome.kind is TopupOutcomeKind.CREDITED
    assert (outcome.payment_id, outcome.credits, outcome.balance) == (
        payment.id,
        PACKAGE.credits,
        PACKAGE.credits,
    )
    payment = await _fresh_payment(session, payment.id)
    assert (payment.status, payment.provider_payment_id) == (PaymentStatus.SUCCEEDED, "charge-1")
    assert payment.raw == {"source": "test", "telegram_payment_charge_id": "charge-1"}
    [row] = await session.scalars(
        select(BalanceTransaction).where(BalanceTransaction.user_id == user_id)
    )
    assert (row.amount, row.reason, row.payment_id, row.order_id) == (
        PACKAGE.credits,
        TransactionReason.TOPUP,
        payment.id,
        None,
    )
    assert await _assert_ledger(session, user_id) == PACKAGE.credits


async def test_duplicate_successful_payment_is_idempotent(session: AsyncSession) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)
    await confirm_topup(session, _notification(payment))

    for _ in range(3):
        outcome = await confirm_topup(session, _notification(payment))
        assert outcome.kind is TopupOutcomeKind.ALREADY_CREDITED

    assert await _transactions(session, user_id) == [(PACKAGE.credits, "topup")]
    assert await _assert_ledger(session, user_id) == PACKAGE.credits


async def test_second_charge_for_paid_invoice_is_rejected(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)
    await confirm_topup(session, _notification(payment))

    outcome = await confirm_topup(session, _notification(payment, charge="charge-2"))

    assert (outcome.kind, outcome.error_code) == (
        TopupOutcomeKind.REJECTED,
        TopupRejection.DUPLICATE_CHARGE,
    )
    assert (await _fresh_payment(session, payment.id)).provider_payment_id == "charge-1"
    assert await _assert_ledger(session, user_id) == PACKAGE.credits
    assert "code=duplicate_charge" in caplog.text


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"amount": PACKAGE.stars - 1}, TopupRejection.AMOUNT_MISMATCH),
        ({"currency": "RUB"}, TopupRejection.AMOUNT_MISMATCH),
        ({"payer": TG_ID + 1}, TopupRejection.FOREIGN_PAYER),
        ({"status": PaymentStatus.CANCELED}, TopupRejection.NOT_SUCCEEDED),
        ({"payload": "topup:999999999"}, TopupRejection.UNKNOWN_INVOICE),
        ({"payload": "garbage"}, TopupRejection.UNKNOWN_INVOICE),
    ],
)
async def test_rejected_confirmation_credits_nothing(
    session: AsyncSession, kwargs: dict[str, Any], code: TopupRejection
) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)

    outcome = await confirm_topup(session, _notification(payment, **kwargs))

    assert (outcome.kind, outcome.error_code) == (TopupOutcomeKind.REJECTED, code)
    payment = await _fresh_payment(session, payment.id)
    assert (payment.status, payment.provider_payment_id) == (PaymentStatus.PENDING, None)
    assert await _transactions(session, user_id) == []
    assert await _assert_ledger(session, user_id) == 0


async def test_credits_come_from_invoice_not_current_packages(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)
    # Прайс изменился после выставления счёта.
    monkeypatch.setattr(
        billing_domain, "CREDIT_PACKAGES", (CreditPackage(PACKAGE.id, 1, PACKAGE.stars),)
    )

    outcome = await confirm_topup(session, _notification(payment))

    assert outcome.credits == PACKAGE.credits
    assert await _assert_ledger(session, user_id) == PACKAGE.credits


async def test_expired_invoice_paid_anyway_is_credited(session: AsyncSession) -> None:
    """Деньги уже списаны Telegram: подтверждение не отвергается по возрасту счёта."""
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)
    await session.execute(
        update(Payment)
        .where(Payment.id == payment.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=3))
    )
    await session.commit()

    outcome = await confirm_topup(session, _notification(payment))

    assert outcome.kind is TopupOutcomeKind.CREDITED


# --- pre_checkout ---------------------------------------------------------------------


async def test_pre_checkout_accepts_own_pending_invoice(session: AsyncSession) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)

    refusal = await check_pre_checkout(
        session,
        payload=topup_payload(payment.id),
        payer_tg_id=TG_ID,
        amount=PACKAGE.stars,
        currency="XTR",
    )

    assert refusal is None
    # Только чтение: ничего не изменилось.
    assert (await _fresh_payment(session, payment.id)).status is PaymentStatus.PENDING


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"payload": "topup:999999999"}, PreCheckoutRefusal.UNKNOWN_INVOICE),
        ({"payload": "nonsense"}, PreCheckoutRefusal.UNKNOWN_INVOICE),
        ({"payer_tg_id": TG_ID + 1}, PreCheckoutRefusal.FOREIGN_INVOICE),
        ({"amount": PACKAGE.stars + 1}, PreCheckoutRefusal.AMOUNT_MISMATCH),
        ({"currency": "RUB"}, PreCheckoutRefusal.AMOUNT_MISMATCH),
    ],
)
async def test_pre_checkout_refusals(
    session: AsyncSession, change: dict[str, Any], refusal: PreCheckoutRefusal
) -> None:
    user_id = await _new_user_id(session)
    payment = await _topup(session, user_id)
    kwargs: dict[str, Any] = {
        "payload": topup_payload(payment.id),
        "payer_tg_id": TG_ID,
        "amount": PACKAGE.stars,
        "currency": "XTR",
    }
    kwargs.update(change)

    assert await check_pre_checkout(session, **kwargs) is refusal


async def test_pre_checkout_refuses_paid_and_expired(session: AsyncSession) -> None:
    user_id = await _new_user_id(session)
    paid = await _topup(session, user_id)
    await confirm_topup(session, _notification(paid))
    old = await _topup(session, user_id)
    await session.execute(
        update(Payment)
        .where(Payment.id == old.id)
        .values(created_at=datetime.now(UTC) - billing_domain.INVOICE_TTL - timedelta(minutes=1))
    )
    await session.commit()

    async def check(payment: Payment) -> PreCheckoutRefusal | None:
        return await check_pre_checkout(
            session,
            payload=topup_payload(payment.id),
            payer_tg_id=TG_ID,
            amount=PACKAGE.stars,
            currency="XTR",
        )

    assert await check(paid) is PreCheckoutRefusal.ALREADY_PAID
    assert await check(old) is PreCheckoutRefusal.EXPIRED


# --- Цена -----------------------------------------------------------------------------


async def test_price_fixed_when_plan_confirmed(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)

    order = await _fresh_order(session, order_id)
    assert (order.status, order.price) == (S.PAYMENT_PENDING, PRICE)


async def test_price_change_does_not_affect_existing_order(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    monkeypatch.setitem(billing_domain.ORDER_PRICES, DocType.PRACTICE_REPORT, PRICE * 3)

    # Повторный вход в PAYMENT_PENDING цену не пересчитывает.
    await orders.change_status(session, order_id, S.WAITING_FOR_PLAN_CONFIRMATION)
    await orders.change_status(session, order_id, S.PAYMENT_PENDING)
    await session.commit()
    assert (await _fresh_order(session, order_id)).price == PRICE

    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE)
    outcome = await pay_order(session, user_id, order_id)

    assert outcome == ChargeOutcome(ChargeOutcomeKind.PAID, order_id, PRICE, 0)
    assert await _transactions(session, user_id) == [(PRICE, "bonus"), (-PRICE, "order_charge")]


async def test_new_order_gets_new_price(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(billing_domain.ORDER_PRICES, DocType.PRACTICE_REPORT, PRICE * 2)

    order_id = await payment_pending_order(session, tg_id=TG_ID)

    assert (await _fresh_order(session, order_id)).price == PRICE * 2


# --- Оплата заказа --------------------------------------------------------------------


async def test_pay_order_charges_and_starts_generation(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE + 30)
    await session.commit()

    outcome = await pay_order(session, user_id, order_id)

    assert outcome == ChargeOutcome(ChargeOutcomeKind.PAID, order_id, PRICE, 30)
    order = await _fresh_order(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (S.GENERATING, 0, None)
    [charge] = await session.scalars(
        select(BalanceTransaction).where(
            BalanceTransaction.reason == TransactionReason.ORDER_CHARGE,
            BalanceTransaction.user_id == user_id,
        )
    )
    assert (charge.amount, charge.order_id, charge.payment_id) == (-PRICE, order_id, None)
    assert await _assert_ledger(session, user_id) == 30
    assert order_id in await find_orders_for_generation(session, limit=10)
    assert not session.in_transaction()


async def test_pay_order_with_exact_balance(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE)

    assert (await pay_order(session, user_id, order_id)).kind is ChargeOutcomeKind.PAID
    assert await _assert_ledger(session, user_id) == 0


async def test_insufficient_credits_change_nothing(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE - 1)
    await session.commit()

    outcome = await pay_order(session, user_id, order_id)

    assert outcome == ChargeOutcome(ChargeOutcomeKind.INSUFFICIENT, order_id, PRICE, PRICE - 1)
    assert (await _fresh_order(session, order_id)).status is S.PAYMENT_PENDING
    assert await _transactions(session, user_id) == [(PRICE - 1, "bonus")]
    assert await _assert_ledger(session, user_id) == PRICE - 1
    # Генерация не запускается без оплаты.
    assert order_id not in await find_orders_for_generation(session, limit=10)


async def test_repeated_payment_charges_once(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE * 3)
    await pay_order(session, user_id, order_id)

    for _ in range(2):
        outcome = await pay_order(session, user_id, order_id)
        assert outcome.kind is ChargeOutcomeKind.ALREADY_PAID

    assert await _transactions(session, user_id) == [(PRICE * 3, "bonus"), (-PRICE, "order_charge")]
    assert await _assert_ledger(session, user_id) == PRICE * 2


async def test_foreign_order_cannot_be_paid(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    stranger_id = await _new_user_id(session, tg_id=TG_ID + 1)
    await grant_credits(session, stranger_id, PRICE)
    await session.commit()

    with pytest.raises(OrderNotFoundError):
        await pay_order(session, stranger_id, order_id)
    await session.rollback()

    assert (await _fresh_order(session, order_id)).status is S.PAYMENT_PENDING
    assert await _assert_ledger(session, stranger_id) == PRICE


async def test_unpayable_statuses(session: AsyncSession) -> None:
    cancelled = await payment_pending_order(session, tg_id=TG_ID)
    await orders.change_status(session, cancelled, S.CANCELLED)
    await session.commit()
    planning = await payment_pending_order(session, tg_id=TG_ID + 1)
    await orders.change_status(session, planning, S.WAITING_FOR_PLAN_CONFIRMATION)
    await session.commit()

    for order_id in (cancelled, planning):
        user_id = await _user_id(session, order_id)
        await grant_credits(session, user_id, PRICE)
        await session.commit()
        outcome = await pay_order(session, user_id, order_id)
        assert outcome.kind is ChargeOutcomeKind.NOT_PAYABLE
        assert await _assert_ledger(session, user_id) == PRICE


async def test_order_without_price_is_not_payable(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    await session.execute(update(Order).where(Order.id == order_id).values(price=None))
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE)
    await session.commit()

    outcome = await pay_order(session, user_id, order_id)

    assert outcome == ChargeOutcome(ChargeOutcomeKind.NOT_PAYABLE, order_id)
    assert f"no price: order_id={order_id}" in caplog.text
    assert await _assert_ledger(session, user_id) == PRICE


async def test_failure_after_charge_rolls_everything_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    await grant_credits(session, user_id, PRICE)
    await session.commit()
    real_change_status = service.change_status

    async def broken(session: AsyncSession, order_id: int, target: S, **kwargs: Any) -> Order:
        if target is S.GENERATING:
            raise RuntimeError("connection lost")
        return await real_change_status(session, order_id, target, **kwargs)

    monkeypatch.setattr(service, "change_status", broken)
    with pytest.raises(RuntimeError):
        await pay_order(session, user_id, order_id)
    await session.rollback()

    assert (await _fresh_order(session, order_id)).status is S.PAYMENT_PENDING
    assert await _transactions(session, user_id) == [(PRICE, "bonus")]
    assert await _assert_ledger(session, user_id) == PRICE


async def test_paid_and_generating_unreachable_without_charge(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)

    with pytest.raises(OrderNotPaidError):
        await orders.change_status(session, order_id, S.PAID)
    with pytest.raises(InvalidStatusTransitionError):
        await orders.change_status(session, order_id, S.GENERATING)
    await session.rollback()

    assert (await _fresh_order(session, order_id)).status is S.PAYMENT_PENDING
    assert order_id not in await find_orders_for_generation(session, limit=10)


async def test_paid_guard_holds_even_if_status_set_directly(session: AsyncSession) -> None:
    """Статус выставлен в обход сервиса: PAID всё равно требует списания."""
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    await session.execute(
        text("UPDATE orders SET status = 'payment_pending' WHERE id = :id"), {"id": order_id}
    )

    with pytest.raises(OrderNotPaidError):
        await orders.change_status(session, order_id, S.PAID)


async def test_payable_order(session: AsyncSession) -> None:
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    assert (await payable_order(session, user_id)) is not None

    await grant_credits(session, user_id, PRICE)
    await pay_order(session, user_id, order_id)

    assert await payable_order(session, user_id) is None


# --- Сквозной путь --------------------------------------------------------------------


async def test_topup_pay_generate_complete_deliver(session: AsyncSession, tmp_path: Path) -> None:
    """Stars → кредиты → оплата заказа → разделы → DOCX → доставка."""
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    package = next(p for p in CREDIT_PACKAGES if p.credits >= PRICE)
    payment = await _topup(session, user_id, package)
    assert (await confirm_topup(session, _notification(payment))).kind is TopupOutcomeKind.CREDITED

    assert (await pay_order(session, user_id, order_id)).kind is ChargeOutcomeKind.PAID
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        assert (await generate_next_section(session, llm, order_id)).kind is ReportOutcomeKind.SAVED
    storage = LocalFileStorage(tmp_path)
    completed = await complete_report(session, storage, PracticeReportDocxRenderer(), order_id)
    assert completed.kind is CompletionOutcomeKind.COMPLETED
    sender = RecordingSender()
    assert (await deliver_report(session, storage, sender, order_id)).kind is (
        DeliveryOutcomeKind.DELIVERED
    )

    assert [chat for chat, _, _ in sender.calls] == [TG_ID]
    assert await _transactions(session, user_id) == [
        (package.credits, "topup"),
        (-PRICE, "order_charge"),
    ]
    assert await _assert_ledger(session, user_id) == package.credits - PRICE


async def test_logs_have_no_telegram_id(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    order_id = await payment_pending_order(session, tg_id=TG_ID)
    user_id = await _user_id(session, order_id)
    payment = await _topup(session, user_id)
    await confirm_topup(session, _notification(payment, payer=TG_ID + 5))
    await confirm_topup(session, _notification(payment))
    await grant_credits(session, user_id, PRICE)
    await pay_order(session, user_id, order_id)

    assert "Credits topped up" in caplog.text and "Order paid" in caplog.text
    assert str(TG_ID) not in caplog.text
    assert str(TG_ID + 5) not in caplog.text
