"""order materials: files attached to an order and their processing status

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

material_processing_status = postgresql.ENUM(
    "received",
    "processing",
    "processed",
    "failed",
    name="material_processing_status",
    create_type=False,
)


def upgrade() -> None:
    material_processing_status.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "order_materials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("upload_order", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("processing_status", material_processing_status, nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "upload_order >= 1", name=op.f("ck_order_materials_upload_order_positive")
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_order_materials_size_positive")),
        sa.CheckConstraint(
            "(processing_status = 'failed') = (processing_error IS NOT NULL)",
            name=op.f("ck_order_materials_processing_error_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_materials_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_materials")),
        sa.UniqueConstraint(
            "order_id", "upload_order", name=op.f("uq_order_materials_order_id_upload_order")
        ),
    )


def downgrade() -> None:
    op.drop_table("order_materials")
    material_processing_status.drop(op.get_bind(), checkfirst=False)
