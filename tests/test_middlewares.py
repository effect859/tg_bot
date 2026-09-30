from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.db.models import User
from app.services.users import register_or_update_user

TG_USER = TgUser(id=555, is_bot=False, first_name="Анна", username="anna", language_code="ru")


async def test_user_middleware_registers_user(session: AsyncSession) -> None:
    handler = AsyncMock(return_value="ok")
    data: dict[str, Any] = {"session": session, "event_from_user": TG_USER}

    result = await UserMiddleware()(handler, AsyncMock(), data)

    assert result == "ok"
    handler.assert_awaited_once()
    assert data["user"].tg_id == 555
    assert data["user"].first_name == "Анна"


async def test_user_middleware_drops_blocked_user(session: AsyncSession) -> None:
    user = await register_or_update_user(
        session, tg_id=555, username=None, first_name=None, language_code=None
    )
    user.is_blocked = True
    await session.flush()
    handler = AsyncMock()

    result = await UserMiddleware()(
        handler, AsyncMock(), {"session": session, "event_from_user": TG_USER}
    )

    assert result is None
    handler.assert_not_awaited()


async def test_db_middleware_commits_on_success(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    async def handler(event: Any, data: dict[str, Any]) -> None:
        data["session"].add(User(tg_id=777))

    await DbSessionMiddleware(sessionmaker)(handler, AsyncMock(), {})

    assert await session.scalar(select(User).where(User.tg_id == 777)) is not None


async def test_db_middleware_rolls_back_on_error(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    async def handler(event: Any, data: dict[str, Any]) -> None:
        data["session"].add(User(tg_id=888))
        await data["session"].flush()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await DbSessionMiddleware(sessionmaker)(handler, AsyncMock(), {})

    assert await session.scalar(select(User).where(User.tg_id == 888)) is None


async def test_db_middleware_allows_commit_inside_handler(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    async def handler(event: Any, data: dict[str, Any]) -> None:
        # Как приём файла: commit перед скачиванием, затем снова работа с БД.
        data["session"].add(User(tg_id=901))
        await data["session"].commit()
        data["session"].add(User(tg_id=902))

    await DbSessionMiddleware(sessionmaker)(handler, AsyncMock(), {})

    tg_ids = set(await session.scalars(select(User.tg_id).where(User.tg_id.in_([901, 902]))))
    assert tg_ids == {901, 902}


async def test_db_middleware_rolls_back_only_uncommitted_work(
    sessionmaker: async_sessionmaker[AsyncSession], session: AsyncSession
) -> None:
    async def handler(event: Any, data: dict[str, Any]) -> None:
        data["session"].add(User(tg_id=903))
        await data["session"].commit()
        data["session"].add(User(tg_id=904))
        await data["session"].flush()
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await DbSessionMiddleware(sessionmaker)(handler, AsyncMock(), {})

    tg_ids = set(await session.scalars(select(User.tg_id).where(User.tg_id.in_([903, 904]))))
    assert tg_ids == {903}
