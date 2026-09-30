"""Интерфейс платёжного шлюза. Реализации (Telegram Stars, ЮKassa) — на этапе оплаты.

Шлюз только общается с провайдером. Запись в payments, начисление в
balance_transactions и обновление кэша users.balance делает сервис биллинга
в одной транзакции БД.
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
    provider_payment_id: str
    status: PaymentStatus
    amount: int
    currency: str
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(Protocol):
    provider: PaymentProvider

    async def create_invoice(
        self, *, user_id: int, amount: int, currency: str, description: str
    ) -> Invoice: ...

    async def parse_notification(self, raw: dict[str, Any]) -> PaymentNotification:
        """Проверяет подлинность уведомления провайдера и приводит его к общему виду."""
        ...

    async def refund(self, *, provider_payment_id: str, amount: int) -> None: ...
