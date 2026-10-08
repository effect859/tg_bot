import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.setup import create_dispatcher
from app.config import get_settings, require_llm_api_key
from app.db.schema import SchemaMismatchError, ensure_schema_is_current
from app.db.session import create_engine, create_sessionmaker
from app.extraction.runner import SubprocessTextExtractor
from app.llm.anthropic import AnthropicLLMClient
from app.logging_setup import setup_logging
from app.services.heartbeat import Heartbeat
from app.services.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    # До создания подключений: без LLM бот не должен запускаться как работоспособный.
    llm_client = AnthropicLLMClient(api_key=require_llm_api_key(settings), model=settings.llm_model)

    engine = create_engine(settings)
    # До polling: на несовпадающей схеме бот не работает (миграции — отдельный шаг).
    try:
        revision = await ensure_schema_is_current(engine)
    except SchemaMismatchError as exc:
        logger.error("Bot is not started: %s", exc)
        await engine.dispose()
        raise SystemExit(1) from None
    except BaseException:
        await engine.dispose()
        raise
    logger.info("Database schema is current: revision=%s", revision)

    storage = RedisStorage.from_url(settings.redis_url)
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    text_extractor = SubprocessTextExtractor(
        memory_limit_bytes=settings.extraction_memory_limit_mib * 1024 * 1024
    )
    sessionmaker = create_sessionmaker(engine)
    dispatcher = create_dispatcher(
        sessionmaker,
        storage,
        LocalFileStorage(settings.storage_dir),
        text_extractor,
        llm_client,
    )

    heartbeat = Heartbeat(
        sessionmaker, settings.heartbeat_path, interval_seconds=settings.heartbeat_interval_seconds
    )

    logger.info("Bot is starting (polling)")
    heartbeat.start()
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await heartbeat.stop()
        await bot.session.close()
        await storage.close()
        await engine.dispose()
        logger.info("Bot stopped")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
