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
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    TransactionReason,
)


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
    # в той же транзакции, что и запись в журнал. Единицы — копейки.
    balance: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doc_type: Mapped[DocType] = mapped_column(_pg_enum(DocType, "doc_type"))
    status: Mapped[OrderStatus] = mapped_column(
        _pg_enum(OrderStatus, "order_status"), default=OrderStatus.DRAFT, index=True
    )
    topic: Mapped[str | None] = mapped_column(Text)
    # Параметры заказа: вуз, дисциплина, объём, место практики, доп. требования.
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    outline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    price: Mapped[int | None] = mapped_column(Integer)
    # Прогресс шагов пайплайна — для возобновления генерации после сбоя.
    pipeline_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderFile(Base):
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


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        # Идемпотентность: повторное уведомление провайдера не создаст второй платёж.
        UniqueConstraint("provider", "provider_payment_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[PaymentProvider] = mapped_column(_pg_enum(PaymentProvider, "payment_provider"))
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[PaymentStatus] = mapped_column(
        _pg_enum(PaymentStatus, "payment_status"), default=PaymentStatus.PENDING
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class BalanceTransaction(Base):
    """Журнал операций с балансом — источник истины. Записи только добавляются."""

    __tablename__ = "balance_transactions"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="amount_non_zero"),
        # Не больше одного списания и одного возврата на заказ.
        Index(
            "uq_balance_transactions_order_id_reason",
            "order_id",
            "reason",
            unique=True,
            postgresql_where=text("order_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # Положительное — зачисление, отрицательное — списание. Единицы — копейки.
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
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[LLMCallStatus] = mapped_column(_pg_enum(LLMCallStatus, "llm_call_status"))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
