from collections.abc import Callable
from functools import partial

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
)
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import menu, order_intake, plan, requirements_dialog, start
from app.bot.middlewares.db import DbSessionMiddleware
from app.bot.middlewares.user import UserMiddleware
from app.bot.stars import fetch_star_transactions
from app.domain.plan import PlanOutcome
from app.extraction.base import TextExtractor
from app.llm.base import LLMClient
from app.services.billing import RefundOutcome
from app.services.material_processing import ProcessingOutcome
from app.services.material_recovery import MaterialRecovery
from app.services.order_refunds import RefundRecovery
from app.services.payment_reconciliation import PaymentReconciliation
from app.services.periodic import PeriodicPass
from app.services.plan_generation import PlanRecovery
from app.services.report_completion import ReportCompletionRecovery
from app.services.report_delivery import DeliveryError, ReportDeliveryRecovery
from app.services.report_generation import ReportGenerationRecovery
from app.services.requirements_analysis import AnalysisOutcome, AnalysisRecovery
from app.services.storage.base import FileStorage


def create_dispatcher(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: BaseStorage,
    file_storage: FileStorage,
    text_extractor: TextExtractor | None = None,
    llm_client: LLMClient | None = None,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=storage)
    # Доступны хендлерам как аргументы file_storage, text_extractor и llm_client.
    dispatcher["file_storage"] = file_storage
    # Без извлекателя файлы только принимаются (так работают тесты приёма Stage 2.3).
    dispatcher["text_extractor"] = text_extractor
    # Без LLM-клиента заказ после завершения приёма остаётся ждать анализа (тесты 2.6).
    dispatcher["llm_client"] = llm_client

    # Порядок важен: UserMiddleware использует сессию из DbSessionMiddleware.
    dispatcher.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dispatcher.update.outer_middleware(UserMiddleware())

    # Роутер уточнения ТЗ — последним: его хендлер обычного текста не должен
    # перехватывать команды, кнопки меню и документы.
    dispatcher.include_routers(
        start.router, menu.router, order_intake.router, plan.router, requirements_dialog.router
    )
    if text_extractor is not None:
        _run_while_polling(
            dispatcher,
            lambda bot: MaterialRecovery(
                sessionmaker, file_storage, text_extractor, partial(notify_material_outcome, bot)
            ),
        )
    if llm_client is not None:
        _run_while_polling(
            dispatcher,
            lambda bot: AnalysisRecovery(
                sessionmaker, llm_client, partial(notify_analysis_outcome, bot)
            ),
        )
        _run_while_polling(
            dispatcher,
            lambda bot: PlanRecovery(sessionmaker, llm_client, partial(notify_plan_outcome, bot)),
        )
        # Генерация текста отчёта: уведомлений пользователю на этом этапе нет.
        _run_while_polling(
            dispatcher, lambda bot: ReportGenerationRecovery(sessionmaker, llm_client)
        )
    # Завершение отчёта (DOCX → хранилище → COMPLETED) не вызывает модель: работает
    # и без LLM-клиента.
    _run_while_polling(dispatcher, lambda bot: ReportCompletionRecovery(sessionmaker, file_storage))
    # Доставка готового DOCX пользователю — тоже без LLM-клиента.
    _run_while_polling(
        dispatcher,
        lambda bot: ReportDeliveryRecovery(
            sessionmaker, file_storage, partial(send_report_document, bot)
        ),
    )
    # Возврат кредитов за сбой генерации после оплаты; уведомление — после commit.
    _run_while_polling(
        dispatcher, lambda bot: RefundRecovery(sessionmaker, partial(notify_refund_outcome, bot))
    )
    # Сверка платежей Stars: зачисляет кредиты, если successful_payment не обработан.
    _run_while_polling(
        dispatcher,
        lambda bot: PaymentReconciliation(sessionmaker, partial(fetch_star_transactions, bot)),
    )
    return dispatcher


def _run_while_polling(dispatcher: Dispatcher, create: Callable[[Bot], PeriodicPass]) -> None:
    """Фоновый проход работает, пока работает polling.

    Запускается в startup (Bot известен только там), останавливается в shutdown — до
    закрытия сессии бота и пула БД в main_bot.
    """
    started: list[PeriodicPass] = []

    async def on_startup(bot: Bot) -> None:
        periodic = create(bot)
        periodic.start()
        started.append(periodic)

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


async def notify_analysis_outcome(bot: Bot, chat_id: int, outcome: AnalysisOutcome) -> None:
    """Сообщение пользователю об исходе фонового анализа материалов."""
    reply = order_intake.analysis_reply(outcome)
    if reply is not None:
        text, markup = reply
        await bot.send_message(chat_id, text, reply_markup=markup)


async def notify_refund_outcome(bot: Bot, chat_id: int, outcome: RefundOutcome) -> None:
    """Сообщение о возврате кредитов: только после commit возврата (best-effort)."""
    balance = outcome.balance if outcome.balance is not None else 0
    await bot.send_message(chat_id, texts.order_refunded(outcome.order_id, outcome.amount, balance))


async def notify_plan_outcome(bot: Bot, chat_id: int, outcome: PlanOutcome) -> None:
    """Сообщение пользователю об исходе фонового составления плана."""
    reply = plan.plan_reply(outcome)
    if reply is not None:
        text, markup = reply
        await bot.send_message(chat_id, text, reply_markup=markup)


TELEGRAM_FORBIDDEN = "forbidden"
TELEGRAM_BAD_REQUEST = "bad_request"
TELEGRAM_TOO_LARGE = "too_large"
TELEGRAM_ERROR = "telegram_error"


async def send_report_document(bot: Bot, chat_id: int, data: bytes, filename: str) -> None:
    """ReportSender для фоновой доставки: документ в чат пользователя.

    Ошибки aiogram переводятся в DeliveryError: только код, без текста исключения
    (в нём может оказаться описание запроса) и без содержимого отчёта.
    """
    document = BufferedInputFile(data, filename=filename)
    try:
        await bot.send_document(chat_id, document, caption=texts.REPORT_DELIVERED)
    except TelegramAPIError as exc:
        raise _delivery_error(exc) from None


def _delivery_error(exc: TelegramAPIError) -> DeliveryError:
    # TelegramEntityTooLarge — подкласс TelegramNetworkError: проверяется раньше.
    if isinstance(exc, TelegramEntityTooLarge):
        return DeliveryError(TELEGRAM_TOO_LARGE, temporary=False)
    if isinstance(exc, TelegramForbiddenError):
        # Бот заблокирован пользователем: повтор бесполезен.
        return DeliveryError(TELEGRAM_FORBIDDEN, temporary=False)
    if isinstance(exc, TelegramBadRequest | TelegramNotFound):
        return DeliveryError(TELEGRAM_BAD_REQUEST, temporary=False)
    # TelegramRetryAfter, TelegramNetworkError, TelegramServerError и прочие —
    # повтор в следующем проходе (число попыток ограничено).
    return DeliveryError(TELEGRAM_ERROR, temporary=True)
