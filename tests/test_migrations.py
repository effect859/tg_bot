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
    # Финансовые записи не удаляются каскадом (FK без ON DELETE): раньше заказов.
    for table in ("balance_transactions", "payments"):
        await _query(
            url, f"DELETE FROM {table} WHERE user_id IN ({users})", {"base": COMMITTED_TG_ID_BASE}
        )
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


# --- 0008: готовый файл отчёта ---------------------------------------------------------------

REPORT_FILE_TABLE = "SELECT to_regclass('order_report_files')::text"
REPORT_FILE_CONSTRAINTS = (
    "SELECT conname FROM pg_constraint WHERE conrelid = 'order_report_files'::regclass "
    "AND contype IN ('c', 'u', 'f') ORDER BY conname"
)
REPORT_FILE_INDEXES = (
    "SELECT indexname FROM pg_indexes WHERE tablename = 'order_report_files' ORDER BY indexname"
)
SHA256 = "0" * 64


def _insert_report_file(
    url: str, ids: dict[str, int], *, size: int = 1, sha256: str = SHA256
) -> None:
    asyncio.run(
        _query(
            url,
            "INSERT INTO order_report_files (order_id, plan_version_id, storage_key, size_bytes, "
            "sha256) VALUES (:order, :plan, 'orders/1/report/plan-1.docx', :size, :sha256)",
            {**ids, "size": size, "sha256": sha256},
        )
    )


def test_report_files_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    try:
        ids = asyncio.run(_seed_plan(database_url))
        assert asyncio.run(_query(database_url, REPORT_FILE_TABLE)) == [("order_report_files",)]
        assert [row[0] for row in asyncio.run(_query(database_url, REPORT_FILE_CONSTRAINTS))] == [
            "ck_order_report_files_sha256_hex",
            "ck_order_report_files_size_positive",
            "fk_order_report_files_order_id_orders",
            "fk_order_report_files_plan_version_id_order_plan_versions",
            "uq_order_report_files_plan_version_id",
        ]
        assert [row[0] for row in asyncio.run(_query(database_url, REPORT_FILE_INDEXES))] == [
            "ix_order_report_files_order_id",
            "pk_order_report_files",
            "uq_order_report_files_plan_version_id",
        ]
        _insert_report_file(database_url, ids)

        command.downgrade(config, "0007")
        assert asyncio.run(_query(database_url, REPORT_FILE_TABLE)) == [(None,)]

        command.upgrade(config, "head")
        # Таблица пуста и рабочая.
        count = "SELECT count(*) FROM order_report_files WHERE order_id = :order"
        assert asyncio.run(_query(database_url, count, {"order": ids["order"]})) == [(0,)]
        _insert_report_file(database_url, ids)
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("size", "sha256", "constraint"),
    [
        (1, SHA256, "uq_order_report_files_plan_version_id"),
        (0, "1" * 64, "ck_order_report_files_size_positive"),
        (1, "A" * 64, "ck_order_report_files_sha256_hex"),
        (1, "0" * 63, "ck_order_report_files_sha256_hex"),
    ],
)
def test_report_file_constraints(
    database_url: str, size: int, sha256: str, constraint: str
) -> None:
    try:
        ids = asyncio.run(_seed_plan(database_url))
        _insert_report_file(database_url, ids)
        # Второй файл той же версии плана — нарушение UNIQUE; остальные — CHECK
        # (значения проверяются раньше уникальности).
        with pytest.raises(Exception, match=constraint):
            _insert_report_file(database_url, ids, size=size, sha256=sha256)
    finally:
        asyncio.run(_cleanup(database_url))


def test_report_files_deleted_with_order(database_url: str) -> None:
    try:
        ids = asyncio.run(_seed_plan(database_url))
        _insert_report_file(database_url, ids)
        asyncio.run(_cleanup(database_url))  # удаляет заказы: файлы — каскадом
        count = "SELECT count(*) FROM order_report_files WHERE order_id = :order"
        assert asyncio.run(_query(database_url, count, {"order": ids["order"]})) == [(0,)]
    finally:
        asyncio.run(_cleanup(database_url))


# --- 0009: доставка готового отчёта ----------------------------------------------------------

DELIVERY_TABLE = "SELECT to_regclass('order_report_deliveries')::text"
DELIVERY_CONSTRAINTS = (
    "SELECT conname FROM pg_constraint WHERE conrelid = 'order_report_deliveries'::regclass "
    "AND contype IN ('c', 'u', 'f') ORDER BY conname"
)
DELIVERY_INDEXES = (
    "SELECT indexname FROM pg_indexes WHERE tablename = 'order_report_deliveries' "
    "ORDER BY indexname"
)
DELIVERY_CASCADE = (
    "SELECT confdeltype::text FROM pg_constraint "
    "WHERE conname = 'fk_order_report_deliveries_report_file_id_order_report_files'"
)
DELIVERY_BY_FILE = "SELECT count(*) FROM order_report_deliveries WHERE report_file_id = :file"
DELIVERY_COUNT = (
    "SELECT count(*) FROM order_report_deliveries d "
    "JOIN order_report_files f ON f.id = d.report_file_id WHERE f.order_id = :order"
)


def _seed_report_file(url: str) -> dict[str, int]:
    """Заказ, план и строка файла (зафиксировано): id заказа, плана и файла."""
    ids = asyncio.run(_seed_plan(url))
    _insert_report_file(url, ids)
    [(file_id,)] = asyncio.run(
        _query(url, "SELECT id FROM order_report_files WHERE order_id = :order", ids)
    )
    return {**ids, "file": file_id}


def _insert_delivery(url: str, file_id: int, values: str = "0, NULL, NULL, NULL") -> None:
    asyncio.run(
        _query(
            url,
            "INSERT INTO order_report_deliveries "
            f"(report_file_id, attempts, started_at, delivered_at, error) VALUES (:file, {values})",
            {"file": file_id},
        )
    )


def test_report_deliveries_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    try:
        ids = _seed_report_file(database_url)
        assert asyncio.run(_query(database_url, DELIVERY_TABLE)) == [("order_report_deliveries",)]
        assert [row[0] for row in asyncio.run(_query(database_url, DELIVERY_CONSTRAINTS))] == [
            "ck_order_report_deliveries_attempts_non_negative",
            "ck_order_report_deliveries_delivered_xor_error",
            "ck_order_report_deliveries_started_only_while_pending",
            "fk_order_report_deliveries_report_file_id_order_report_files",
            "uq_order_report_deliveries_report_file_id",
        ]
        # Отдельного индекса нет: поиск по файлу обслуживает UNIQUE.
        assert [row[0] for row in asyncio.run(_query(database_url, DELIVERY_INDEXES))] == [
            "pk_order_report_deliveries",
            "uq_order_report_deliveries_report_file_id",
        ]
        assert asyncio.run(_query(database_url, DELIVERY_CASCADE)) == [("c",)]
        _insert_delivery(database_url, ids["file"])

        command.downgrade(config, "0008")
        assert asyncio.run(_query(database_url, DELIVERY_TABLE)) == [(None,)]
        # Файл отчёта (0008) остался.
        count = "SELECT count(*) FROM order_report_files WHERE order_id = :order"
        assert asyncio.run(_query(database_url, count, {"order": ids["order"]})) == [(1,)]

        command.upgrade(config, "head")
        # Таблица пуста и рабочая; attempts по умолчанию — 0.
        assert asyncio.run(_query(database_url, DELIVERY_COUNT, ids)) == [(0,)]
        asyncio.run(
            _query(
                database_url,
                "INSERT INTO order_report_deliveries (report_file_id) VALUES (:file)",
                ids,
            )
        )
        attempts = "SELECT attempts, created_at IS NOT NULL FROM order_report_deliveries"
        assert [tuple(r) for r in asyncio.run(_query(database_url, attempts))] == [(0, True)]
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ("-1, NULL, NULL, NULL", "ck_order_report_deliveries_attempts_non_negative"),
        ("1, NULL, now(), 'forbidden'", "ck_order_report_deliveries_delivered_xor_error"),
        ("1, now(), now(), NULL", "ck_order_report_deliveries_started_only_while_pending"),
        ("1, now(), NULL, 'forbidden'", "ck_order_report_deliveries_started_only_while_pending"),
        ("0, NULL, NULL, NULL", "uq_order_report_deliveries_report_file_id"),
    ],
)
def test_report_delivery_constraints(database_url: str, values: str, constraint: str) -> None:
    try:
        ids = _seed_report_file(database_url)
        if constraint.startswith("uq_"):
            _insert_delivery(database_url, ids["file"])
        with pytest.raises(Exception, match=constraint):
            _insert_delivery(database_url, ids["file"], values)
    finally:
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    "values",
    [
        "0, NULL, NULL, NULL",  # ещё не начата
        "1, now(), NULL, NULL",  # идёт попытка
        "1, NULL, now(), NULL",  # доставлено
        "3, NULL, NULL, 'attempts_exhausted'",  # постоянная ошибка
    ],
)
def test_report_delivery_valid_states(database_url: str, values: str) -> None:
    try:
        ids = _seed_report_file(database_url)
        _insert_delivery(database_url, ids["file"], values)
        assert asyncio.run(_query(database_url, DELIVERY_COUNT, ids)) == [(1,)]
    finally:
        asyncio.run(_cleanup(database_url))


def test_report_delivery_deleted_with_file(database_url: str) -> None:
    try:
        ids = _seed_report_file(database_url)
        _insert_delivery(database_url, ids["file"])
        asyncio.run(_query(database_url, "DELETE FROM order_report_files WHERE id = :file", ids))
        assert asyncio.run(_query(database_url, DELIVERY_BY_FILE, ids)) == [(0,)]
    finally:
        asyncio.run(_cleanup(database_url))


def test_report_delivery_deleted_with_order(database_url: str) -> None:
    try:
        ids = _seed_report_file(database_url)
        _insert_delivery(database_url, ids["file"])
        asyncio.run(_cleanup(database_url))  # удаляет заказы: файлы и доставки — каскадом
        assert asyncio.run(_query(database_url, DELIVERY_BY_FILE, ids)) == [(0,)]
    finally:
        asyncio.run(_cleanup(database_url))


# --- 0010: кредиты и биллинг -----------------------------------------------------------------

BILLING_CONSTRAINTS = (
    "SELECT conname FROM pg_constraint WHERE conname IN ("
    "'ck_payments_credits_positive', 'ck_payments_succeeded_has_provider_id', "
    "'ck_balance_transactions_reason_consistent', 'ck_orders_price_positive') ORDER BY conname"
)
BILLING_INDEX = (
    "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_balance_transactions_payment_id'"
)
CREDITS_COLUMN = (
    "SELECT data_type, is_nullable FROM information_schema.columns "
    "WHERE table_name = 'payments' AND column_name = 'credits'"
)


def _seed_billing(url: str) -> dict[str, int]:
    """Заказ (как _seed_plan), его пользователь и PENDING-платёж на 100 кредитов."""
    ids = asyncio.run(_seed_plan(url))
    [(user_id,)] = asyncio.run(_query(url, "SELECT user_id FROM orders WHERE id = :order", ids))
    [(payment_id,)] = asyncio.run(
        _query(
            url,
            "INSERT INTO payments (user_id, provider, amount, currency, credits, status) "
            "VALUES (:user, 'telegram_stars', 100, 'XTR', 100, 'pending') RETURNING id",
            {"user": user_id},
        )
    )
    return {**ids, "user": user_id, "payment": payment_id}


def _insert_transaction(url: str, ids: dict[str, int], values: str) -> None:
    """values — (amount, reason, order_id, payment_id) с параметрами :order и :payment."""
    asyncio.run(
        _query(
            url,
            "INSERT INTO balance_transactions (user_id, amount, reason, order_id, payment_id) "
            f"VALUES (:user, {values})",
            ids,
        )
    )


def test_billing_migration_downgrade_and_upgrade(database_url: str) -> None:
    config = make_alembic_config(database_url)
    try:
        assert [row[0] for row in asyncio.run(_query(database_url, BILLING_CONSTRAINTS))] == [
            "ck_balance_transactions_reason_consistent",
            "ck_orders_price_positive",
            "ck_payments_credits_positive",
            "ck_payments_succeeded_has_provider_id",
        ]
        [(indexdef,)] = asyncio.run(_query(database_url, BILLING_INDEX))
        assert "UNIQUE" in indexdef and "WHERE (payment_id IS NOT NULL)" in indexdef
        assert [tuple(r) for r in asyncio.run(_query(database_url, CREDITS_COLUMN))] == [
            ("integer", "NO")
        ]

        command.downgrade(config, "0009")
        assert asyncio.run(_query(database_url, BILLING_CONSTRAINTS)) == []
        assert asyncio.run(_query(database_url, BILLING_INDEX)) == []
        assert asyncio.run(_query(database_url, CREDITS_COLUMN)) == []

        command.upgrade(config, "head")
        assert len(asyncio.run(_query(database_url, BILLING_CONSTRAINTS))) == 4
        # Схема рабочая: счёт, зачисление и списание проходят.
        ids = _seed_billing(database_url)
        _insert_transaction(database_url, ids, "100, 'topup', NULL, :payment")
        _insert_transaction(database_url, ids, "-50, 'order_charge', :order, NULL")
    finally:
        command.upgrade(config, "head")
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        # Пополнение: только плюс, только по платежу, без заказа.
        ("-100, 'topup', NULL, :payment", "ck_balance_transactions_reason_consistent"),
        ("100, 'topup', NULL, NULL", "ck_balance_transactions_reason_consistent"),
        ("100, 'topup', :order, :payment", "ck_balance_transactions_reason_consistent"),
        # Списание: только минус, только по заказу, без платежа.
        ("50, 'order_charge', :order, NULL", "ck_balance_transactions_reason_consistent"),
        ("-50, 'order_charge', NULL, NULL", "ck_balance_transactions_reason_consistent"),
        ("-50, 'order_charge', :order, :payment", "ck_balance_transactions_reason_consistent"),
        # Возврат: только плюс и только по заказу.
        ("-50, 'order_refund', :order, NULL", "ck_balance_transactions_reason_consistent"),
        ("50, 'order_refund', NULL, NULL", "ck_balance_transactions_reason_consistent"),
        ("0, 'bonus', NULL, NULL", "ck_balance_transactions_amount_non_zero"),
    ],
)
def test_balance_transaction_constraints(database_url: str, values: str, constraint: str) -> None:
    try:
        ids = _seed_billing(database_url)
        with pytest.raises(Exception, match=constraint):
            _insert_transaction(database_url, ids, values)
    finally:
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    "values",
    [
        "100, 'topup', NULL, :payment",
        "-50, 'order_charge', :order, NULL",
        "50, 'order_refund', :order, NULL",
        "10, 'bonus', NULL, NULL",
        "-10, 'admin_adjust', NULL, NULL",
    ],
)
def test_balance_transaction_valid_rows(database_url: str, values: str) -> None:
    try:
        ids = _seed_billing(database_url)
        _insert_transaction(database_url, ids, values)
    finally:
        asyncio.run(_cleanup(database_url))


def test_one_topup_per_payment(database_url: str) -> None:
    try:
        ids = _seed_billing(database_url)
        _insert_transaction(database_url, ids, "100, 'topup', NULL, :payment")
        with pytest.raises(Exception, match="uq_balance_transactions_payment_id"):
            _insert_transaction(database_url, ids, "100, 'topup', NULL, :payment")
    finally:
        asyncio.run(_cleanup(database_url))


def test_one_charge_per_order(database_url: str) -> None:
    try:
        ids = _seed_billing(database_url)
        _insert_transaction(database_url, ids, "-50, 'order_charge', :order, NULL")
        with pytest.raises(Exception, match="uq_balance_transactions_order_id_reason"):
            _insert_transaction(database_url, ids, "-50, 'order_charge', :order, NULL")
    finally:
        asyncio.run(_cleanup(database_url))


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("UPDATE payments SET credits = 0 WHERE id = :payment", "ck_payments_credits_positive"),
        (
            "UPDATE payments SET status = 'succeeded' WHERE id = :payment",
            "ck_payments_succeeded_has_provider_id",
        ),
        ("UPDATE orders SET price = 0 WHERE id = :order", "ck_orders_price_positive"),
        ("UPDATE users SET balance = -1 WHERE id = :user", "ck_users_balance_non_negative"),
        (
            "INSERT INTO payments (user_id, provider, provider_payment_id, amount, currency, "
            "credits, status) VALUES (:user, 'telegram_stars', 'charge-1', 100, 'XTR', 100, "
            "'succeeded'), (:user, 'telegram_stars', 'charge-1', 100, 'XTR', 100, 'succeeded')",
            "uq_payments_provider_provider_payment_id",
        ),
    ],
)
def test_billing_row_constraints(database_url: str, sql: str, constraint: str) -> None:
    try:
        ids = _seed_billing(database_url)
        with pytest.raises(Exception, match=constraint):
            asyncio.run(_query(database_url, sql, ids))
    finally:
        asyncio.run(_cleanup(database_url))


def test_succeeded_payment_with_provider_id_is_valid(database_url: str) -> None:
    try:
        ids = _seed_billing(database_url)
        asyncio.run(
            _query(
                database_url,
                "UPDATE payments SET status = 'succeeded', provider_payment_id = 'charge-1', "
                "credits = 1 WHERE id = :payment",
                ids,
            )
        )
        asyncio.run(_query(database_url, "UPDATE orders SET price = 1 WHERE id = :order", ids))
    finally:
        asyncio.run(_cleanup(database_url))
