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


class MaterialFailureReason(StrEnum):
    """Почему материал в статусе FAILED. Какие причины допускают повтор — в app.domain.materials.

    Подробности для разработчика — в OrderMaterial.processing_error, пользователю не показываются.
    """

    # Файла нет в хранилище: нарушение целостности, а не ошибка извлечения.
    FILE_MISSING = "file_missing"
    # Хранилище не смогло прочитать файл.
    STORAGE_ERROR = "storage_error"
    CORRUPTED = "corrupted"
    # Нужен пароль или неподдерживаемая защита.
    ENCRYPTED = "encrypted"
    # PDF открылся, но в нём 0 страниц.
    EMPTY_DOCUMENT = "empty_document"
    # Конструкция, которую мы сознательно не разбираем (DOCTYPE в DOCX, метод сжатия ZIP).
    UNSUPPORTED = "unsupported"
    # Превышен ресурсный лимит до получения текста (размер частей ZIP, лимит парсера, память).
    LIMIT_EXCEEDED = "limit_exceeded"
    # Дочерний процесс не уложился в жёсткий тайм-аут и снят.
    TIMEOUT = "timeout"
    # Дочерний процесс завершился аварийно, без результата (сигнал, код выхода).
    EXTRACTOR_CRASH = "extractor_crash"
    # Неожиданное исключение в извлечении или некорректный ответ дочернего процесса.
    EXTRACTOR_ERROR = "extractor_error"
    # Дочерний процесс не удалось запустить.
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
    # Обработка зависла в PROCESSING (например, бот перезапустился посреди работы).
    INTERRUPTED = "interrupted"
    # Результат не удалось сохранить из-за временной или нераспознанной ошибки БД.
    DATABASE_ERROR = "database_error"
    # БД детерминированно отвергла результат (ограничения, некорректные данные, лимиты).
    RESULT_REJECTED = "result_rejected"


class TextTruncationReason(StrEnum):
    """Почему сохранена только часть текста материала."""

    PAGE_LIMIT = "page_limit"
    CHAR_LIMIT = "char_limit"
    TIME_BUDGET = "time_budget"


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
