"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

doc_type = postgresql.ENUM("practice_report", name="doc_type", create_type=False)
order_status = postgresql.ENUM(
    "draft",
    "collecting",
    "outline_ready",
    "awaiting_payment",
    "paid",
    "queued",
    "generating",
    "rendering",
    "done",
    "failed",
    "refunded",
    name="order_status",
    create_type=False,
)
file_kind = postgresql.ENUM("input", "output", name="file_kind", create_type=False)
payment_provider = postgresql.ENUM(
    "telegram_stars", "yookassa", name="payment_provider", create_type=False
)
payment_status = postgresql.ENUM(
    "pending", "succeeded", "canceled", "refunded", name="payment_status", create_type=False
)
transaction_reason = postgresql.ENUM(
    "topup",
    "order_charge",
    "order_refund",
    "bonus",
    "admin_adjust",
    name="transaction_reason",
    create_type=False,
)
llm_call_status = postgresql.ENUM("success", "error", name="llm_call_status", create_type=False)

ENUMS = (
    doc_type,
    order_status,
    file_kind,
    payment_provider,
    payment_status,
    transaction_reason,
    llm_call_status,
)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=False)

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("language_code", sa.String(length=16), nullable=True),
        sa.Column("balance", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), nullable=True),
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_blocked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint("balance >= 0", name=op.f("ck_users_balance_non_negative")),
        sa.ForeignKeyConstraint(
            ["referrer_id"],
            ["users.id"],
            name=op.f("fk_users_referrer_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tg_id", name=op.f("uq_users_tg_id")),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("doc_type", doc_type, nullable=False),
        sa.Column("status", order_status, nullable=False),
        sa.Column("topic", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("outline", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("pipeline_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_orders_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_status"), "orders", ["status"])
    op.create_index("ix_orders_user_id_created_at", "orders", ["user_id", "created_at"])

    op.create_table(
        "order_files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", file_kind, nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("tg_file_id", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_files_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_files")),
    )
    op.create_index(op.f("ix_order_files_order_id"), "order_files", ["order_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", payment_provider, nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_payments_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint(
            "provider",
            "provider_payment_id",
            name=op.f("uq_payments_provider_provider_payment_id"),
        ),
    )
    op.create_index(op.f("ix_payments_user_id"), "payments", ["user_id"])

    op.create_table(
        "balance_transactions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", transaction_reason, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("payment_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_balance_transactions_amount_non_zero")),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_balance_transactions_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_balance_transactions_payment_id_payments"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_balance_transactions_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_transactions")),
    )
    op.create_index(op.f("ix_balance_transactions_user_id"), "balance_transactions", ["user_id"])
    op.create_index(
        "uq_balance_transactions_order_id_reason",
        "balance_transactions",
        ["order_id", "reason"],
        unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", llm_call_status, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_llm_calls_order_id_orders"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
    )
    op.create_index(op.f("ix_llm_calls_order_id"), "llm_calls", ["order_id"])


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("balance_transactions")
    op.drop_table("payments")
    op.drop_table("order_files")
    op.drop_table("orders")
    op.drop_table("users")

    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=False)
