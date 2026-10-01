"""material texts: extracted text of order materials and processing bookkeeping

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

material_failure_reason = postgresql.ENUM(
    "file_missing",
    "storage_error",
    "corrupted",
    "encrypted",
    "empty_document",
    "unsupported",
    "limit_exceeded",
    "timeout",
    "extractor_crash",
    "extractor_error",
    "extractor_unavailable",
    "interrupted",
    name="material_failure_reason",
    create_type=False,
)
text_truncation_reason = postgresql.ENUM(
    "page_limit",
    "char_limit",
    "time_budget",
    name="text_truncation_reason",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    material_failure_reason.create(bind, checkfirst=False)
    text_truncation_reason.create(bind, checkfirst=False)

    op.add_column(
        "order_materials", sa.Column("failure_reason", material_failure_reason, nullable=True)
    )
    op.add_column(
        "order_materials",
        sa.Column("processing_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "order_materials",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Строки, созданные до этой миграции: у сбоев причина неизвестна, у PROCESSING
    # момент начала — последнее изменение строки (такая обработка будет признана
    # прерванной). Взятые в обработку считаются одной попыткой.
    op.execute(
        "UPDATE order_materials SET failure_reason = 'extractor_error' "
        "WHERE processing_status = 'failed'"
    )
    op.execute(
        "UPDATE order_materials SET processing_started_at = updated_at "
        "WHERE processing_status = 'processing'"
    )
    op.execute(
        "UPDATE order_materials SET processing_attempts = 1 WHERE processing_status <> 'received'"
    )
    op.create_check_constraint(
        op.f("ck_order_materials_processing_failure_reason_consistent"),
        "order_materials",
        "(processing_status = 'failed') = (failure_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_order_materials_processing_started_at_consistent"),
        "order_materials",
        "(processing_status = 'processing') = (processing_started_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_order_materials_processing_attempts_non_negative"),
        "order_materials",
        "processing_attempts >= 0",
    )

    op.create_table(
        "order_material_texts",
        sa.Column("material_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("pages_processed", sa.Integer(), nullable=True),
        sa.Column("truncation_reason", text_truncation_reason, nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("extractor", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "char_count = char_length(text)",
            name=op.f("ck_order_material_texts_char_count_matches"),
        ),
        sa.CheckConstraint(
            "(page_count IS NULL) = (pages_processed IS NULL)",
            name=op.f("ck_order_material_texts_page_counters_together"),
        ),
        sa.CheckConstraint(
            "pages_processed >= 0 AND pages_processed <= page_count",
            name=op.f("ck_order_material_texts_pages_processed_range"),
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["order_materials.id"],
            name=op.f("fk_order_material_texts_material_id_order_materials"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("material_id", name=op.f("pk_order_material_texts")),
    )


def downgrade() -> None:
    op.drop_table("order_material_texts")
    op.drop_constraint(
        op.f("ck_order_materials_processing_attempts_non_negative"), "order_materials"
    )
    op.drop_constraint(
        op.f("ck_order_materials_processing_started_at_consistent"), "order_materials"
    )
    op.drop_constraint(
        op.f("ck_order_materials_processing_failure_reason_consistent"), "order_materials"
    )
    op.drop_column("order_materials", "processing_started_at")
    op.drop_column("order_materials", "processing_attempts")
    op.drop_column("order_materials", "failure_reason")
    text_truncation_reason.drop(op.get_bind(), checkfirst=False)
    material_failure_reason.drop(op.get_bind(), checkfirst=False)
