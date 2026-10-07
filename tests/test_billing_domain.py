"""Кредиты без БД: пакеты и цены, payload счёта, нормализация подтверждений Stars, тексты."""

from datetime import UTC, datetime

import pytest

from app.bot import texts
from app.domain import billing
from app.domain.billing import CREDIT_PACKAGES, INVOICE_TTL, credit_package, order_price
from app.domain.enums import DocType, PaymentProvider, PaymentStatus
from app.services.billing import parse_topup_payload, topup_payload
from app.services.payments.telegram_stars import (
    STARS_CURRENCY,
    StarTransactionRecord,
    TelegramStarsGateway,
    notification_from_transaction,
)

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def test_packages_are_positive_and_unique() -> None:
    assert CREDIT_PACKAGES
    assert len({p.id for p in CREDIT_PACKAGES}) == len(CREDIT_PACKAGES)
    for package in CREDIT_PACKAGES:
        assert package.credits > 0 and package.stars > 0
        assert credit_package(package.id) is package
    assert credit_package("unknown") is None


def test_every_doc_type_has_positive_price() -> None:
    for doc_type in DocType:
        assert order_price(doc_type) > 0
    assert INVOICE_TTL.total_seconds() > 0


def test_values_are_marked_temporary() -> None:
    # Экономика не утверждена: значения — временные, это записано в модуле.
    assert billing.__doc__ is not None and "ВРЕМЕННЫЕ" in billing.__doc__


@pytest.mark.parametrize("payment_id", [1, 42, 10**17])
def test_topup_payload_round_trip(payment_id: int) -> None:
    payload = topup_payload(payment_id)

    assert len(payload.encode()) <= 128  # лимит Telegram
    assert parse_topup_payload(payload) == payment_id


@pytest.mark.parametrize(
    "payload", ["", "topup:", "topup:0", "topup:-1", "topup:1x", "x:1", " topup:1", "topup:01"]
)
def test_bad_topup_payload(payload: str) -> None:
    assert parse_topup_payload(payload) is None


async def test_stars_gateway_parses_successful_payment() -> None:
    gateway = TelegramStarsGateway(send_invoice=_never_send)

    notification = await gateway.parse_notification(
        {
            "telegram_payment_charge_id": "charge-1",
            "total_amount": 500,
            "currency": "XTR",
            "invoice_payload": "topup:7",
            "payer_tg_id": 42,
        }
    )

    assert (notification.provider_payment_id, notification.amount, notification.currency) == (
        "charge-1",
        500,
        "XTR",
    )
    assert (notification.invoice_payload, notification.payer_tg_id) == ("topup:7", 42)
    assert notification.status is PaymentStatus.SUCCEEDED
    # В raw — только идентификаторы и суммы.
    assert set(notification.raw) == {
        "source",
        "telegram_payment_charge_id",
        "total_amount",
        "currency",
    }
    assert gateway.provider is PaymentProvider.TELEGRAM_STARS
    assert gateway.currency == STARS_CURRENCY == "XTR"


async def test_stars_gateway_sends_invoice_and_has_no_refund_yet() -> None:
    sent: list[tuple[object, ...]] = []

    async def send(*args: object) -> None:
        sent.append(args)

    gateway = TelegramStarsGateway(send_invoice=send)

    invoice = await gateway.create_invoice(
        chat_id=5, payload="topup:1", amount=100, title="t", description="d"
    )

    assert sent == [(5, "topup:1", "t", "d", 100)]
    assert invoice.provider_payment_id is None
    with pytest.raises(NotImplementedError):
        await gateway.refund(provider_payment_id="charge-1", amount=100)


def _record(**kwargs: object) -> StarTransactionRecord:
    values: dict[str, object] = {
        "id": "charge-1",
        "amount": 100,
        "date": NOW,
        "transaction_type": "invoice_payment",
        "invoice_payload": "topup:1",
        "payer_tg_id": 42,
    }
    values.update(kwargs)
    return StarTransactionRecord(**values)  # type: ignore[arg-type]


def test_notification_from_invoice_transaction() -> None:
    notification = notification_from_transaction(_record())

    assert notification is not None
    assert (notification.provider_payment_id, notification.amount) == ("charge-1", 100)
    assert (notification.invoice_payload, notification.payer_tg_id) == ("topup:1", 42)
    assert notification.currency == "XTR"
    assert notification.raw["source"] == "reconciliation"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"transaction_type": "paid_media_payment"},
        {"transaction_type": None},
        {"invoice_payload": None},
        {"payer_tg_id": None},
        {"nanostar_amount": 5},
    ],
)
def test_other_transactions_are_not_notifications(kwargs: dict[str, object]) -> None:
    assert notification_from_transaction(_record(**kwargs)) is None


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "0 кредитов"),
        (1, "1 кредит"),
        (2, "2 кредита"),
        (5, "5 кредитов"),
        (11, "11 кредитов"),
        (21, "21 кредит"),
        (104, "104 кредита"),
        (112, "112 кредитов"),
        (500, "500 кредитов"),
    ],
)
def test_credits_plural(amount: int, expected: str) -> None:
    assert texts.credits(amount) == expected


def test_payment_pending_text_depends_on_balance() -> None:
    assert "Оплатить" in texts.payment_pending(7, 500, 500)
    assert "Не хватает 200 кредитов" in texts.payment_pending(7, 500, 300)
    assert texts.pre_checkout_refusal("expired") != texts.PAYMENT_TEMPORARILY_UNAVAILABLE
    assert texts.pre_checkout_refusal("other") == texts.PAYMENT_TEMPORARILY_UNAVAILABLE
    # Номер платежа из Telegram экранируется (parse_mode=HTML).
    assert "&lt;b&gt;" in texts.payment_rejected("<b>")


async def _never_send(*args: object) -> None:  # pragma: no cover
    raise AssertionError("invoice must not be sent")
