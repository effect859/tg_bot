"""Восстановление обработки материалов: периодический проход по всем заказам.

Обычно материал обрабатывается в апдейте, где его прислали. Проход восстановления
подбирает то, что там не получилось или не случилось, без участия пользователя:

* RECEIVED старше RECEIVED_GRACE — очередь была занята, бот перезапустился до
  обработки, апдейт упал;
* PROCESSING дольше STALE_PROCESSING_AFTER — обработку прервали;
* FAILED с повторяемой причиной, пока не исчерпаны попытки.

Первый проход — сразу при запуске бота, дальше раз в RECOVERY_INTERVAL_SECONDS. За
проход — не больше RECOVERY_BATCH материалов, начиная с самых старых; если очередь
извлечения занята, проход заканчивается, остальное — в следующем. Срок обработки
не гарантируется: при большом числе материалов они ждут своей очереди.

Материал обрабатывается тем же process_material, что и в апдейте: двойной захват
исключают блокировка строки и номер попытки, своих блокировок здесь нет. Очередь
извлечения общая с апдейтами. Свежий PROCESSING после рестарта не трогается, пока не
истечёт STALE_PROCESSING_AFTER: при перекрытии экземпляров во время деплоя его ещё
может обрабатывать старый процесс.

Пользователю сообщается только окончательный исход — текст извлечён или повтора не
будет — после commit и не больше одного раза. Сбой отправки не влияет на обработку.
Если процесс упадёт между commit и отправкой, сообщения не будет: источник истины —
статус в БД.

Рассчитано на один экземпляр бота. При нескольких каждый выполняет свои проходы и
запускает свой дочерний процесс извлечения: один материал дважды не захватывается,
но одновременно может работать по процессу на экземпляр.
"""

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Order, OrderMaterial, User
from app.extraction.base import TextExtractor
from app.services.material_processing import (
    OutcomeKind,
    ProcessingOutcome,
    find_recoverable_materials,
    process_material,
)
from app.services.periodic import PeriodicPass
from app.services.storage.base import FileStorage

logger = logging.getLogger(__name__)

RECOVERY_INTERVAL_SECONDS = 60.0
RECOVERY_BATCH = 10

# (chat_id пользователя, исход) -> сообщение пользователю.
Notify = Callable[[int, ProcessingOutcome], Awaitable[None]]


async def run_recovery_pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: FileStorage,
    extractor: TextExtractor,
    notify: Notify,
    *,
    batch: int = RECOVERY_BATCH,
) -> list[ProcessingOutcome]:
    """Один проход: обработать до ``batch`` ожидающих материалов, сообщить итог."""
    async with sessionmaker() as session:
        material_ids = await find_recoverable_materials(session, limit=batch)

    outcomes: list[ProcessingOutcome] = []
    for material_id in material_ids:
        try:
            async with sessionmaker() as session:
                outcome = await process_material(session, storage, extractor, material_id)
                chat_id = await _chat_id(session, material_id) if _is_final(outcome) else None
        except Exception:
            # Материал остаётся как был (в худшем случае — в PROCESSING до признания
            # прерванным); остальные материалы прохода от него не зависят.
            logger.exception("Material recovery failed: id=%s", material_id)
            continue
        outcomes.append(outcome)
        if outcome.kind is OutcomeKind.BUSY:
            break
        if chat_id is not None:
            await _notify_quietly(notify, chat_id, outcome)
    return outcomes


class MaterialRecovery(PeriodicPass):
    """Периодический проход восстановления; первый — сразу после start().

    При остановке идущее извлечение снимает runner: дочерний процесс завершается
    вместе с задачей.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        storage: FileStorage,
        extractor: TextExtractor,
        notify: Notify,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        batch: int = RECOVERY_BATCH,
    ) -> None:
        super().__init__(name="material-recovery", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._storage = storage
        self._extractor = extractor
        self._notify = notify
        self._batch = batch

    async def run_pass(self) -> list[ProcessingOutcome]:
        return await run_recovery_pass(
            self._sessionmaker, self._storage, self._extractor, self._notify, batch=self._batch
        )


def _is_final(outcome: ProcessingOutcome) -> bool:
    """Сообщать ли исход: только окончательный и только по заказу, который ещё идёт."""
    if not outcome.order_active:
        return False
    return outcome.kind is OutcomeKind.PROCESSED or (
        outcome.kind is OutcomeKind.FAILED and not outcome.will_retry
    )


async def _chat_id(session: AsyncSession, material_id: int) -> int | None:
    stmt = (
        select(User.tg_id)
        .join(Order, Order.user_id == User.id)
        .join(OrderMaterial, OrderMaterial.order_id == Order.id)
        .where(OrderMaterial.id == material_id)
    )
    return await session.scalar(stmt)


async def _notify_quietly(notify: Notify, chat_id: int, outcome: ProcessingOutcome) -> None:
    try:
        await notify(chat_id, outcome)
    except Exception:
        # Пользователь заблокировал бота, сеть, лимиты Telegram: обработка уже
        # зафиксирована, повторной отправки нет.
        logger.warning(
            "Material recovery notification failed: id=%s", outcome.material_id, exc_info=True
        )
