from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import menu, order_intake, start
from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.services.storage.base import FileStorage


def create_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: BaseStorage,
    file_storage: FileStorage,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=storage)
    # Доступно хендлерам как аргумент file_storage.
    dispatcher["file_storage"] = file_storage

    # Порядок важен: UserMiddleware использует сессию из DbSessionMiddleware.
    dispatcher.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dispatcher.update.outer_middleware(UserMiddleware())

    dispatcher.include_routers(start.router, menu.router, order_intake.router)
    return dispatcher
