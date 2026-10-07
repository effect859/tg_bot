"""Интерфейс платёжного шлюза: покупка кредитов за реальные деньги.

Шлюз только общается с провайдером и приводит его подтверждения к общему виду
(PaymentNotification). Запись в payments, зачисление в balance_transactions и
обновление кэша users.balance делает services.billing в одной транзакции БД —
одинаково для любого провайдера.

Счёт связан с нашей строкой payments через payload (services.billing.topup_payload):
идентификатор провайдера появляется только после оплаты, искать по нему счёт нельзя.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.enums import PaymentProvider, PaymentStatus


@dataclass(frozen=True, slots=True)
class Invoice:
    provider_payment_id: str | None
    # Ссылка на оплату для внешних провайдеров; None для оплаты внутри Telegram.
    payment_url: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentNotification:
    """Подтверждение провайдера в общем виде."""

    provider_payment_id: str
    status: PaymentStatus
    amount: int
    currency: str
    # Payload счёта: ссылка на нашу строку payments.
    invoice_payload: str
    # Telegram id плательщика: зачисление — только владельцу счёта.
    payer_tg_id: int
    # Без персональных данных: только идентификаторы и суммы.
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(Protocol):
    provider: PaymentProvider
    currency: str

    async def create_invoice(
        self, *, chat_id: int, payload: str, amount: int, title: str, description: str
    ) -> Invoice: ...

    async def parse_notification(self, raw: dict[str, Any]) -> PaymentNotification:
        """Проверяет подлинность уведомления провайдера и приводит его к общему виду."""
        ...

    async def refund(self, *, provider_payment_id: str, amount: int) -> None: ...
