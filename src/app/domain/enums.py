"""Доменные перечисления.

Значения хранятся в PostgreSQL как native enum. Добавление нового значения
(например, нового типа документа) требует миграции с ALTER TYPE ... ADD VALUE.
"""

from enum import StrEnum


class DocType(StrEnum):
    PRACTICE_REPORT = "practice_report"


class OrderStatus(StrEnum):
    """Статусы заказа. Допустимые переходы — в app.domain.order_flow."""

    NEW = "new"
    WAITING_FOR_CONSENT = "waiting_for_consent"
    WAITING_FOR_FILES = "waiting_for_files"
    ANALYZING_MATERIALS = "analyzing_materials"
    WAITING_FOR_MISSING_DATA = "waiting_for_missing_data"
    WAITING_FOR_DATA_CONFIRMATION = "waiting_for_data_confirmation"
    PREPARING_PLAN = "preparing_plan"
    WAITING_FOR_PLAN_CONFIRMATION = "waiting_for_plan_confirmation"
    PAYMENT_PENDING = "payment_pending"
    PAID = "paid"
    GENERATING = "generating"
    COMPLETED = "completed"
    REVISION_REQUESTED = "revision_requested"
    REGENERATING = "regenerating"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VersionStatus(StrEnum):
    """Статус версии ТЗ или плана: черновик редактируется, подтверждённая неизменна."""

    DRAFT = "draft"
    CONFIRMED = "confirmed"


class MaterialProcessingStatus(StrEnum):
    """Статус обработки материала заказа. Переходы — в app.domain.materials."""

    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


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
