from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr

    database_url: str = "postgresql+asyncpg://tg_bot:tg_bot@localhost:5432/tg_bot"
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"

    log_level: str = "INFO"

    storage_dir: Path = Path("var/storage")


@lru_cache
def get_settings() -> Settings:
    return Settings()
