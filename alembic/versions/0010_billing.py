"""billing: credits of a payment, ledger integrity, positive order price

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07

Кредиты (Stage 3.6): payments.credits — сколько кредитов даёт счёт (фиксируется при
выставлении); подтверждённый платёж хранит идентификатор провайдера; не больше одного
зачисления на платёж; знак суммы и ссылки проводки согласованы с причиной; цена
заказа (в кредитах) положительна.

До 0010 ни одна строка кода не писала в payments, поэтому NOT NULL у credits
добавляется без значения по умолчанию: непустая таблица — ошибка миграции, а не
выдуманные кредиты.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Продублировано из app.db.models._reason_consistent_predicate.
REASON_CONSISTENT = (
    "(reason <> 'topup' OR (amount > 0 AND payment_id IS NOT NULL AND order_id IS NULL))"
    " AND (reason <> 'order_charge'"
    " OR (amount < 0 AND order_id IS NOT NULL AND payment_id IS NULL))"
    " AND (reason <> 'order_refund' OR (amount > 0 AND order_id IS NOT NULL))"
)


def upgrade() -> None:
    op.add_column("payments", sa.Column("credits", sa.Integer(), nullable=False))
    op.create_check_constraint(op.f("ck_payments_credits_positive"), "payments", "credits > 0")
    op.create_check_constraint(
        op.f("ck_payments_succeeded_has_provider_id"),
        "payments",
        "status <> 'succeeded' OR provider_payment_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_balance_transactions_reason_consistent"), "balance_transactions", REASON_CONSISTENT
    )
    op.create_index(
        "uq_balance_transactions_payment_id",
        "balance_transactions",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )
    op.create_check_constraint(
        op.f("ck_orders_price_positive"), "orders", "price IS NULL OR price > 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_price_positive"), "orders", type_="check")
    op.drop_index("uq_balance_transactions_payment_id", table_name="balance_transactions")
    op.drop_constraint(
        op.f("ck_balance_transactions_reason_consistent"), "balance_transactions", type_="check"
    )
    op.drop_constraint(op.f("ck_payments_succeeded_has_provider_id"), "payments", type_="check")
    op.drop_constraint(op.f("ck_payments_credits_positive"), "payments", type_="check")
    op.drop_column("payments", "credits")
