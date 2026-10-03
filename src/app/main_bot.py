import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.setup import create_dispatcher
from app.config import get_settings, require_llm_api_key
from app.db.session import create_engine, create_sessionmaker
from app.extraction.runner import SubprocessTextExtractor
from app.llm.anthropic import AnthropicLLMClient
from app.logging_setup import setup_logging
from app.services.storage.local import LocalFileStorage

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    # До создания подключений: без LLM бот не должен запускаться как работоспособный.
    llm_client = AnthropicLLMClient(api_key=require_llm_api_key(settings), model=settings.llm_model)

    engine = create_engine(settings)
    storage = RedisStorage.from_url(settings.redis_url)
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    text_extractor = SubprocessTextExtractor(
        memory_limit_bytes=settings.extraction_memory_limit_mib * 1024 * 1024
    )
    dispatcher = create_dispatcher(
        create_sessionmaker(engine),
        storage,
        LocalFileStorage(settings.storage_dir),
        text_extractor,
        llm_client,
    )

    logger.info("Bot is starting (polling)")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await storage.close()
        await engine.dispose()
        logger.info("Bot stopped")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
