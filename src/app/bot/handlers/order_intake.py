"""Оформление заказа: создание, согласие, приём материалов, отмена.

Источник истины о шаге заказа — его статус в БД, FSM-состояния не используются.
Документ всегда прикрепляется к активному заказу отправителя; order_id из кнопок
проверяется на принадлежность пользователю. Правила приёма файлов — в домене и
сервисах, здесь только перевод ошибок в сообщения.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import (
    OrderAction,
    OrderCallback,
    cancel_order_keyboard,
    consent_keyboard,
    main_menu_keyboard,
)
from app.bot.telegram_files import FileDownloadError, FileTooLargeError, download_document
from app.db.models import Order, User
from app.domain.enums import OrderStatus
from app.domain.errors import (
    ActiveOrderExistsError,
    ConsentNotGivenError,
    InvalidStatusTransitionError,
    OrderDomainError,
    OrderNotFoundError,
)
from app.domain.materials import MAX_MATERIAL_SIZE_BYTES
from app.extraction.base import TextExtractor
from app.services.material_processing import process_pending_materials
from app.services.order_materials import (
    MaterialStorageError,
    ensure_can_add_material,
    store_material,
)
from app.services.orders import change_status, create_order, get_active_order, get_user_order
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

router = Router(name="order_intake")

S = OrderStatus


# --- Заказ ---------------------------------------------------------------------------


@router.message(F.text == texts.BTN_NEW_REPORT)
async def new_report(message: Message, session: AsyncSession, user: User) -> None:
    order = await get_active_order(session, user.id)
    if order is not None:
        await _remind_active_order(message, order)
        return

    try:
        order = await create_order(session, user_id=user.id)
    except ActiveOrderExistsError:
        # Параллельное нажатие успело создать заказ раньше.
        order = await get_active_order(session, user.id)
        if order is None:
            await message.answer(texts.ORDER_CREATE_FAILED)
        else:
            await _remind_active_order(message, order)
        return
    order = await change_status(session, order.id, S.WAITING_FOR_CONSENT)
    # Кнопки ссылаются на order_id: заказ должен существовать до того, как их увидят.
    await session.commit()
    await message.answer(texts.CONSENT_REQUEST, reply_markup=consent_keyboard(order.id))


async def _remind_active_order(message: Message, order: Order) -> None:
    if order.status is S.WAITING_FOR_CONSENT:
        await message.answer(texts.CONSENT_REQUEST, reply_markup=consent_keyboard(order.id))
    elif order.status is S.WAITING_FOR_FILES:
        await message.answer(
            texts.waiting_for_files(order), reply_markup=cancel_order_keyboard(order.id)
        )
    else:
        await message.answer(texts.active_order_exists(order))


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
    await message.answer(texts.material_accepted(label, upload_order))
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
