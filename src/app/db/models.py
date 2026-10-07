from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.domain.enums import (
    DocType,
    FileKind,
    LLMCallStatus,
    MaterialFailureReason,
    MaterialProcessingStatus,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    TextTruncationReason,
    TransactionReason,
    VersionStatus,
)
from app.domain.order_flow import FAILABLE_STATUSES, INACTIVE_ORDER_STATUSES


def _pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Native PostgreSQL enum, хранящий значения (``"draft"``), а не имена (``"DRAFT"``)."""
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("balance >= 0", name="balance_non_negative"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(255))
    language_code: Mapped[str | None] = mapped_column(String(16))
    # Денормализованный кэш суммы balance_transactions.amount пользователя.
    # Источник истины — журнал balance_transactions; кэш меняется только
    # в той же транзакции, что и запись в журнал (services.billing). Единицы — кредиты.
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def _active_order_predicate() -> str:
    inactive = ", ".join(f"'{status.value}'" for status in sorted(INACTIVE_ORDER_STATUSES))
    return f"status NOT IN ({inactive})"


def _step_running_predicate() -> str:
    # Список продублирован в миграции 0006: при изменении FAILABLE_STATUSES нужна миграция.
    steps = ", ".join(f"'{status.value}'" for status in sorted(FAILABLE_STATUSES))
    return f"step_started_at IS NULL OR status IN ({steps})"


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        # Не больше одного активного заказа на пользователя — гарантия на уровне БД.
        Index(
            "uq_orders_user_id_active",
            "user_id",
            unique=True,
            postgresql_where=text(_active_order_predicate()),
        ),
        CheckConstraint(
            "(status = 'failed') = (failed_from_status IS NOT NULL)",
            name="failed_from_status_consistent",
        ),
        CheckConstraint("step_attempts >= 0", name="step_attempts_non_negative"),
        CheckConstraint(_step_running_predicate(), name="step_started_at_consistent"),
        CheckConstraint("price IS NULL OR price > 0", name="price_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Тип заказа: определяет схему ТЗ и пайплайн генерации.
    doc_type: Mapped[DocType] = mapped_column(_pg_enum(DocType, "doc_type"))
    status: Mapped[OrderStatus] = mapped_column(
        _pg_enum(OrderStatus, "order_status"), default=OrderStatus.NEW, index=True
    )
    # Шаг, на котором произошёл сбой: повтор из FAILED возможен только в него.
    failed_from_status: Mapped[OrderStatus | None] = mapped_column(
        _pg_enum(OrderStatus, "order_status")
    )
    # Цена в кредитах: фиксируется при переходе в PAYMENT_PENDING и больше не меняется.
    price: Mapped[int | None] = mapped_column(Integer)
    # Прогресс шагов пайплайна — для возобновления генерации после сбоя.
    pipeline_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Автоматический шаг (анализ материалов, позже план и генерация): сколько раз его
    # брали в работу с момента входа в шаг и когда началась текущая попытка (NULL —
    # попытка не идёт). Сбрасываются при смене статуса — см. services.orders.change_status.
    step_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    step_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _OrderVersionMixin(TimestampMixin):
    """Общие поля версии ТЗ/плана.

    Черновик (draft) редактируется, подтверждённая (confirmed) версия неизменна:
    триггер БД запрещает UPDATE и DELETE подтверждённых строк (миграция 0002).
    Изменения после подтверждения — только новой версией с номером на 1 больше.
    """

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[VersionStatus] = mapped_column(
        _pg_enum(VersionStatus, "version_status"), default=VersionStatus.DRAFT
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _version_table_args(table: str) -> tuple[Any, ...]:
    return (
        UniqueConstraint("order_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status = 'confirmed') = (confirmed_at IS NOT NULL)",
            name="confirmed_at_consistent",
        ),
        # Не больше одного черновика на заказ.
        Index(
            f"uq_{table}_order_id_draft",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )


class OrderRequirementsVersion(_OrderVersionMixin, Base):
    """Версия ТЗ заказа. content валидируется схемой из REQUIREMENTS_SCHEMAS."""

    __tablename__ = "order_requirements_versions"
    __table_args__ = _version_table_args(__tablename__)


class OrderPlanVersion(_OrderVersionMixin, Base):
    """Версия плана заказа. content валидируется схемой PlanContent."""

    __tablename__ = "order_plan_versions"
    __table_args__ = _version_table_args(__tablename__)

    # Версия ТЗ, на основе которой построен план.
    requirements_version_id: Mapped[int] = mapped_column(
        # Имя по соглашению длиннее лимита PostgreSQL в 63 символа.
        ForeignKey("order_requirements_versions.id", name="fk_order_plan_versions_requirements")
    )


class OrderReportSection(Base):
    """Сгенерированный текст раздела подтверждённого плана: строка = готовый раздел.

    Строка только вставляется. Статуса нет: попытка генерации живёт в полях шага
    заказа, ошибки — в llm_calls, объём раздела — в плане. Названия разделов и
    подразделов — в плане, здесь только текст.
    """

    __tablename__ = "order_report_sections"
    __table_args__ = (
        # Один текст на раздел плана: повторное сохранение раздела невозможно.
        UniqueConstraint("plan_version_id", "section_index"),
        CheckConstraint("section_index >= 0", name="section_index_non_negative"),
        CheckConstraint("char_count > 0", name="char_count_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("order_plan_versions.id"))
    # Номер раздела в plan.content["sections"], с нуля.
    section_index: Mapped[int] = mapped_column(Integer)
    # {"paragraphs": [...], "subsections": [{"paragraphs": [...]}]}
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Символы с пробелами во всех абзацах раздела и подразделов.
    char_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderReportFile(Base):
    """Готовый файл отчёта в FileStorage: строка = результат версии плана.

    Строка только вставляется — в той же транзакции, что и переход заказа в
    COMPLETED. Выходной файл, а не материал: order_files не используется.
    """

    __tablename__ = "order_report_files"
    __table_args__ = (
        # Один результат на версию плана: второй файл того же плана невозможен.
        UniqueConstraint("plan_version_id"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("order_plan_versions.id"))
    storage_key: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # SHA-256 байтов файла, 64 шестнадцатеричные цифры в нижнем регистре.
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderReportDelivery(Base):
    """Фоновая доставка готового файла отчёта пользователю: строка = файл.

    Отдельно от order_report_files: строки файлов только вставляются. Состояние —
    три nullable-колонки: started_at (идёт попытка), delivered_at (доставлено),
    error (код постоянной ошибки). Ручное скачивание из «Мои заказы» строку не меняет.
    """

    __tablename__ = "order_report_deliveries"
    __table_args__ = (
        # Одна доставка на файл; UNIQUE обслуживает и поиск по файлу.
        UniqueConstraint("report_file_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("delivered_at IS NULL OR error IS NULL", name="delivered_xor_error"),
        CheckConstraint(
            "started_at IS NULL OR (delivered_at IS NULL AND error IS NULL)",
            name="started_only_while_pending",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    report_file_id: Mapped[int] = mapped_column(
        ForeignKey("order_report_files.id", ondelete="CASCADE")
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderMaterial(TimestampMixin, Base):
    """Материал заказа, присланный пользователем (задание, методичка, пример отчёта).

    Метаданные (имя, размер, ключ) после создания не меняются; меняется только
    статус обработки. Материалы не удаляются: проблемный файл заменяется новым.
    """

    __tablename__ = "order_materials"
    __table_args__ = (
        # Порядок загрузки внутри заказа; индекс обслуживает выборку материалов заказа.
        UniqueConstraint("order_id", "upload_order"),
        CheckConstraint("upload_order >= 1", name="upload_order_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint(
            "(processing_status = 'failed') = (processing_error IS NOT NULL)",
            name="processing_error_consistent",
        ),
        CheckConstraint(
            "(processing_status = 'failed') = (failure_reason IS NOT NULL)",
            name="processing_failure_reason_consistent",
        ),
        CheckConstraint(
            "(processing_status = 'processing') = (processing_started_at IS NOT NULL)",
            name="processing_started_at_consistent",
        ),
        CheckConstraint("processing_attempts >= 0", name="processing_attempts_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    upload_order: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(255))
    # Нормализованное расширение без точки: "pdf", "docx", "txt".
    extension: Mapped[str] = mapped_column(String(16))
    # MIME, заявленный клиентом; содержимым файла не подтверждён.
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # Ключ файла в FileStorage; файл сохраняется в той же транзакции, что и строка.
    storage_key: Mapped[str] = mapped_column(String(512))
    processing_status: Mapped[MaterialProcessingStatus] = mapped_column(
        _pg_enum(MaterialProcessingStatus, "material_processing_status"),
        default=MaterialProcessingStatus.RECEIVED,
    )
    # Технические подробности сбоя — для логов и разработчика, не для пользователя.
    processing_error: Mapped[str | None] = mapped_column(Text)
    # Машиночитаемая причина сбоя; заполнена тогда и только тогда, когда статус FAILED.
    failure_reason: Mapped[MaterialFailureReason | None] = mapped_column(
        _pg_enum(MaterialFailureReason, "material_failure_reason")
    )
    # Сколько раз материал брали в обработку; ограничивает повторы.
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Начало текущей попытки (только в PROCESSING): по нему находится зависшая обработка.
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Вне класса: внутри OrderMaterialText имя text занято колонкой.
_EMPTY_JSONB_ARRAY = text("'[]'::jsonb")


class OrderMaterialText(Base):
    """Текст, извлечённый из материала; есть только у материала в статусе PROCESSED.

    Отдельная таблица: списки материалов не загружают тексты до 500 000 символов.
    """

    __tablename__ = "order_material_texts"
    __table_args__ = (
        CheckConstraint("char_count = char_length(text)", name="char_count_matches"),
        CheckConstraint(
            "(page_count IS NULL) = (pages_processed IS NULL)", name="page_counters_together"
        ),
        CheckConstraint(
            "pages_processed >= 0 AND pages_processed <= page_count", name="pages_processed_range"
        ),
    )

    material_id: Mapped[int] = mapped_column(
        ForeignKey("order_materials.id", ondelete="CASCADE"), primary_key=True
    )
    # Нормализованный текст (NFC, переводы строк LF, страницы PDF разделены символом FF).
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    # Только для PDF: всего страниц и сколько из них обработано.
    page_count: Mapped[int | None] = mapped_column(Integer)
    pages_processed: Mapped[int | None] = mapped_column(Integer)
    # Сохранена только часть текста — почему.
    truncation_reason: Mapped[TextTruncationReason | None] = mapped_column(
        _pg_enum(TextTruncationReason, "text_truncation_reason")
    )
    # [{"code": ..., "kind": "fact" | "heuristic", "pages": [...]}]
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=_EMPTY_JSONB_ARRAY
    )
    # Библиотека и версия, например "pypdfium2 5.13.0 / pdfium 153.0.7999.0".
    extractor: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrderFile(Base):
    """Пока не используется в коде."""

    __tablename__ = "order_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    kind: Mapped[FileKind] = mapped_column(_pg_enum(FileKind, "file_kind"))
    storage_key: Mapped[str] = mapped_column(String(512))
    tg_file_id: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _reason_consistent_predicate() -> str:
    # Продублировано в миграции 0010: при изменении нужна миграция.
    return (
        "(reason <> 'topup' OR (amount > 0 AND payment_id IS NOT NULL AND order_id IS NULL))"
        " AND (reason <> 'order_charge'"
        " OR (amount < 0 AND order_id IS NOT NULL AND payment_id IS NULL))"
        " AND (reason <> 'order_refund' OR (amount > 0 AND order_id IS NOT NULL))"
    )


class Payment(TimestampMixin, Base):
    """Покупка кредитов за реальные деньги: строка = выставленный счёт.

    Создаётся в PENDING при выставлении счёта (payload счёта ссылается на id строки) и
    переводится в SUCCEEDED подтверждением провайдера — в той же транзакции, что и
    зачисление кредитов (services.billing.confirm_topup). С заказами не связана.
    """

    __tablename__ = "payments"
    __table_args__ = (
        # Идемпотентность: повторное уведомление провайдера не создаст второй платёж.
        UniqueConstraint("provider", "provider_payment_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("credits > 0", name="credits_positive"),
        # Подтверждённый платёж всегда хранит идентификатор провайдера (нужен для возврата).
        CheckConstraint(
            "status <> 'succeeded' OR provider_payment_id IS NOT NULL",
            name="succeeded_has_provider_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[PaymentProvider] = mapped_column(_pg_enum(PaymentProvider, "payment_provider"))
    # Telegram Stars: telegram_payment_charge_id (для refundStarPayment).
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    # Реальные деньги в минимальных единицах валюты провайдера (Stars — целые XTR).
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    # Сколько кредитов даёт этот платёж: фиксируется при выставлении счёта.
    credits: Mapped[int] = mapped_column(Integer)
    status: Mapped[PaymentStatus] = mapped_column(
        _pg_enum(PaymentStatus, "payment_status"), default=PaymentStatus.PENDING
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class BalanceTransaction(Base):
    """Журнал операций с балансом — источник истины. Записи только добавляются."""

    __tablename__ = "balance_transactions"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        # Знак суммы и ссылки определяются причиной: пополнение — только по платежу,
        # списание и возврат — только по заказу.
        CheckConstraint(_reason_consistent_predicate(), name="reason_consistent"),
        # Не больше одного списания и одного возврата на заказ.
        Index(
            "uq_balance_transactions_order_id_reason",
            "order_id",
            "reason",
            unique=True,
            postgresql_where=text("order_id IS NOT NULL"),
        ),
        # Не больше одного зачисления на платёж.
        Index(
            "uq_balance_transactions_payment_id",
            "payment_id",
            unique=True,
            postgresql_where=text("payment_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Положительное — зачисление, отрицательное — списание. Единицы — кредиты.
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[TransactionReason] = mapped_column(
        _pg_enum(TransactionReason, "transaction_reason")
    )
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    step: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Прочитано из кэша промпта и записано в него (у Anthropic — не в input_tokens).
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[LLMCallStatus] = mapped_column(_pg_enum(LLMCallStatus, "llm_call_status"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
