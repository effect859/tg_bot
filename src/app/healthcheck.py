"""Docker HEALTHCHECK процесса бота: свежа ли метка heartbeat.

    python -m app.healthcheck   # код 0 — метка свежая, 1 — нет

Метку пишет app.services.heartbeat.Heartbeat после успешного SELECT 1, поэтому
свежая метка означает: процесс жив, его цикл событий работает и БД доступна.
Модуль намеренно крошечный: не импортирует приложение и не требует ни БД, ни Redis,
ни Telegram, ни Anthropic. Путь и допустимый возраст метки берутся из тех же
переменных окружения, что и у бота (HEARTBEAT_PATH, HEARTBEAT_MAX_AGE_SECONDS).
"""

import math
import os
import sys
import time
from pathlib import Path

DEFAULT_HEARTBEAT_PATH = Path("var/heartbeat")
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 120.0
# Метка «из будущего» дальше этого — повреждена (часы одного контейнера не прыгают).
FUTURE_TOLERANCE_SECONDS = 5.0


def read_marker(path: Path) -> float | None:
    """Время из метки (Unix, секунды); None — метки нет или она повреждена."""
    try:
        raw = path.read_text(encoding="ascii").strip()
        timestamp = float(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def is_fresh(path: Path, max_age_seconds: float, *, now: float | None = None) -> bool:
    timestamp = read_marker(path)
    if timestamp is None:
        return False
    age = (time.time() if now is None else now) - timestamp
    return -FUTURE_TOLERANCE_SECONDS <= age <= max_age_seconds


def main() -> int:
    path = Path(os.environ.get("HEARTBEAT_PATH") or DEFAULT_HEARTBEAT_PATH)
    try:
        max_age = float(
            os.environ.get("HEARTBEAT_MAX_AGE_SECONDS") or DEFAULT_HEARTBEAT_MAX_AGE_SECONDS
        )
    except ValueError:
        print("unhealthy: invalid HEARTBEAT_MAX_AGE_SECONDS")
        return 1
    if is_fresh(path, max_age):
        print("healthy")
        return 0
    print("unhealthy: heartbeat missing, corrupted or stale")
    return 1


if __name__ == "__main__":
    sys.exit(main())
