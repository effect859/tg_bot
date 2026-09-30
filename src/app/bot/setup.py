from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import menu, start
from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware


def create_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession], storage: BaseStorage
) -> Dispatcher:
    dispatcher = Dispatcher(storage=storage)

    # Порядок важен: UserMiddleware использует сессию из DbSessionMiddleware.
    dispatcher.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dispatcher.update.outer_middleware(UserMiddleware())

    dispatcher.include_routers(start.router, menu.router)
    return dispatcher
