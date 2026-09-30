"""Доменные перечисления.

Значения хранятся в PostgreSQL как native enum. Добавление нового значения
(например, нового типа документа) требует миграции с ALTER TYPE ... ADD VALUE.
"""

from enum import StrEnum


class DocType(StrEnum):
    PRACTICE_REPORT = "practice_report"


class OrderStatus(StrEnum):
    DRAFT = "draft"
    COLLECTING = "collecting"
    OUTLINE_READY = "outline_ready"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    QUEUED = "queued"
    GENERATING = "generating"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"
    REFUNDED = "refunded"


class FileKind(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class PaymentProvider(StrEnum):
    TELEGRAM_STARS = "telegram_stars"
    YOOKASSA = "yookassa"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    REFUNDED = "refunded"


class TransactionReason(StrEnum):
    TOPUP = "topup"
    ORDER_CHARGE = "order_charge"
    ORDER_REFUND = "order_refund"
    BONUS = "bonus"
    ADMIN_ADJUST = "admin_adjust"


class LLMCallStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
