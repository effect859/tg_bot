"""Настройки heartbeat: значения по умолчанию и валидация (Stage 3.8)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

HEARTBEAT_ENV = ("HEARTBEAT_PATH", "HEARTBEAT_INTERVAL_SECONDS", "HEARTBEAT_MAX_AGE_SECONDS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in HEARTBEAT_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, bot_token="1:test", **values)  # type: ignore[arg-type]


def test_heartbeat_defaults() -> None:
    settings = _settings()

    assert settings.heartbeat_path == Path("var/heartbeat")
    assert settings.heartbeat_interval_seconds == 30.0
    assert settings.heartbeat_max_age_seconds == 120.0


def test_heartbeat_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEARTBEAT_PATH", "/tmp/hb")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("HEARTBEAT_MAX_AGE_SECONDS", "10")

    settings = _settings()

    assert (settings.heartbeat_path, settings.heartbeat_interval_seconds) == (Path("/tmp/hb"), 10)
    assert settings.heartbeat_max_age_seconds == 10  # равенство допустимо


@pytest.mark.parametrize("interval", [0, -1, -0.5])
def test_interval_must_be_positive(interval: float) -> None:
    with pytest.raises(ValidationError, match="heartbeat_interval_seconds"):
        _settings(heartbeat_interval_seconds=interval)


def test_max_age_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="heartbeat_max_age_seconds"):
        _settings(heartbeat_max_age_seconds=0)


def test_max_age_not_less_than_interval() -> None:
    with pytest.raises(ValidationError, match="HEARTBEAT_MAX_AGE_SECONDS must be >="):
        _settings(heartbeat_interval_seconds=60, heartbeat_max_age_seconds=59)
