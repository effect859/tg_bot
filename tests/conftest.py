from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    test_database_url: str = "postgresql+asyncpg://tg_bot:tg_bot@localhost:5432/tg_bot_test"


def make_alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Пересоздаёт схему тестовой БД через миграции (заодно проверяя downgrade/upgrade)."""
    url = _TestEnv().test_database_url
    config = make_alembic_config(url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Внешняя транзакция на тест: всё, что тест записал, откатывается."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield conn
        await transaction.rollback()


@pytest.fixture
def sessionmaker(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    # commit()/rollback() внутри кода превращаются в SAVEPOINT внешней транзакции.
    return async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as session:
        yield session


# tg_id пользователей тестов с настоящими транзакциями: по ним данные удаляются после теста.
COMMITTED_TG_ID_BASE = 9_000_000_000


@pytest.fixture
async def committed_sessionmaker(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Сессии как в боте: каждая — своё соединение и настоящие commit (NullPool).

    Для гонок между независимыми сессиями: SAVEPOINT-фикстура выше держит одно
    соединение, и блокировки строк между её сессиями не действуют. Тест создаёт
    пользователей с tg_id >= COMMITTED_TG_ID_BASE; их заказы (с материалами и
    текстами — каскадом) и сами пользователи удаляются после теста.
    """
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        users = "SELECT id FROM users WHERE tg_id >= :base"
        await connection.execute(
            text(f"DELETE FROM orders WHERE user_id IN ({users})"),
            {"base": COMMITTED_TG_ID_BASE},
        )
        await connection.execute(
            text("DELETE FROM users WHERE tg_id >= :base"), {"base": COMMITTED_TG_ID_BASE}
        )
