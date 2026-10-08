"""Heartbeat бота и CLI healthcheck (Stage 3.8).

Heartbeat — PeriodicPass с SELECT 1 и атомарной записью метки; healthcheck читает
метку без БД и без приложения. Проверяются и запуск бота: heartbeat стартует до
polling и останавливается после него.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app import healthcheck, main_bot
from app.config import Settings
from app.services import heartbeat as heartbeat_module
from app.services.heartbeat import Heartbeat, write_marker

ROOT = Path(__file__).resolve().parents[1]


# --- Метка ----------------------------------------------------------------------------


def test_write_marker_is_atomic_and_cleans_up(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "heartbeat"

    write_marker(path, 1234.5)

    assert path.read_text(encoding="ascii") == "1234.500\n"
    assert [p.name for p in path.parent.iterdir()] == ["heartbeat"]


def test_failed_replace_keeps_old_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "heartbeat"
    write_marker(path, 100.0)

    def broken(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(heartbeat_module.os, "replace", broken)
    with pytest.raises(OSError):
        write_marker(path, 200.0)

    assert path.read_text(encoding="ascii") == "100.000\n"
    assert [p.name for p in tmp_path.iterdir()] == ["heartbeat"]  # tmp удалён


# --- Healthcheck ----------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "garbage", "nan", "inf", "-inf", "1e400x", "  "])
def test_corrupted_marker_is_unhealthy(tmp_path: Path, content: str) -> None:
    path = tmp_path / "heartbeat"
    path.write_text(content, encoding="ascii")

    assert healthcheck.read_marker(path) is None
    assert not healthcheck.is_fresh(path, 120, now=time.time())


def test_non_ascii_marker_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.write_bytes(b"\xff\xfe")

    assert not healthcheck.is_fresh(path, 120)


def test_missing_marker_is_unhealthy(tmp_path: Path) -> None:
    assert not healthcheck.is_fresh(tmp_path / "missing", 120)


@pytest.mark.parametrize(
    ("age", "fresh"),
    [
        (0, True),
        (119.9, True),
        (120, True),
        (120.1, False),
        (3600, False),
        (-1, True),
        (-60, False),
    ],
)
def test_marker_age(tmp_path: Path, age: float, fresh: bool) -> None:
    path = tmp_path / "heartbeat"
    now = 1_000_000.0
    write_marker(path, now - age)

    assert healthcheck.is_fresh(path, 120, now=now) is fresh


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("HEARTBEAT_PATH", str(path))
    monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "120")

    assert healthcheck.main() == 1  # нет метки
    write_marker(path, time.time())
    assert healthcheck.main() == 0
    write_marker(path, time.time() - 121)
    assert healthcheck.main() == 1
    monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "not-a-number")
    assert healthcheck.main() == 1


def test_cli_runs_without_app_settings(tmp_path: Path) -> None:
    """Отдельный процесс: без BOT_TOKEN, БД и сети — только файл метки."""
    path = tmp_path / "heartbeat"
    write_marker(path, time.time())
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BOT_TOKEN", "DATABASE_URL", "ANTHROPIC_API_KEY"}
    }
    env.update(
        {"HEARTBEAT_PATH": str(path), "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
    )

    fresh = subprocess.run(
        [sys.executable, "-m", "app.healthcheck"], env=env, capture_output=True, text=True
    )
    env["HEARTBEAT_PATH"] = str(tmp_path / "missing")
    missing = subprocess.run(
        [sys.executable, "-m", "app.healthcheck"], env=env, capture_output=True, text=True
    )

    assert (fresh.returncode, fresh.stdout.strip()) == (0, "healthy")
    assert missing.returncode == 1


def test_healthcheck_does_not_import_application() -> None:
    code = (
        "import sys; import app.healthcheck; "
        "print(sorted(m for m in sys.modules if m.startswith('app.') and m != 'app.healthcheck'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "[]"


# --- Heartbeat (БД) -------------------------------------------------------------------


async def test_heartbeat_writes_fresh_marker(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    path = tmp_path / "heartbeat"

    assert await Heartbeat(sessionmaker, path, interval_seconds=30).run_pass() == []

    assert healthcheck.is_fresh(path, 30)


async def test_heartbeat_only_selects_one(engine: AsyncEngine, tmp_path: Path) -> None:
    """Никаких блокировок строк и долгой транзакции: ровно один SELECT 1."""
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    await Heartbeat(sessionmaker, tmp_path / "heartbeat", interval_seconds=30).run_pass()

    assert [s for s in statements if not s.lstrip().upper().startswith("SELECT PG_")] == [
        "SELECT 1"
    ]
    assert not any("FOR UPDATE" in s.upper() for s in statements)


async def test_database_failure_keeps_marker_and_logs_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    path = tmp_path / "heartbeat"
    write_marker(path, 500.0)
    secret_url = "postgresql+asyncpg://tg_bot:very-secret@127.0.0.1:1/nowhere"
    engine = create_async_engine(secret_url, poolclass=NullPool)
    try:
        sessionmaker = async_sessionmaker(engine)
        assert await Heartbeat(sessionmaker, path, interval_seconds=30).run_pass() == []
    finally:
        await engine.dispose()

    assert path.read_text(encoding="ascii") == "500.000\n"  # метка не обновлена
    assert "Heartbeat database check failed: error=" in caplog.text
    assert "very-secret" not in caplog.text


async def test_heartbeat_first_pass_at_start_and_stop(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    path = tmp_path / "heartbeat"
    heartbeat = Heartbeat(sessionmaker, path, interval_seconds=3600)

    heartbeat.start()
    try:
        for _ in range(250):
            if path.exists():
                break
            await asyncio.sleep(0.02)
        assert heartbeat.running
    finally:
        await heartbeat.stop()

    assert healthcheck.is_fresh(path, 3600)
    assert not heartbeat.running


# --- Запуск бота ----------------------------------------------------------------------


async def test_main_starts_heartbeat_before_polling_and_stops_after(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        bot_token="123456:test",
        anthropic_api_key="test-key",
        database_url=database_url,
        storage_dir=tmp_path / "storage",
        heartbeat_path=tmp_path / "heartbeat",
    )

    class FakeHeartbeat:
        def __init__(self, sessionmaker: object, path: Path, *, interval_seconds: float) -> None:
            assert (path, interval_seconds) == (settings.heartbeat_path, 30.0)

        def start(self) -> None:
            events.append("heartbeat.start")

        async def stop(self) -> None:
            events.append("heartbeat.stop")

    async def polling(*args: object, **kwargs: object) -> None:
        events.append("polling")

    dispatcher = MagicMock()
    dispatcher.start_polling = AsyncMock(side_effect=polling)
    monkeypatch.setattr(main_bot, "get_settings", lambda: settings)
    monkeypatch.setattr(main_bot, "setup_logging", lambda level: None)
    monkeypatch.setattr(main_bot, "create_dispatcher", lambda *args: dispatcher)
    monkeypatch.setattr(main_bot, "Heartbeat", FakeHeartbeat)

    await main_bot.main()

    assert events == ["heartbeat.start", "polling", "heartbeat.stop"]


def test_heartbeat_logger_name() -> None:
    assert logging.getLogger(heartbeat_module.__name__).name == "app.services.heartbeat"
