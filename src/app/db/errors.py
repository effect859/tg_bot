"""Ошибки БД: временная (повтор имеет смысл) или постоянная (повтор даст тот же отказ).

Решение принимается по SQLSTATE PostgreSQL. SQLAlchemy с asyncpg переводит в свои
классы только часть ошибок сервера (IntegrityError, ProgrammingError, InterfaceError);
остальные — deadlock, сериализация, тайм-аут запроса — приходят общим DBAPIError.
Поэтому смотрим не на класс, а на код: SQLAlchemy кладёт его в ``orig.sqlstate``,
asyncpg — в исключение-причину. Обрыв соединения asyncpg иногда пробрасывает без
обёртки (OSError).

Нераспознанная ошибка считается временной: число попыток ограничено, а ошибочный
окончательный отказ лишил бы материал шанса.
"""

from collections.abc import Iterator
from enum import StrEnum

import asyncpg
from sqlalchemy import exc as sa_exc

# Классы SQLSTATE, при которых повтор с теми же данными даст тот же отказ:
# 22 — некорректные данные, 23 — нарушение ограничений, 54 — превышен предел программы.
PERMANENT_SQLSTATE_CLASSES = frozenset({"22", "23", "54"})
# Заведомо временные: соединение (08), откат транзакции — сериализация и deadlock (40),
# нехватка ресурсов сервера (53), отмена и остановка сервера, в том числе
# statement_timeout (57), и занятая блокировка (55P03).
TEMPORARY_SQLSTATE_CLASSES = frozenset({"08", "40", "53", "57"})
TEMPORARY_SQLSTATES = frozenset({"55P03"})

MAX_MESSAGE_LENGTH = 200
_MAX_CHAIN_DEPTH = 10


class DbErrorKind(StrEnum):
    TEMPORARY = "temporary"
    PERMANENT = "permanent"


def classify_db_error(error: BaseException) -> DbErrorKind:
    # Параметр, отвергнутый asyncpg ещё на клиенте (asyncpg.DataError), тоже несёт
    # SQLSTATE класса 22.
    code = sqlstate(error)
    if code is not None and code[:2] in PERMANENT_SQLSTATE_CLASSES:
        return DbErrorKind.PERMANENT
    return DbErrorKind.TEMPORARY


def sqlstate(error: BaseException) -> str | None:
    for e in _chain(error):
        code = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        if isinstance(code, str) and len(code) == 5:
            return code
    return None


def describe_db_error(error: BaseException) -> str:
    """Короткое описание для processing_error и логов: код, классы, сообщение сервера.

    Без параметров запроса: str() у ошибок SQLAlchemy включает SQL и параметры, а в
    них могут быть данные пользователя (например, извлечённый текст).
    """
    kind = classify_db_error(error).value
    if kind == DbErrorKind.TEMPORARY and not _is_recognized_temporary(error):
        kind += " (unrecognized)"
    root = list(_chain(error))[-1]
    if isinstance(root, asyncpg.PostgresError):
        message = root.message or ""
    elif isinstance(root, sa_exc.StatementError):
        message = ""
    else:
        message = str(root)
    message = " ".join(message.split())[:MAX_MESSAGE_LENGTH]
    return (
        f"{kind} database error: sqlstate={sqlstate(error) or '-'} "
        f"{type(error).__name__}/{type(root).__name__}: {message}"
    )


def _is_recognized_temporary(error: BaseException) -> bool:
    code = sqlstate(error)
    if code is not None:
        return code in TEMPORARY_SQLSTATES or code[:2] in TEMPORARY_SQLSTATE_CLASSES
    return any(
        (isinstance(e, sa_exc.DBAPIError) and e.connection_invalidated)
        or isinstance(
            e,
            sa_exc.InterfaceError
            | sa_exc.DisconnectionError
            | sa_exc.TimeoutError
            | asyncpg.InterfaceError
            | OSError,
        )
        for e in _chain(error)
    )


def _chain(error: BaseException) -> Iterator[BaseException]:
    """Исключение и его причины: ``orig`` у ошибок SQLAlchemy, иначе ``__cause__``."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < _MAX_CHAIN_DEPTH:
        seen.add(id(current))
        yield current
        orig = current.orig if isinstance(current, sa_exc.StatementError) else None
        current = orig if isinstance(orig, BaseException) else current.__cause__
