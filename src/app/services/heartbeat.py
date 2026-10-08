"""Heartbeat процесса бота: метка живости для Docker HEALTHCHECK (app.healthcheck).

Периодический проход (PeriodicPass, как остальные фоновые проходы; первый — сразу
после start()): короткая сессия с SELECT 1 — без блокировок строк и без долгой
транзакции, — затем атомарная запись метки (временный файл + os.replace). При ошибке
БД метка не обновляется: через HEARTBEAT_MAX_AGE_SECONDS healthcheck сообщит о
проблеме. В метке — только время, никаких данных пользователей.
"""

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.periodic import PeriodicPass

logger = logging.getLogger(__name__)


def write_marker(path: Path, timestamp: float) -> None:
    """Атомарно: метка либо прежняя, либо новая, но не обрезанная."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(f"{timestamp:.3f}\n", encoding="ascii")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Heartbeat(PeriodicPass):
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        path: Path,
        *,
        interval_seconds: float,
    ) -> None:
        super().__init__(name="heartbeat", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._path = path

    async def run_pass(self) -> list[Any]:
        try:
            async with self._sessionmaker() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            # Без текста исключения: в нём могут быть адрес и параметры подключения.
            logger.warning("Heartbeat database check failed: error=%s", type(exc).__name__)
            return []
        await asyncio.to_thread(write_marker, self._path, time.time())
        return []
