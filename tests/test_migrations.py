import asyncio

import pytest
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


# --- 0006: поля автоматического шага заказа ------------------------------------------

ORDER_STEP_COLUMNS = (
    "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = 'orders' AND column_name LIKE 'step_%' ORDER BY column_name"
)
ORDER_STEP_CHECKS = (
    "SELECT conname FROM pg_constraint WHERE conrelid = 'orders'::regclass "
    "AND contype = 'c' AND conname LIKE 'ck_orders_step%' ORDER BY conname"
)


async def _seed_order(url: str, status: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text("INSERT INTO users (tg_id) VALUES (:tg) RETURNING id"),
                {"tg": COMMITTED_TG_ID_BASE + 600},
            )
            await connection.execute(
                text(
                    "INSERT INTO orders (user_id, doc_type, status) "
                    "VALUES (:user, 'practice_report', CAST(:status AS order_status))"
                ),
                {"user": user_id, "status": status},
            )
    finally:
        await engine.dispose()


def test_order_steps_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    try:
        asyncio.run(_seed_order(database_url, "analyzing_materials"))
        columns = asyncio.run(_query(database_url, ORDER_STEP_COLUMNS))
        assert [(c[0], c[1], c[2], c[3]) for c in columns] == [
            ("step_attempts", "integer", "NO", "0"),
            ("step_started_at", "timestamp with time zone", "YES", None),
        ]
        assert [row[0] for row in asyncio.run(_query(database_url, ORDER_STEP_CHECKS))] == [
            "ck_orders_step_attempts_non_negative",
            "ck_orders_step_started_at_consistent",
        ]

        command.downgrade(config, "0005")
        assert asyncio.run(_query(database_url, ORDER_STEP_COLUMNS)) == []
        assert asyncio.run(_query(database_url, ORDER_STEP_CHECKS)) == []

        command.upgrade(config, "head")
        # Существующая строка получила значения по умолчанию.
        rows = asyncio.run(
            _query(
                database_url,
                "SELECT step_attempts, step_started_at FROM orders WHERE user_id IN "
                "(SELECT id FROM users WHERE tg_id >= :base)",
                {"base": COMMITTED_TG_ID_BASE},
            )
        )
        assert [tuple(r) for r in rows] == [(0, None)]
        assert len(asyncio.run(_query(database_url, ORDER_STEP_CHECKS))) == 2
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("UPDATE orders SET step_attempts = -1", "ck_orders_step_attempts_non_negative"),
        (
            "UPDATE orders SET status = 'waiting_for_files', step_started_at = now()",
            "ck_orders_step_started_at_consistent",
        ),
    ],
)
def test_order_step_constraints(database_url: str, sql: str, constraint: str) -> None:
    try:
        asyncio.run(_seed_order(database_url, "analyzing_materials"))
        with pytest.raises(Exception, match=constraint):
            asyncio.run(
                _query(
                    database_url,
                    f"{sql} WHERE user_id IN (SELECT id FROM users WHERE tg_id >= :base)",
                    {"base": COMMITTED_TG_ID_BASE},
                )
            )
    finally:
        asyncio.run(_cleanup(database_url))


# --- 0007: тексты разделов отчёта и запись в кэш промпта -----------------------------

REPORT_SECTION_TABLE = "SELECT to_regclass('order_report_sections')::text"
REPORT_SECTION_CONSTRAINTS = (
    "SELECT conname FROM pg_constraint WHERE conrelid = 'order_report_sections'::regclass "
    "AND contype IN ('c', 'u', 'f') ORDER BY conname"
)
CACHE_CREATION_COLUMN = (
    "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = 'llm_calls' AND column_name = 'cache_creation_tokens'"
)
SEEDED_ORDERS = "SELECT id FROM orders WHERE user_id IN (SELECT id FROM users WHERE tg_id >= :base)"


async def _seed_plan(url: str) -> dict[str, int]:
    """Заказ с черновиками ТЗ и плана (зафиксировано): id заказа и плана."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            user_id = await connection.scalar(
                text("INSERT INTO users (tg_id) VALUES (:tg) RETURNING id"),
                {"tg": COMMITTED_TG_ID_BASE + 700},
            )
            order_id = await connection.scalar(
                text(
                    "INSERT INTO orders (user_id, doc_type, status) "
                    "VALUES (:user, 'practice_report', 'waiting_for_files') RETURNING id"
                ),
                {"user": user_id},
            )
            requirements_id = await connection.scalar(
                text(
                    "INSERT INTO order_requirements_versions (order_id, version, status, content) "
                    "VALUES (:order, 1, 'draft', '{}') RETURNING id"
                ),
                {"order": order_id},
            )
            plan_id = await connection.scalar(
                text(
                    "INSERT INTO order_plan_versions "
                    "(order_id, version, status, content, requirements_version_id) "
                    "VALUES (:order, 1, 'draft', '{}', :requirements) RETURNING id"
                ),
                {"order": order_id, "requirements": requirements_id},
            )
    finally:
        await engine.dispose()
    return {"order": order_id, "plan": plan_id}


def _insert_section(url: str, ids: dict[str, int], index: int = 0, chars: int = 5) -> None:
    asyncio.run(
        _query(
            url,
            "INSERT INTO order_report_sections "
            "(order_id, plan_version_id, section_index, content, char_count) "
            'VALUES (:order, :plan, :index, \'{"paragraphs": ["Текст"], "subsections": []}\', '
            ":chars)",
            {**ids, "index": index, "chars": chars},
        )
    )


def test_report_sections_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    try:
        ids = asyncio.run(_seed_plan(database_url))
        assert asyncio.run(_query(database_url, REPORT_SECTION_TABLE)) == [
            ("order_report_sections",)
        ]
        assert [
            row[0] for row in asyncio.run(_query(database_url, REPORT_SECTION_CONSTRAINTS))
        ] == [
            "ck_order_report_sections_char_count_positive",
            "ck_order_report_sections_section_index_non_negative",
            "fk_order_report_sections_order_id_orders",
            "fk_order_report_sections_plan_version_id_order_plan_versions",
            "uq_order_report_sections_plan_version_id_section_index",
        ]
        assert [tuple(r) for r in asyncio.run(_query(database_url, CACHE_CREATION_COLUMN))] == [
            ("integer", "NO", "0")
        ]
        _insert_section(database_url, ids)
        asyncio.run(
            _query(
                database_url,
                "INSERT INTO llm_calls (order_id, step, provider, model, status) "
                "VALUES (:order, 'plan_generation', 'anthropic', 'm', 'success')",
                {"order": ids["order"]},
            )
        )

        command.downgrade(config, "0006")
        assert asyncio.run(_query(database_url, REPORT_SECTION_TABLE)) == [(None,)]
        assert asyncio.run(_query(database_url, CACHE_CREATION_COLUMN)) == []

        command.upgrade(config, "head")
        # Существующая строка llm_calls получила 0; таблица разделов пуста и рабочая.
        rows = asyncio.run(
            _query(
                database_url,
                f"SELECT cache_creation_tokens FROM llm_calls WHERE order_id IN ({SEEDED_ORDERS})",
                {"base": COMMITTED_TG_ID_BASE},
            )
        )
        assert [tuple(r) for r in rows] == [(0,)]
        _insert_section(database_url, ids)
    finally:
        command.upgrade(config, "head")
        # Строки llm_calls не удаляются вместе с заказом (SET NULL).
        asyncio.run(
            _query(
                database_url,
                f"DELETE FROM llm_calls WHERE order_id IN ({SEEDED_ORDERS})",
                {"base": COMMITTED_TG_ID_BASE},
            )
        )
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("index", "chars", "constraint"),
    [
        (0, 7, "uq_order_report_sections_plan_version_id_section_index"),
        (-1, 5, "ck_order_report_sections_section_index_non_negative"),
        (1, 0, "ck_order_report_sections_char_count_positive"),
    ],
)
def test_report_section_constraints(
    database_url: str, index: int, chars: int, constraint: str
) -> None:
    try:
        ids = asyncio.run(_seed_plan(database_url))
        _insert_section(database_url, ids)
        with pytest.raises(Exception, match=constraint):
            _insert_section(database_url, ids, index=index, chars=chars)
    finally:
        asyncio.run(_cleanup(database_url))


def test_report_sections_deleted_with_order(database_url: str) -> None:
    try:
        ids = asyncio.run(_seed_plan(database_url))
        _insert_section(database_url, ids)
        asyncio.run(_cleanup(database_url))  # удаляет заказы: разделы — каскадом
        count = "SELECT count(*) FROM order_report_sections WHERE order_id = :order"
        assert asyncio.run(_query(database_url, count, {"order": ids["order"]})) == [(0,)]
    finally:
        asyncio.run(_cleanup(database_url))
