"""report sections: generated text of report sections, prompt cache write accounting

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-04

order_report_sections — текст разделов подтверждённого плана: строка появляется,
когда раздел сгенерирован и проверен, и больше не меняется. Один текст на раздел
плана (уникальность plan_version_id + section_index).

llm_calls.cache_creation_tokens — токены, записанные в кэш промпта: у Anthropic
они не входят в input_tokens. Существующие строки получают 0.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_report_sections",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_version_id", sa.BigInteger(), nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "section_index >= 0", name=op.f("ck_order_report_sections_section_index_non_negative")
        ),
        sa.CheckConstraint(
            "char_count > 0", name=op.f("ck_order_report_sections_char_count_positive")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_report_sections_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["order_plan_versions.id"],
            name=op.f("fk_order_report_sections_plan_version_id_order_plan_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_report_sections")),
        sa.UniqueConstraint(
            "plan_version_id",
            "section_index",
            name=op.f("uq_order_report_sections_plan_version_id_section_index"),
        ),
    )
    op.create_index(
        op.f("ix_order_report_sections_order_id"), "order_report_sections", ["order_id"]
    )
    op.add_column(
        "llm_calls",
        sa.Column(
            "cache_creation_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_calls", "cache_creation_tokens")
    op.drop_index(op.f("ix_order_report_sections_order_id"), table_name="order_report_sections")
    op.drop_table("order_report_sections")
