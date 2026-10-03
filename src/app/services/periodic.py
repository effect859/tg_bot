"""Периодический проход в фоне процесса бота: первый — сразу после start().

Общая основа проходов восстановления (материалы, анализ материалов). Ошибка
прохода логируется и не останавливает цикл; отмена (остановка бота) — не ошибка.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)


class PeriodicPass(ABC):
    def __init__(self, *, name: str, interval_seconds: float) -> None:
        self._name = name
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @abstractmethod
    async def run_pass(self) -> Sequence[Any]:
        """Один проход; возвращает исходы с атрибутом ``kind`` (для лога)."""

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(f"{self._name} is already started")
        self._task = asyncio.create_task(self._run(), name=self._name)

    async def stop(self) -> None:
        """Отменяет проход и дожидается его завершения."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Отменили саму остановку — отмену не проглатываем.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    async def _run(self) -> None:
        while True:
            try:
                outcomes = await self.run_pass()
            except Exception:
                logger.exception("%s pass failed", self._name)
            else:
                if outcomes:
                    kinds = Counter(outcome.kind.value for outcome in outcomes)
                    logger.info("%s pass: %s", self._name, dict(kinds))
            await asyncio.sleep(self._interval)
