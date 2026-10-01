from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import menu, order_intake, start
from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.extraction.base import TextExtractor
from app.services.storage.base import FileStorage


def create_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: BaseStorage,
    file_storage: FileStorage,
    text_extractor: TextExtractor | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=storage)
    # Доступны хендлерам как аргументы file_storage и text_extractor.
    dispatcher["file_storage"] = file_storage
    # Без извлекателя файлы только принимаются (так работают тесты приёма Stage 2.3).
    dispatcher["text_extractor"] = text_extractor

    # Порядок важен: UserMiddleware использует сессию из DbSessionMiddleware.
    dispatcher.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dispatcher.update.outer_middleware(UserMiddleware())

    dispatcher.include_routers(start.router, menu.router, order_intake.router)
    return dispatcher
