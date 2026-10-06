"""report files: rendered report file of a confirmed plan version

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06

order_report_files — готовый файл отчёта в FileStorage: ключ, размер и SHA-256.
Строка вставляется в той же транзакции, что и переход заказа в COMPLETED, и больше
не меняется. Один файл на версию плана (уникальность plan_version_id).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_report_files",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_version_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_order_report_files_size_positive")),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_order_report_files_sha256_hex")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_report_files_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["order_plan_versions.id"],
            name=op.f("fk_order_report_files_plan_version_id_order_plan_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_report_files")),
        sa.UniqueConstraint("plan_version_id", name=op.f("uq_order_report_files_plan_version_id")),
    )
    op.create_index(op.f("ix_order_report_files_order_id"), "order_report_files", ["order_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_order_report_files_order_id"), table_name="order_report_files")
    op.drop_table("order_report_files")
