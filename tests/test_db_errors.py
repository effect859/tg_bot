"""Классификация ошибок БД на настоящих исключениях SQLAlchemy + asyncpg.

Ошибки получаются от настоящего PostgreSQL: часть — естественным путём (нарушение
CHECK, деление на ноль, statement_timeout, обрыв соединения), остальные — через
RAISE с нужным SQLSTATE: asyncpg и SQLAlchemy оборачивают их так же, как настоящие.
"""

import asyncio

import asyncpg
import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.errors import DbErrorKind, classify_db_error, describe_db_error, sqlstate

T, P = DbErrorKind.TEMPORARY, DbErrorKind.PERMANENT


async def _error(session: AsyncSession, sql: str, params: dict[str, object] | None = None):
    with pytest.raises(sa_exc.SQLAlchemyError) as caught:
        async with session.begin_nested():
            await session.execute(text(sql), params or {})
    return caught.value


def _raise_sql(code: str) -> str:
    return f"DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{code}'; END $$"


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("08006", T),  # connection_failure
        ("08003", T),  # connection_does_not_exist
        ("40001", T),  # serialization_failure
        ("40P01", T),  # deadlock_detected
        ("55P03", T),  # lock_not_available
        ("53100", T),  # disk_full
        ("53200", T),  # out_of_memory
        ("57014", T),  # query_canceled
        ("57P01", T),  # admin_shutdown
        ("23514", P),  # check_violation
        ("23505", P),  # unique_violation
        ("23503", P),  # foreign_key_violation
        ("22001", P),  # string_data_right_truncation
        ("22021", P),  # character_not_in_repertoire
        ("54000", P),  # program_limit_exceeded
    ],
)
async def test_sqlstate_classification(session: AsyncSession, code: str, kind: DbErrorKind) -> None:
    error = await _error(session, _raise_sql(code))

    assert isinstance(error, sa_exc.DBAPIError)
    assert sqlstate(error) == code
    assert classify_db_error(error) is kind
    assert describe_db_error(error).startswith(f"{kind.value} database error: sqlstate={code} ")


async def test_deadlock_is_a_generic_dbapi_error_not_operational(session: AsyncSession) -> None:
    """SQLAlchemy не переводит 40P01 в OperationalError: классифицировать по классу нельзя."""
    error = await _error(session, _raise_sql("40P01"))

    assert type(error) is sa_exc.DBAPIError
    assert classify_db_error(error) is T


async def test_unknown_sqlstate_is_temporary_and_marked(session: AsyncSession) -> None:
    error = await _error(session, _raise_sql("P0001"))

    assert classify_db_error(error) is T
    assert describe_db_error(error).startswith("temporary (unrecognized) database error")


async def test_real_check_violation_is_permanent(session: AsyncSession) -> None:
    error = await _error(
        session,
        "INSERT INTO order_material_texts (material_id, text, char_count, extractor) "
        "VALUES (0, 'abc', 2, 'x')",
    )

    assert isinstance(error, sa_exc.IntegrityError)
    assert sqlstate(error) == "23514"
    assert classify_db_error(error) is P


async def test_real_division_by_zero_is_permanent(session: AsyncSession) -> None:
    error = await _error(session, "SELECT 1 / 0")

    assert sqlstate(error) == "22012"
    assert classify_db_error(error) is P


async def test_real_statement_timeout_is_temporary(session: AsyncSession) -> None:
    with pytest.raises(sa_exc.SQLAlchemyError) as caught:
        async with session.begin_nested():
            await session.execute(text("SET LOCAL statement_timeout = 50"))
            await session.execute(text("SELECT pg_sleep(2)"))
    error = caught.value

    assert sqlstate(error) == "57014"
    assert classify_db_error(error) is T


async def test_client_side_parameter_error_is_permanent(session: AsyncSession) -> None:
    """asyncpg отвергает параметр до отправки на сервер (asyncpg.DataError, SQLSTATE 22000)."""
    error = await _error(session, "SELECT CAST(:x AS integer) + 1", {"x": "not a number"})

    assert isinstance(error.__cause__.__cause__, asyncpg.exceptions.DataError)
    assert sqlstate(error) == "22000"
    assert classify_db_error(error) is P


async def test_dropped_connection_is_temporary(engine: AsyncEngine) -> None:
    async with engine.connect() as victim, engine.connect() as killer:
        pid = await victim.scalar(text("SELECT pg_backend_pid()"))
        await killer.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await asyncio.sleep(0.2)
        with pytest.raises(sa_exc.SQLAlchemyError) as caught:
            await victim.execute(text("SELECT 1"))
        error = caught.value

    assert classify_db_error(error) is T
    assert "unrecognized" not in describe_db_error(error)


@pytest.mark.parametrize(
    "error",
    [ConnectionResetError("reset by peer"), TimeoutError(), sa_exc.TimeoutError("pool timeout")],
    ids=["os-error", "timeout", "pool-timeout"],
)
def test_unwrapped_connection_errors_are_temporary(error: BaseException) -> None:
    assert classify_db_error(error) is T
    assert "unrecognized" not in describe_db_error(error)


async def test_description_has_no_query_parameters(session: AsyncSession) -> None:
    secret = "Текст документа пользователя, который не должен попасть в processing_error"
    error = await _error(
        session,
        "INSERT INTO order_material_texts (material_id, text, char_count, extractor) "
        "VALUES (0, :text, 1, 'x')",
        {"text": secret},
    )

    assert secret in str(error)  # str() у ошибки SQLAlchemy содержит параметры запроса
    description = describe_db_error(error)
    assert secret not in description
    assert "ck_order_material_texts_char_count_matches" in description


async def test_description_is_bounded(session: AsyncSession) -> None:
    long_message = "x" * 5000
    error = await _error(
        session, f"DO $$ BEGIN RAISE EXCEPTION '{long_message}' USING ERRCODE = '40001'; END $$"
    )

    assert len(describe_db_error(error)) < 400
