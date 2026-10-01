from functools import partial

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, start
from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.extraction.base import TextExtractor
from app.services.material_processing import ProcessingOutcome
from app.services.material_recovery import MaterialRecovery
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
    if text_extractor is not None:
        _setup_material_recovery(dispatcher, sessionmaker, file_storage, text_extractor)
    return dispatcher


def _setup_material_recovery(
    dispatcher: Dispatcher,
    sessionmaker: async_sessionmaker[AsyncSession],
    file_storage: FileStorage,
    text_extractor: TextExtractor,
) -> None:
    """Восстановление обработки материалов работает, пока работает polling.

    Запускается в startup (Bot известен только там), останавливается в shutdown — до
    закрытия сессии бота и пула БД в main_bot.
    """
    started: list[MaterialRecovery] = []

    async def on_startup(bot: Bot) -> None:
        recovery = MaterialRecovery(
            sessionmaker, file_storage, text_extractor, partial(notify_material_outcome, bot)
        )
        recovery.start()
        started.append(recovery)

    async def on_shutdown() -> None:
        while started:
            await started.pop().stop()

    dispatcher.startup.register(on_startup)
    dispatcher.shutdown.register(on_shutdown)


async def notify_material_outcome(bot: Bot, chat_id: int, outcome: ProcessingOutcome) -> None:
    """Сообщение пользователю об исходе фоновой обработки материала."""
    text = texts.material_processing_result(outcome)
    if text is not None:
        await bot.send_message(chat_id, text)
