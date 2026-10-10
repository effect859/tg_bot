"""Уточнение и подтверждение ТЗ: ответы на вопросы, изменение поля, подтверждение.

Источник истины — статус заказа и черновик ТЗ в БД: текущий вопрос вычисляется
из черновика, отдельного состояния диалога нет. Черновик один (последняя версия
в статусе DRAFT), новые версии не создаются. Каждая операция блокирует строку
заказа, поэтому одновременные ответы, изменения, подтверждения и отмена
выполняются по очереди. Коммит — забота вызывающего кода.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderRequirementsVersion
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    OrderNotFoundError,
    RequirementsChangedError,
    RequirementsVolumeConsentError,
    VersionNotFoundError,
)
from app.domain.plan import split_volume, unsupported_volume
from app.domain.requirements import missing_fields
from app.domain.requirements_dialog import (
    AnswerError,
    apply_answer,
    clear_for_edit,
    content_digest,
    current_field,
    parse_answer,
)
from app.services.orders import (
    change_status,
    confirm_requirements_version,
    get_requirements_versions,
    lock_order,
    update_requirements_draft,
)

S = OrderStatus


@dataclass(frozen=True)
class RequirementsDialog:
    """Состояние уточнения ТЗ: статус заказа и содержимое черновика (копия)."""

    order_id: int
    status: OrderStatus
    content: dict[str, Any]

    @property
    def field(self) -> str | None:
        """Поле текущего вопроса; None — обязательные поля заполнены."""
        return current_field(self.content)

    @property
    def digest(self) -> str:
        return content_digest(self.content)


class AnswerOutcomeKind(StrEnum):
    # Ответ записан, есть следующий вопрос.
    ACCEPTED = "accepted"
    # Записан последний ответ: заказ ждёт подтверждения ТЗ.
    COMPLETED = "completed"
    # Ответ записан, но даты противоречили друг другу: обе спрашиваются заново.
    DATES_RESET = "dates_reset"
    # Ответ не прошёл проверку: черновик не изменён.
    REJECTED = "rejected"
    # Заказ не ждёт ответа (другой шаг, отменён и т. п.): ничего не записано.
    NOT_EXPECTED = "not_expected"


@dataclass(frozen=True)
class AnswerOutcome:
    kind: AnswerOutcomeKind
    # Поле, на вопрос о котором пришёл ответ.
    field: str | None = None
    # Записанное значение (даты — в ISO).
    value: str | None = None
    error: AnswerError | None = None
    # Состояние после ответа; None — NOT_EXPECTED.
    dialog: RequirementsDialog | None = None


async def get_dialog(session: AsyncSession, order_id: int) -> RequirementsDialog:
    """Текущее состояние уточнения ТЗ без блокировки — для напоминаний и экранов.

    Raises OrderNotFoundError, VersionNotFoundError — у заказа нет черновика ТЗ.
    """
    order = await session.get(Order, order_id, populate_existing=True)
    if order is None:
        raise OrderNotFoundError(order_id)
    draft = await _draft(session, order_id)
    return RequirementsDialog(order_id, order.status, dict(draft.content))


async def submit_answer(session: AsyncSession, order_id: int, text: str) -> AnswerOutcome:
    """Ответ на текущий вопрос: проверка, запись в черновик, переход к подтверждению.

    Raises VersionNotFoundError — заказ ждёт ответа, но черновика ТЗ нет.
    """
    order = await lock_order(session, order_id)
    if order.status is not S.WAITING_FOR_MISSING_DATA:
        return AnswerOutcome(AnswerOutcomeKind.NOT_EXPECTED)
    draft = await _draft(session, order_id)
    field = current_field(draft.content)
    if field is None:
        # Всё уже заполнено: вопроса нет, текст не записывается.
        await change_status(session, order_id, S.WAITING_FOR_DATA_CONFIRMATION)
        return AnswerOutcome(AnswerOutcomeKind.COMPLETED, dialog=_dialog(order_id, draft))

    parsed = parse_answer(field, text)
    if isinstance(parsed, AnswerError):
        return AnswerOutcome(
            AnswerOutcomeKind.REJECTED,
            field=field,
            error=parsed,
            dialog=RequirementsDialog(order_id, order.status, dict(draft.content)),
        )

    content, dates_reset = apply_answer(draft.content, field, parsed)
    draft = await update_requirements_draft(session, order_id, draft.id, content)
    if dates_reset:
        kind = AnswerOutcomeKind.DATES_RESET
    elif current_field(draft.content) is None:
        await change_status(session, order_id, S.WAITING_FOR_DATA_CONFIRMATION)
        kind = AnswerOutcomeKind.COMPLETED
    else:
        kind = AnswerOutcomeKind.ACCEPTED
    return AnswerOutcome(kind, field=field, value=parsed, dialog=_dialog(order_id, draft))


async def start_edit(session: AsyncSession, order_id: int, field: str) -> RequirementsDialog:
    """«Изменить поле»: поле очищается, заказ снова ждёт ответа — вопрос именно о нём.

    Raises InvalidStatusTransitionError (заказ не на подтверждении ТЗ),
    RequirementFieldNotEditableError, VersionNotFoundError.
    """
    order = await lock_order(session, order_id)
    if order.status is not S.WAITING_FOR_DATA_CONFIRMATION:
        raise InvalidStatusTransitionError(order.status, S.WAITING_FOR_MISSING_DATA)
    draft = await _draft(session, order_id)
    content = clear_for_edit(draft.content, field)
    draft = await update_requirements_draft(session, order_id, draft.id, content)
    await change_status(session, order_id, S.WAITING_FOR_MISSING_DATA)
    return _dialog(order_id, draft)


async def confirm_requirements(
    session: AsyncSession, order_id: int, digest: str, *, without_volume: bool = False
) -> OrderRequirementsVersion:
    """Подтверждение ТЗ, которое видел пользователь: черновик → CONFIRMED, заказ → PREPARING_PLAN.

    ``digest`` — отпечаток содержимого с экрана подтверждения: изменившееся с тех
    пор ТЗ не подтверждается. ``without_volume`` — согласие продолжить без объёма из
    материалов, который план не выполнит; нужно ровно тогда, когда такой объём есть.
    Черновик, сохранённый до поля unsupported_target_pages (объём в target_pages),
    при согласии приводится к нему. Всё — одной транзакцией вызывающего кода: при
    ошибке откат не оставляет частично изменённого черновика.

    Raises InvalidStatusTransitionError (заказ не на подтверждении ТЗ),
    RequirementsChangedError, RequirementsVolumeConsentError, VersionNotFoundError.
    """
    order = await lock_order(session, order_id)
    if order.status is not S.WAITING_FOR_DATA_CONFIRMATION:
        raise InvalidStatusTransitionError(order.status, S.PREPARING_PLAN)
    draft = await _draft(session, order_id)
    # Отпечаток — от того, что видел пользователь: до переноса объёма.
    if content_digest(draft.content) != digest or missing_fields(draft.content):
        raise RequirementsChangedError(order_id)
    if without_volume != (unsupported_volume(draft.content) is not None):
        raise RequirementsVolumeConsentError(order_id)
    split = split_volume(draft.content)
    if split != draft.content:
        draft = await update_requirements_draft(session, order_id, draft.id, split)
    version = await confirm_requirements_version(session, order_id, draft.id)
    await change_status(session, order_id, S.PREPARING_PLAN)
    return version


async def _draft(session: AsyncSession, order_id: int) -> OrderRequirementsVersion:
    """Черновик ТЗ — последняя версия, и она должна быть черновиком."""
    versions = await get_requirements_versions(session, order_id)
    if not versions or versions[-1].status is not VersionStatus.DRAFT:
        raise VersionNotFoundError(f"Order {order_id} has no requirements draft")
    return versions[-1]


def _dialog(order_id: int, draft: OrderRequirementsVersion) -> RequirementsDialog:
    # Статус — по итогу операции: следующий вопрос или подтверждение.
    content = dict(draft.content)
    status = (
        S.WAITING_FOR_MISSING_DATA if missing_fields(content) else S.WAITING_FOR_DATA_CONFIRMATION
    )
    return RequirementsDialog(order_id, status, content)
