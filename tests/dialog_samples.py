"""Заготовки для тестов уточнения ТЗ: содержимое черновика и заказ на нужном шаге."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderRequirementsVersion
from app.domain.enums import OrderStatus
from app.domain.schemas import PracticeReportRequirements
from app.services import orders
from app.services.users import register_or_update_user

S = OrderStatus

# Как после анализа: вуз и ФИО у модели не запрашиваются, необязательные поля найдены.
AFTER_ANALYSIS: dict[str, Any] = PracticeReportRequirements(
    topic="Анализ бизнес-процессов отдела продаж",
    practice_type="производственная",
    practice_place="ООО «Ёлка»",
    practice_start="2026-07-01",
    practice_end="2026-07-28",
    specialty="Бизнес-информатика",
    target_pages=25,
    extra_requirements=["Оформление по ГОСТ 7.32"],
).model_dump(mode="json")

FULL: dict[str, Any] = {
    **AFTER_ANALYSIS,
    "university": "НИУ ВШЭ",
    "student_name": "Иванов Иван Иванович",
}

EMPTY: dict[str, Any] = PracticeReportRequirements().model_dump(mode="json")


async def order_with_draft(
    session: AsyncSession,
    content: dict[str, Any],
    *,
    status: OrderStatus = S.WAITING_FOR_MISSING_DATA,
    tg_id: int = 1,
    with_draft: bool = True,
) -> int:
    """Заказ после анализа с черновиком ТЗ v1 на шаге ``status`` (зафиксировано)."""
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for step in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES, S.ANALYZING_MATERIALS):
        order = await orders.change_status(session, order.id, step)
    if with_draft:
        await orders.create_requirements_version(session, order.id, content)
    order = await orders.change_status(session, order.id, S.WAITING_FOR_MISSING_DATA)
    if status is not S.WAITING_FOR_MISSING_DATA:
        order = await orders.change_status(session, order.id, status)
    order_id = order.id
    await session.commit()
    return order_id


async def fresh_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def versions(session: AsyncSession, order_id: int) -> list[OrderRequirementsVersion]:
    found = await orders.get_requirements_versions(session, order_id)
    for version in found:
        await session.refresh(version)
    return found
