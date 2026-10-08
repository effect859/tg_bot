"""Проверка схемы БД перед запуском бота: текущая ревизия Alembic == head.

Только чтение (таблица alembic_version и каталог миграций). Миграции бот не
выполняет: их применяет отдельный шаг (`alembic upgrade head`, сервис migrate в
docker-compose.prod.yml) до старта бота. На несовпадающей схеме бот не начинает
polling — иначе он упал бы на первом запросе к новым или удалённым колонкам.
"""

from pathlib import Path

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import ConfigurationError

# src/app/db/schema.py → корень проекта (рядом с alembic.ini).
ALEMBIC_DIR = Path(__file__).resolve().parents[3] / "alembic"


class SchemaMismatchError(ConfigurationError):
    def __init__(self, current: str | None, head: str | None) -> None:
        super().__init__(
            f"Database schema revision is {current or 'empty'}, expected head {head}: "
            "run `alembic upgrade head` before starting the bot"
        )
        self.current = current
        self.head = head


def head_revision(script_dir: Path = ALEMBIC_DIR) -> str | None:
    return ScriptDirectory(str(script_dir)).get_current_head()


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_revision()
        )


async def ensure_schema_is_current(engine: AsyncEngine, *, script_dir: Path = ALEMBIC_DIR) -> str:
    """Ревизия head, если схема на ней. Raises SchemaMismatchError."""
    head = head_revision(script_dir)
    current = await current_revision(engine)
    if head is None or current != head:
        raise SchemaMismatchError(current, head)
    return head
