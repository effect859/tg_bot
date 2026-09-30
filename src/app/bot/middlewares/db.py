from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия на апдейт: коммит при успехе, откат при исключении.

    Хендлер может сам сделать commit посреди апдейта (например, чтобы не держать
    транзакцию во время скачивания файла) и продолжить работу: сессия начнёт новую
    транзакцию, её зафиксирует или откатит middleware.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except BaseException:
                await session.rollback()
                raise
            await session.commit()
            return result
