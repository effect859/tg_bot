"""Кредиты: пакеты пополнения и цены документов. Модуль чистый (без БД).

Внутренняя единица — кредиты. Реальные деньги — только в пакете (Stars, XTR) и в
payments.amount; баланс, журнал и цена заказа — в кредитах.

ВНИМАНИЕ: значения пакетов и цен ниже ВРЕМЕННЫЕ. Экономика не утверждена: значения
нужны только для работы механизма и должны быть заменены утверждёнными до выпуска.
Изменение цены не влияет на уже оформленные заказы (orders.price фиксируется при
переходе в PAYMENT_PENDING) и на уже выставленные счета (payments.credits и amount
фиксируются при создании счёта).
"""

from dataclasses import dataclass
from datetime import timedelta

from app.domain.enums import DocType


@dataclass(frozen=True, slots=True)
class CreditPackage:
    # Идентификатор в callback-данных кнопки: короткий и стабильный.
    id: str
    credits: int
    # Цена в Telegram Stars (целое число, валюта XTR).
    stars: int


# ВРЕМЕННЫЕ значения (см. docstring модуля): заменить утверждёнными.
CREDIT_PACKAGES: tuple[CreditPackage, ...] = (
    CreditPackage(id="c100", credits=100, stars=100),
    CreditPackage(id="c500", credits=500, stars=500),
    CreditPackage(id="c1000", credits=1000, stars=1000),
)

# ВРЕМЕННЫЕ значения (см. docstring модуля): цена документа в кредитах.
ORDER_PRICES: dict[DocType, int] = {
    DocType.PRACTICE_REPORT: 500,
}

# Счёт старше этого не принимается к оплате (pre_checkout): старые счета в чате не
# оплачиваются по устаревшим условиям, а сверка платежей ищет их в ограниченном окне.
INVOICE_TTL = timedelta(hours=24)


def credit_package(package_id: str) -> CreditPackage | None:
    for package in CREDIT_PACKAGES:
        if package.id == package_id:
            return package
    return None


def order_price(doc_type: DocType) -> int:
    """Текущая цена документа в кредитах. Raises KeyError для неизвестного типа."""
    return ORDER_PRICES[doc_type]
