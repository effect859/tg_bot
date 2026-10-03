"""order steps: bookkeeping of automatic pipeline steps (attempts, current attempt start)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02

Общие поля для автоматических шагов заказа (анализ материалов, позже план и
генерация): сколько раз шаг брали в работу и когда началась текущая попытка.
Существующие строки получают значения по умолчанию: попыток 0, попытка не идёт.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Автоматические шаги (FAILABLE_STATUSES на момент миграции).
STEP_STATUSES = ("analyzing_materials", "generating", "preparing_plan", "regenerating")


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("step_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("orders", sa.Column("step_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_step_attempts_non_negative"), "orders", "step_attempts >= 0"
    )
    steps = ", ".join(f"'{status}'" for status in STEP_STATUSES)
    op.create_check_constraint(
        op.f("ck_orders_step_started_at_consistent"),
        "orders",
        f"step_started_at IS NULL OR status IN ({steps})",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_step_started_at_consistent"), "orders")
    op.drop_constraint(op.f("ck_orders_step_attempts_non_negative"), "orders")
    op.drop_column("orders", "step_started_at")
    op.drop_column("orders", "step_attempts")
