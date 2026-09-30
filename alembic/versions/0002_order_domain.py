"""order domain: new order statuses, one active order per user, versioned requirements and plan

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ORDER_STATUSES = (
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
)
NEW_ORDER_STATUSES = (
    "new",
    "waiting_for_consent",
    "waiting_for_files",
    "analyzing_materials",
    "waiting_for_missing_data",
    "waiting_for_data_confirmation",
    "preparing_plan",
    "waiting_for_plan_confirmation",
    "payment_pending",
    "paid",
    "generating",
    "completed",
    "revision_requested",
    "regenerating",
    "failed",
    "cancelled",
)

UPGRADE_STATUS_MAP = {
    "draft": "new",
    "collecting": "waiting_for_files",
    "outline_ready": "waiting_for_plan_confirmation",
    "awaiting_payment": "payment_pending",
    "paid": "paid",
    "queued": "paid",
    "generating": "generating",
    "rendering": "generating",
    "done": "completed",
    "failed": "failed",
    "refunded": "cancelled",
}
# Обратное отображение с потерей точности: у старого enum нет части статусов.
DOWNGRADE_STATUS_MAP = {
    "new": "draft",
    "waiting_for_consent": "draft",
    "waiting_for_files": "collecting",
    "analyzing_materials": "collecting",
    "waiting_for_missing_data": "collecting",
    "waiting_for_data_confirmation": "collecting",
    "preparing_plan": "collecting",
    "waiting_for_plan_confirmation": "outline_ready",
    "payment_pending": "awaiting_payment",
    "paid": "paid",
    "generating": "generating",
    "completed": "done",
    "revision_requested": "done",
    "regenerating": "generating",
    "failed": "failed",
    "cancelled": "refunded",
}

version_status = postgresql.ENUM("draft", "confirmed", name="version_status", create_type=False)
order_status = postgresql.ENUM(*NEW_ORDER_STATUSES, name="order_status", create_type=False)

VERSION_TABLES = ("order_requirements_versions", "order_plan_versions")

FORBID_CONFIRMED_CHANGE_FN = """
CREATE FUNCTION forbid_confirmed_version_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'confirmed' THEN
        RAISE EXCEPTION '%: confirmed version % is immutable', TG_TABLE_NAME, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


def _swap_order_status_type(values: Sequence[str], mapping: dict[str, str]) -> None:
    """Заменяет набор значений order_status.

    ALTER TYPE ... ADD VALUE не подходит: новое значение нельзя использовать в той же
    транзакции, а старые значения PostgreSQL удалять из enum не умеет.
    """
    op.execute("ALTER TYPE order_status RENAME TO order_status_old")
    postgresql.ENUM(*values, name="order_status").create(op.get_bind(), checkfirst=False)
    cases = " ".join(f"WHEN '{old}' THEN '{new}'" for old, new in mapping.items())
    op.execute(
        "ALTER TABLE orders ALTER COLUMN status TYPE order_status "
        f"USING (CASE status::text {cases} END)::order_status"
    )
    op.execute("DROP TYPE order_status_old")


def _version_table(name: str, *extra: SchemaItem) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", version_status, nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *extra,
        sa.CheckConstraint("version >= 1", name=op.f(f"ck_{name}_version_positive")),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_at IS NOT NULL)",
            name=op.f(f"ck_{name}_confirmed_at_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f(f"fk_{name}_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{name}")),
        sa.UniqueConstraint("order_id", "version", name=op.f(f"uq_{name}_order_id_version")),
    )
    op.create_index(
        f"uq_{name}_order_id_draft",
        name,
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.execute(
        f"CREATE TRIGGER trg_{name}_immutable_confirmed BEFORE UPDATE OR DELETE ON {name} "
        "FOR EACH ROW EXECUTE FUNCTION forbid_confirmed_version_change()"
    )


def upgrade() -> None:
    _swap_order_status_type(NEW_ORDER_STATUSES, UPGRADE_STATUS_MAP)

    # Данные ТЗ и плана переезжают в версионируемые таблицы.
    op.drop_column("orders", "topic")
    op.drop_column("orders", "params")
    op.drop_column("orders", "outline")
    op.add_column("orders", sa.Column("failed_from_status", order_status, nullable=True))
    op.add_column(
        "orders", sa.Column("consent_given_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("orders", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE orders SET failed_from_status = 'generating' WHERE status = 'failed'")
    op.create_check_constraint(
        op.f("ck_orders_failed_from_status_consistent"),
        "orders",
        "(status = 'failed') = (failed_from_status IS NOT NULL)",
    )
    op.create_index(
        "uq_orders_user_id_active",
        "orders",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('cancelled', 'completed')"),
    )

    version_status.create(op.get_bind(), checkfirst=False)
    op.execute(FORBID_CONFIRMED_CHANGE_FN)
    _version_table("order_requirements_versions")
    _version_table(
        "order_plan_versions",
        sa.Column("requirements_version_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["requirements_version_id"],
            ["order_requirements_versions.id"],
            name="fk_order_plan_versions_requirements",
        ),
    )


def downgrade() -> None:
    for name in reversed(VERSION_TABLES):
        op.drop_table(name)
    op.execute("DROP FUNCTION forbid_confirmed_version_change()")
    version_status.drop(op.get_bind(), checkfirst=False)

    op.drop_index("uq_orders_user_id_active", table_name="orders")
    op.drop_constraint(op.f("ck_orders_failed_from_status_consistent"), "orders", type_="check")
    op.drop_column("orders", "cancelled_at")
    op.drop_column("orders", "consent_given_at")
    op.drop_column("orders", "failed_from_status")
    op.add_column("orders", sa.Column("topic", sa.Text(), nullable=True))
    op.add_column(
        "orders",
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "orders", sa.Column("outline", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )

    _swap_order_status_type(OLD_ORDER_STATUSES, DOWNGRADE_STATUS_MAP)
