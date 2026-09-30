from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models  # noqa: F401  — регистрирует модели в Base.metadata
from app.db.base import Base


def _diff(connection: Connection) -> list:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(context, Base.metadata)


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    """Аналог `alembic check`: модели и схема после миграций не расходятся."""
    async with engine.connect() as connection:
        diff = await connection.run_sync(_diff)

    assert diff == []
