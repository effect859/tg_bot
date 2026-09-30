import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.users import register_or_update_user

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Регистрирует/обновляет пользователя и передаёт его в хендлеры как ``user``.

    Должен стоять после DbSessionMiddleware. Апдейты заблокированных пользователей
    отбрасываются без ответа.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        session: AsyncSession = data["session"]
        user = await register_or_update_user(
            session,
            tg_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code,
        )
        if user.is_blocked:
            logger.info("Ignoring update from blocked user tg_id=%s", tg_user.id)
            return None

        data["user"] = user
        return await handler(event, data)
