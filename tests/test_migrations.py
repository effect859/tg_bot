import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  — регистрирует модели в Base.metadata
from app.db.base import Base
from app.domain.enums import MaterialFailureReason
from conftest import COMMITTED_TG_ID_BASE, make_alembic_config


def _diff(connection: Connection) -> list:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(context, Base.metadata)


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    """Аналог `alembic check`: модели и схема после миграций не расходятся."""
    async with engine.connect() as connection:
        diff = await connection.run_sync(_diff)

    assert diff == []


# --- 0005: причины сбоя сохранения результата -------------------------------------------------

# Синхронный тест: alembic/env.py сам запускает цикл событий (asyncio.run).

ENUM_LABELS = (
    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = 'material_failure_reason' ORDER BY e.enumsortorder"
)
MATERIAL_CHECKS = (
    "SELECT conname FROM pg_constraint "
    "WHERE conrelid = 'order_materials'::regclass AND contype = 'c' ORDER BY conname"
)


async def _query(url: str, sql: str, params: dict[str, object] | None = None) -> list:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql), params or {})
            return list(result) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _seed_failed_materials(url: str) -> None:
    """Пользователь, заказ и два FAILED-материала с новыми причинами (зафиксировано)."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text("INSERT INTO users (tg_id) VALUES (:tg) RETURNING id"),
                {"tg": COMMITTED_TG_ID_BASE + 500},
            )
            order_id = await connection.scalar(
                text(
                    "INSERT INTO orders (user_id, doc_type, status) "
                    "VALUES (:user, 'practice_report', 'waiting_for_files') RETURNING id"
                ),
                {"user": user_id},
            )
            for upload_order, reason in enumerate(("database_error", "result_rejected"), 1):
                await connection.execute(
                    text(
                        "INSERT INTO order_materials (order_id, upload_order, original_filename, "
                        "extension, size_bytes, storage_key, processing_status, "
                        "processing_error, failure_reason, processing_attempts) VALUES "
                        "(:order, :n, 'f.txt', 'txt', 1, :key, 'failed', 'detail', "
                        "CAST(:reason AS material_failure_reason), 1)"
                    ),
                    {
                        "order": order_id,
                        "n": upload_order,
                        "key": f"k{upload_order}",
                        "reason": reason,
                    },
                )
    finally:
        await engine.dispose()


async def _cleanup(url: str) -> None:
    users = "SELECT id FROM users WHERE tg_id >= :base"
    await _query(
        url, f"DELETE FROM orders WHERE user_id IN ({users})", {"base": COMMITTED_TG_ID_BASE}
    )
    await _query(url, "DELETE FROM users WHERE tg_id >= :base", {"base": COMMITTED_TG_ID_BASE})


def _labels(url: str) -> list[str]:
    return [row[0] for row in asyncio.run(_query(url, ENUM_LABELS))]


def _reasons(url: str) -> list[tuple[int, str, str]]:
    sql = (
        "SELECT upload_order, failure_reason::text, processing_error FROM order_materials "
        "WHERE storage_key IN ('k1', 'k2') ORDER BY upload_order"
    )
    return [tuple(row) for row in asyncio.run(_query(url, sql))]


def test_material_recovery_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    all_reasons = [reason.value for reason in MaterialFailureReason]
    checks = [row[0] for row in asyncio.run(_query(database_url, MATERIAL_CHECKS))]
    try:
        assert _labels(database_url) == all_reasons
        asyncio.run(_seed_failed_materials(database_url))

        command.downgrade(config, "0004")
        # Новые причины сведены к совместимым старым; подробности сбоя сохранились.
        assert _labels(database_url) == [
            r for r in all_reasons if r not in ("database_error", "result_rejected")
        ]
        assert _reasons(database_url) == [
            (1, "interrupted", "detail"),
            (2, "extractor_error", "detail"),
        ]
        assert [row[0] for row in asyncio.run(_query(database_url, MATERIAL_CHECKS))] == checks

        command.upgrade(config, "head")
        assert _labels(database_url) == all_reasons
        asyncio.run(
            _query(
                database_url,
                "UPDATE order_materials SET failure_reason = 'result_rejected' "
                "WHERE storage_key = 'k2'",
            )
        )
        assert _reasons(database_url)[1] == (2, "result_rejected", "detail")
        assert [row[0] for row in asyncio.run(_query(database_url, MATERIAL_CHECKS))] == checks
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup(database_url))
