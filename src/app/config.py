from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.healthcheck import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    DEFAULT_HEARTBEAT_PATH,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr

    database_url: str = "postgresql+asyncpg://tg_bot:tg_bot@localhost:5432/tg_bot"
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"

    log_level: str = "INFO"

    storage_dir: Path = Path("var/storage")

    # RLIMIT_AS дочернего процесса извлечения текста, МиБ (только Linux; 0 — без лимита).
    # Экспериментальное значение: подобрано на синтетических файлах.
    extraction_memory_limit_mib: int = 1024

    # LLM (анализ материалов). Ключ необязателен для миграций и тестов, но бот без него
    # не запускается: см. require_llm_api_key.
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"

    # Heartbeat: метка живости для Docker HEALTHCHECK (python -m app.healthcheck).
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH
    heartbeat_interval_seconds: float = Field(default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS, gt=0)
    heartbeat_max_age_seconds: float = Field(default=DEFAULT_HEARTBEAT_MAX_AGE_SECONDS, gt=0)

    @model_validator(mode="after")
    def _check_heartbeat(self) -> "Settings":
        # Метка обновляется раз в interval: допустимый возраст не может быть меньше.
        if self.heartbeat_max_age_seconds < self.heartbeat_interval_seconds:
            raise ValueError("HEARTBEAT_MAX_AGE_SECONDS must be >= HEARTBEAT_INTERVAL_SECONDS")
        return self


class ConfigurationError(Exception):
    """Настройки не позволяют запустить приложение."""


def require_llm_api_key(settings: Settings) -> str:
    """Ключ Anthropic для бота. Raises ConfigurationError, если он не задан."""
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key.strip():
        raise ConfigurationError(
            "ANTHROPIC_API_KEY is not set: the bot cannot analyze materials without the LLM"
        )
    return key.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
