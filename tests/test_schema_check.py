"""Проверка ревизии Alembic перед запуском бота (Stage 3.8).

Синхронные тесты для downgrade и upgrade: alembic/env.py сам запускает цикл событий
(asyncio.run), как в test_migrations.py.
"""

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app import main_bot
from app.config import ConfigurationError, Settings
from app.db.schema import (
    ALEMBIC_DIR,
    SchemaMismatchError,
    current_revision,
    ensure_schema_is_current,
    head_revision,
)
from conftest import make_alembic_config

ROOT = Path(__file__).resolve().parents[1]


async def _ensure(url: str) -> str:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        return await ensure_schema_is_current(engine)
    finally:
        await engine.dispose()


async def _current(url: str) -> str | None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        return await current_revision(engine)
    finally:
        await engine.dispose()


def test_alembic_dir_is_project_migrations() -> None:
    assert ALEMBIC_DIR == ROOT / "alembic"
    latest = sorted(p.name for p in (ROOT / "alembic" / "versions").glob("0*.py"))[-1]
    assert head_revision() == latest.split("_", 1)[0]


def test_schema_at_head_passes(database_url: str) -> None:
    assert asyncio.run(_ensure(database_url)) == head_revision()


def test_schema_behind_head_is_refused(database_url: str) -> None:
    config = make_alembic_config(database_url)
    head = head_revision()
    try:
        command.downgrade(config, "-1")
        current = asyncio.run(_current(database_url))
        assert current is not None and current != head

        with pytest.raises(SchemaMismatchError) as error:
            asyncio.run(_ensure(database_url))

        assert (error.value.current, error.value.head) == (current, head)
        assert isinstance(error.value, ConfigurationError)
        assert "alembic upgrade head" in str(error.value)
    finally:
        command.upgrade(config, "head")
    assert asyncio.run(_ensure(database_url)) == head


def test_missing_head_is_refused(database_url: str, tmp_path: Path) -> None:
    """Каталог миграций без версий (head None) — тоже несовпадение, бот не стартует."""
    (tmp_path / "versions").mkdir()
    (tmp_path / "script.py.mako").write_text("", encoding="utf-8")
    assert head_revision(tmp_path) is None

    async def check() -> None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            await ensure_schema_is_current(engine, script_dir=tmp_path)
        finally:
            await engine.dispose()

    with pytest.raises(SchemaMismatchError) as error:
        asyncio.run(check())
    assert error.value.head is None


async def test_main_does_not_poll_on_schema_mismatch(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        bot_token="123456:test",
        anthropic_api_key="test-key",
        database_url=database_url,
        storage_dir=tmp_path / "storage",
        heartbeat_path=tmp_path / "heartbeat",
    )
    dispatcher_factory = MagicMock()
    heartbeat_factory = MagicMock()

    async def mismatch(engine: object) -> str:
        raise SchemaMismatchError("0009", "0010")

    monkeypatch.setattr(main_bot, "get_settings", lambda: settings)
    monkeypatch.setattr(main_bot, "setup_logging", lambda level: None)
    monkeypatch.setattr(main_bot, "ensure_schema_is_current", mismatch)
    monkeypatch.setattr(main_bot, "create_dispatcher", dispatcher_factory)
    monkeypatch.setattr(main_bot, "Heartbeat", heartbeat_factory)
    caplog.set_level(logging.ERROR)

    with pytest.raises(SystemExit) as exit_info:
        await main_bot.main()

    assert exit_info.value.code == 1
    # Ни диспетчера, ни heartbeat, ни polling: бот не стартовал.
    dispatcher_factory.assert_not_called()
    heartbeat_factory.assert_not_called()
    assert "Bot is not started: Database schema revision is 0009, expected head 0010" in (
        caplog.text
    )
    assert not (tmp_path / "heartbeat").exists()
