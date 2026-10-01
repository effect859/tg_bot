"""material recovery: failure reasons for errors while saving the extraction result

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01

Downgrade теряет различие причин: database_error становится interrupted (тоже
повторяемой), result_rejected — extractor_error (тоже окончательной). Подробности
сбоя остаются в processing_error.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_VALUES = ("database_error", "result_rejected")
# Значения material_failure_reason до этой миграции (0004), в том же порядке.
OLD_VALUES = (
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
)
DOWNGRADE_MAPPING = {"database_error": "interrupted", "result_rejected": "extractor_error"}


def upgrade() -> None:
    # ADD VALUE допустим в транзакции (PostgreSQL 12+); новые значения здесь не
    # используются, поэтому ограничение «не в той же транзакции» не мешает.
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE material_failure_reason ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Удалить значение из enum PostgreSQL нельзя: строки переводятся на старые
    # значения, тип пересоздаётся, колонка переводится на него.
    for new, old in DOWNGRADE_MAPPING.items():
        op.execute(
            f"UPDATE order_materials SET failure_reason = '{old}' WHERE failure_reason = '{new}'"
        )
    op.execute("ALTER TYPE material_failure_reason RENAME TO material_failure_reason_old")
    values = ", ".join(f"'{value}'" for value in OLD_VALUES)
    op.execute(f"CREATE TYPE material_failure_reason AS ENUM ({values})")
    op.execute(
        "ALTER TABLE order_materials ALTER COLUMN failure_reason TYPE material_failure_reason "
        "USING failure_reason::text::material_failure_reason"
    )
    op.execute("DROP TYPE material_failure_reason_old")
