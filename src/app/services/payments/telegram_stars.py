"""Telegram Stars (валюта XTR): счёт — сообщение бота, подтверждение — апдейт.

Поток: sendInvoice (payload = ссылка на payments) → pre_checkout_query →
answerPreCheckoutQuery → successful_payment (telegram_payment_charge_id) →
services.billing.confirm_topup. Для сверки (services.payment_reconciliation) те же
данные дают транзакции getStarTransactions: StarTransaction.id совпадает с
telegram_payment_charge_id, а source (TransactionPartnerUser) несёт invoice_payload
и плательщика.

Модуль не зависит от aiogram: отправку счёта и чтение транзакций выполняют функции
бот-слоя (app.bot.stars), переданные сюда.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.enums import PaymentProvider, PaymentStatus
from app.services.payments.base import Invoice, PaymentNotification

STARS_CURRENCY = "XTR"
# TransactionPartnerUser.transaction_type платежа по счёту.
INVOICE_PAYMENT = "invoice_payment"

# (chat_id, payload, title, description, amount в Stars)
InvoiceSender = Callable[[int, str, str, str, int], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StarTransactionRecord:
    """Входящая транзакция Stars в общем виде (из getStarTransactions)."""

    # StarTransaction.id == telegram_payment_charge_id для оплаты счёта.
    id: str
    amount: int
    date: datetime
    # Только для оплаты счёта пользователем; иначе None.
    transaction_type: str | None = None
    invoice_payload: str | None = None
    payer_tg_id: int | None = None
    # Доли звезды: у оплаты счёта в целых Stars должно быть 0 или None.
    nanostar_amount: int | None = None


# (offset, limit) → транзакции бота в порядке getStarTransactions
TransactionsFetcher = Callable[[int, int], Awaitable[list[StarTransactionRecord]]]


class TelegramStarsGateway:
    provider = PaymentProvider.TELEGRAM_STARS
    currency = STARS_CURRENCY

    def __init__(self, send_invoice: InvoiceSender) -> None:
        self._send_invoice = send_invoice

    async def create_invoice(
        self, *, chat_id: int, payload: str, amount: int, title: str, description: str
    ) -> Invoice:
        await self._send_invoice(chat_id, payload, title, description, amount)
        # Идентификатор платежа Telegram сообщит только в successful_payment.
        return Invoice(provider_payment_id=None)

    async def parse_notification(self, raw: dict[str, Any]) -> PaymentNotification:
        """raw — поля SuccessfulPayment и ``payer_tg_id`` (Message.from_user.id).

        Подлинность гарантирует Telegram: апдейт пришёл боту по его токену.
        """
        charge_id = str(raw["telegram_payment_charge_id"])
        return PaymentNotification(
            provider_payment_id=charge_id,
            status=PaymentStatus.SUCCEEDED,
            amount=int(raw["total_amount"]),
            currency=str(raw["currency"]),
            invoice_payload=str(raw["invoice_payload"]),
            payer_tg_id=int(raw["payer_tg_id"]),
            raw={
                "source": "successful_payment",
                "telegram_payment_charge_id": charge_id,
                "total_amount": int(raw["total_amount"]),
                "currency": str(raw["currency"]),
            },
        )

    async def refund(self, *, provider_payment_id: str, amount: int) -> None:
        raise NotImplementedError("Возвраты Stars — Stage 3.7 (refundStarPayment)")


def notification_from_transaction(record: StarTransactionRecord) -> PaymentNotification | None:
    """Подтверждение из транзакции getStarTransactions; не оплата счёта — None."""
    if (
        record.transaction_type != INVOICE_PAYMENT
        or record.invoice_payload is None
        or record.payer_tg_id is None
    ):
        return None
    if record.nanostar_amount:
        # Оплата счёта — всегда целые звёзды; дробная сумма не наш платёж.
        return None
    return PaymentNotification(
        provider_payment_id=record.id,
        status=PaymentStatus.SUCCEEDED,
        amount=record.amount,
        currency=STARS_CURRENCY,
        invoice_payload=record.invoice_payload,
        payer_tg_id=record.payer_tg_id,
        raw={
            "source": "reconciliation",
            "telegram_payment_charge_id": record.id,
            "total_amount": record.amount,
            "currency": STARS_CURRENCY,
        },
    )
