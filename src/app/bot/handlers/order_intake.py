"""Оформление заказа: создание, согласие, приём материалов, завершение приёма, анализ, отмена.

Источник истины о шаге заказа — его статус в БД, FSM-состояния не используются.
Документ всегда прикрепляется к активному заказу отправителя; order_id из кнопок
проверяется на принадлежность пользователю. Правила приёма файлов и готовности
материалов — в домене и сервисах, здесь только перевод ошибок в сообщения.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    OrderAction,
    OrderCallback,
    analysis_failed_keyboard,
    cancel_order_keyboard,
    consent_keyboard,
    main_menu_keyboard,
    materials_keyboard,
)
from app.bot.reminders import remind_active_order
from app.bot.telegram_files import FileDownloadError, FileTooLargeError, download_document
from app.db.models import Order, User
from app.domain.enums import OrderStatus
from app.domain.errors import (
    ActiveOrderExistsError,
    AnalysisLimitReachedError,
    ConsentNotGivenError,
    InvalidStatusTransitionError,
    MaterialsNotReadyError,
    OrderDomainError,
    OrderNotFoundError,
)
from app.domain.materials import MAX_MATERIAL_SIZE_BYTES
from app.extraction.base import TextExtractor
from app.llm.base import LLMClient
from app.services.material_processing import process_pending_materials
from app.services.order_materials import (
    MaterialStorageError,
    ensure_can_add_material,
    finish_materials,
    store_material,
)
from app.services.orders import change_status, create_order, get_active_order, get_user_order
from app.services.requirements_analysis import (
    AnalysisOutcome,
    AnalysisOutcomeKind,
    analyze_requirements,
    restart_analysis,
)
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

router = Router(name="order_intake")

S = OrderStatus


# --- Заказ ---------------------------------------------------------------------------


@router.message(F.text == texts.BTN_NEW_REPORT)
async def new_report(message: Message, session: AsyncSession, user: User) -> None:
    order = await get_active_order(session, user.id)
    if order is not None:
        await remind_active_order(message, session, order)
        return

    try:
        order = await create_order(session, user_id=user.id)
    except ActiveOrderExistsError:
        # Параллельное нажатие успело создать заказ раньше.
        order = await get_active_order(session, user.id)
        if order is None:
            await message.answer(texts.ORDER_CREATE_FAILED)
        else:
            await remind_active_order(message, session, order)
        return
    order = await change_status(session, order.id, S.WAITING_FOR_CONSENT)
    # Кнопки ссылаются на order_id: заказ должен существовать до того, как их увидят.
    await session.commit()
    await message.answer(texts.CONSENT_REQUEST, reply_markup=consent_keyboard(order.id))


@router.callback_query(OrderCallback.filter(F.action == OrderAction.CONSENT))
async def give_consent(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    order = await _change_user_order(
        callback, session, user, callback_data.order_id, S.WAITING_FOR_FILES
    )
    if order is None:
        return
    await _send(callback, bot, texts.FILES_REQUEST, cancel_order_keyboard(order.id))


@router.callback_query(OrderCallback.filter(F.action == OrderAction.CANCEL))
async def cancel_order(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    order = await _change_user_order(callback, session, user, callback_data.order_id, S.CANCELLED)
    if order is None:
        return
    await _send(callback, bot, texts.order_cancelled(order))


async def _change_user_order(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    order_id: int,
    target: OrderStatus,
) -> Order | None:
    """Переход заказа пользователя через доменный сервис; None — отказ, ответ уже дан.

    Чужой заказ неотличим от несуществующего. Повторное нажатие кнопки упирается в
    карту переходов (например, CANCELLED -> CANCELLED запрещён) и ничего не меняет.
    """
    try:
        order = await get_user_order(session, user.id, order_id)
    except OrderNotFoundError:
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return None
    try:
        order = await change_status(session, order.id, target)
    except InvalidStatusTransitionError:
        # lock_order внутри change_status обновил order актуальным статусом.
        await callback.answer(texts.action_outdated(order), show_alert=True)
        return None
    await session.commit()
    await callback.answer()
    return order


@router.callback_query(OrderCallback.filter(F.action == OrderAction.FINISH_MATERIALS))
async def finish_order_materials(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
    llm_client: LLMClient | None = None,
) -> None:
    """«Материалы загружены»: WAITING_FOR_FILES -> ANALYZING_MATERIALS, затем анализ.

    Готовность материалов проверяет сервис под блокировкой заказа. При отказе
    транзакция откатывается до ответа в Telegram: блокировка заказа не держится во
    время сетевого запроса. Повторное и устаревшее нажатие упирается в карту переходов.
    Анализ запускается в этом же апдейте, если подключён LLM-клиент; то, что здесь не
    завершилось, доделывает проход восстановления анализа.
    """
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    try:
        completion = await finish_materials(session, order.id)
    except InvalidStatusTransitionError:
        # lock_order обновил order актуальным статусом. Текст — до rollback: после него
        # объекты сессии устаревают.
        if order.status is S.ANALYZING_MATERIALS:
            refusal = texts.materials_already_finished(order)
        else:
            refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except MaterialsNotReadyError as error:
        await session.rollback()
        await callback.answer(texts.materials_not_ready(error), show_alert=True)
        return
    order_id = completion.order.id
    await session.commit()
    await callback.answer()
    await _remove_keyboard(callback, bot)
    await _send(callback, bot, texts.materials_completed(completion))
    if llm_client is not None:
        await _run_analysis(callback, bot, session, llm_client, order_id)


@router.callback_query(OrderCallback.filter(F.action == OrderAction.RETRY_ANALYSIS))
async def retry_analysis(
    callback: CallbackQuery,
    callback_data: OrderCallback,
    session: AsyncSession,
    user: User,
    bot: Bot,
    llm_client: LLMClient | None = None,
) -> None:
    """«Повторить анализ»: FAILED -> ANALYZING_MATERIALS, затем анализ.

    Лимит вызовов модели и исходный статус проверяет сервис под блокировкой заказа:
    одновременные нажатия выполняются по очереди, второе получает отказ.
    """
    try:
        order = await get_user_order(session, user.id, callback_data.order_id)
    except OrderNotFoundError:
        await session.rollback()
        await callback.answer(texts.ORDER_NOT_FOUND, show_alert=True)
        return
    if llm_client is None:
        await session.rollback()
        await callback.answer(texts.ANALYSIS_UNAVAILABLE, show_alert=True)
        return
    try:
        await restart_analysis(session, order.id)
    except InvalidStatusTransitionError:
        # lock_order обновил order актуальным статусом; текст — до rollback.
        if order.status is S.ANALYZING_MATERIALS:
            refusal = texts.analysis_already_restarted(order)
        else:
            refusal = texts.action_outdated(order)
        await session.rollback()
        await callback.answer(refusal, show_alert=True)
        return
    except AnalysisLimitReachedError:
        await session.rollback()
        await callback.answer(texts.ANALYSIS_LIMIT_REACHED, show_alert=True)
        return
    order_id = order.id
    await session.commit()
    await callback.answer()
    await _remove_keyboard(callback, bot)
    await _run_analysis(callback, bot, session, llm_client, order_id)


async def _run_analysis(
    callback: CallbackQuery, bot: Bot, session: AsyncSession, llm: LLMClient, order_id: int
) -> None:
    await _send(callback, bot, texts.ANALYSIS_STARTED)
    try:
        outcome = await analyze_requirements(session, llm, order_id)
    except Exception:
        # Ошибка БД или непредвиденный сбой: попытка будет признана прерванной и
        # повторена проходом восстановления.
        logger.exception("Requirements analysis failed: order_id=%s", order_id)
        await _rollback_quietly(session)
        await _send(callback, bot, texts.ANALYSIS_DELAYED)
        return
    reply = analysis_reply(outcome)
    if reply is not None:
        text, markup = reply
        await _send(callback, bot, text, markup)


def analysis_reply(outcome: AnalysisOutcome) -> tuple[str, InlineKeyboardMarkup | None] | None:
    """Сообщение об исходе анализа; None — сообщать нечего (уже идёт, устарел и т. п.)."""
    match outcome.kind:
        case AnalysisOutcomeKind.COMPLETED if outcome.missing:
            # Уточнение данных начинается сразу: первый вопрос — первое незаполненное поле.
            text = f"{texts.analysis_completed(outcome)}\n\n{texts.question(outcome.missing[0])}"
            return text, cancel_order_keyboard(outcome.order_id)
        case AnalysisOutcomeKind.COMPLETED:
            return texts.analysis_completed(outcome), None
        case AnalysisOutcomeKind.RETRY_LATER:
            return texts.ANALYSIS_DELAYED, None
        case AnalysisOutcomeKind.FAILED:
            return (
                texts.analysis_failed(outcome.order_id, can_retry=outcome.can_retry),
                analysis_failed_keyboard(outcome.order_id, can_retry=outcome.can_retry),
            )
        case _:
            return None


async def _remove_keyboard(callback: CallbackQuery, bot: Bot) -> None:
    """Снимает кнопки с нажатого сообщения. Переход уже зафиксирован: сбой не важен."""
    message = callback.message
    if not isinstance(message, Message):
        return  # Сообщение недоступно боту (удалено или слишком старое).
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=None
        )
    except TelegramAPIError:
        logger.warning("Failed to remove keyboard: message_id=%s", message.message_id)


async def _send(
    callback: CallbackQuery, bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    # Сообщение с кнопкой может быть уже недоступно; в личном чате chat_id = id пользователя.
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    await bot.send_message(chat_id, text, reply_markup=markup or main_menu_keyboard())


# --- Материалы -----------------------------------------------------------------------


@router.message(F.document)
async def receive_document(
    message: Message,
    session: AsyncSession,
    user: User,
    bot: Bot,
    file_storage: FileStorage,
    text_extractor: TextExtractor | None = None,
) -> None:
    document = message.document
    assert document is not None  # гарантирует фильтр F.document
    filename = document.file_name or ""
    label = texts.file_label(filename)

    order = await get_active_order(session, user.id)
    if order is None:
        await message.answer(texts.NO_ACTIVE_ORDER)
        return
    order_id = order.id

    # Предварительная проверка: заведомо отклоняемое не скачиваем. Место не
    # резервируется — окончательная проверка в store_material.
    try:
        await ensure_can_add_material(
            session,
            order_id,
            filename=filename,
            mime_type=document.mime_type,
            size_bytes=document.file_size,
        )
    except OrderDomainError as exc:
        await _reject(message, order_id, label, exc)
        return
    # Во время скачивания не держим ни транзакцию, ни блокировки.
    await session.commit()

    try:
        data = await download_document(bot, document.file_id, max_size=MAX_MATERIAL_SIZE_BYTES)
    except FileTooLargeError:
        await message.answer(texts.material_failed(label, texts.MATERIAL_TOO_LARGE))
        return
    except FileDownloadError as exc:
        # Без traceback: исключения aiohttp содержат URL файла, а в нём — токен бота.
        logger.warning(
            "Material download failed: order_id=%s cause=%s",
            order_id,
            type(exc.__cause__).__name__,
        )
        await message.answer(texts.material_failed(label, texts.MATERIAL_DOWNLOAD_FAILED))
        return

    try:
        material = await store_material(
            session,
            file_storage,
            order_id,
            filename=filename,
            mime_type=document.mime_type,
            data=data,
        )
    except OrderDomainError as exc:
        await _reject(message, order_id, label, exc)
        return
    except MaterialStorageError:
        logger.exception("Material storage failed: order_id=%s", order_id)
        await message.answer(texts.material_failed(label, texts.MATERIAL_STORAGE_FAILED))
        return
    except SQLAlchemyError:
        logger.exception("Material registration failed: order_id=%s", order_id)
        await session.rollback()
        await message.answer(texts.material_failed(label, texts.MATERIAL_DB_FAILED))
        return

    upload_order, storage_key = material.upload_order, material.storage_key
    try:
        await session.commit()
    except Exception:
        # Исход неизвестен: commit мог завершиться на сервере. Файл не удаляем —
        # иначе зафиксированная строка может остаться без файла. Если строки нет,
        # файл по этому ключу — сирота; ключ и контекст — в логе для разбора.
        logger.exception(
            "Material commit outcome unknown, file kept: order_id=%s user_id=%s "
            "upload_order=%s storage_key=%s size_bytes=%s",
            order_id,
            user.id,
            upload_order,
            storage_key,
            len(data),
        )
        await _rollback_quietly(session)
        await message.answer(texts.material_failed(label, texts.MATERIAL_COMMIT_UNKNOWN))
        return
    # upload_order = число материалов заказа: материалы не удаляются.
    await message.answer(
        texts.material_accepted(label, upload_order), reply_markup=materials_keyboard(order_id)
    )
    if text_extractor is not None:
        await _process_materials(message, session, file_storage, text_extractor, order_id)


async def _process_materials(
    message: Message,
    session: AsyncSession,
    file_storage: FileStorage,
    text_extractor: TextExtractor,
    order_id: int,
) -> None:
    """Извлечение текста после приёма файла — в том же апдейте, без фоновых задач.

    Обрабатывается новый файл и материалы заказа, ждущие повтора. Сбой обработки
    не отменяет приём: материал и его файл остаются, статус — в БД.
    """
    try:
        outcomes = await process_pending_materials(session, file_storage, text_extractor, order_id)
    except Exception:
        # Ошибка БД или непредвиденный сбой. Материал, оставшийся в PROCESSING, будет
        # признан прерванным и обработан заново; файл не затрагивается.
        logger.exception("Material processing failed: order_id=%s", order_id)
        await _rollback_quietly(session)
        await message.answer(texts.MATERIAL_PROCESSING_DEFERRED)
        return
    for outcome in outcomes:
        text = texts.material_processing_result(outcome)
        if text is not None:
            await message.answer(text)


async def _reject(message: Message, order_id: int, label: str, error: OrderDomainError) -> None:
    markup = consent_keyboard(order_id) if isinstance(error, ConsentNotGivenError) else None
    await message.answer(
        texts.material_failed(label, texts.material_rejection(error)), reply_markup=markup
    )


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        logger.exception("Rollback after failed commit also failed")
