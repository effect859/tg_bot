"""report deliveries: background delivery state of a rendered report file

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07

order_report_deliveries — доставка готового файла пользователю, одна строка на файл
(уникальность report_file_id; удаляется вместе с файлом). Состояние: started_at —
идёт попытка, delivered_at — доставлено, error — код постоянной ошибки.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_report_deliveries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("report_file_id", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_order_report_deliveries_attempts_non_negative")
        ),
        sa.CheckConstraint(
            "delivered_at IS NULL OR error IS NULL",
            name=op.f("ck_order_report_deliveries_delivered_xor_error"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR (delivered_at IS NULL AND error IS NULL)",
            name=op.f("ck_order_report_deliveries_started_only_while_pending"),
        ),
        sa.ForeignKeyConstraint(
            ["report_file_id"],
            ["order_report_files.id"],
            name=op.f("fk_order_report_deliveries_report_file_id_order_report_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_report_deliveries")),
        sa.UniqueConstraint(
            "report_file_id", name=op.f("uq_order_report_deliveries_report_file_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("order_report_deliveries")
