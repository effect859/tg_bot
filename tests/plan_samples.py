"""Заготовки для тестов плана: ответ модели и заказ в PREPARING_PLAN с подтверждённым ТЗ."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderMaterialText
from app.domain.enums import OrderStatus
from app.services import order_materials as materials
from app.services import orders
from app.services.users import register_or_update_user
from dialog_samples import FULL
from fake_llm import result

S = OrderStatus

MATERIAL = (
    "Методические указания по практике. Отчёт содержит введение, три главы, "
    "заключение и список источников."
)
# ТЗ с персональными полями: они не должны уйти модели.
REQUIREMENTS: dict[str, Any] = {
    **FULL,
    "supervisor": "Петров Пётр Петрович",
    "student_group": "БИ-21-1",
}
PERSONAL_VALUES = ("Иванов Иван Иванович", "НИУ ВШЭ", "Петров Пётр Петрович", "БИ-21-1")


def section(
    title: str,
    pages: int | None = 5,
    *,
    description: str = "Что раскрыть.",
    requirements: list[str] | None = None,
    subsections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "requirements": requirements or [],
        "target_pages": pages,
        "subsections": subsections or [],
    }


def subsection(title: str, pages: int | None = None, description: str = "") -> dict[str, Any]:
    return {"title": title, "description": description, "target_pages": pages}


def plan_payload(*sections: dict[str, Any]) -> dict[str, Any]:
    return {
        "sections": list(sections)
        or [
            # Сумма 25 — объём ТЗ FULL; каждый раздел не больше MAX_SECTION_PAGES (8).
            section("Введение", 3),
            section(
                "Характеристика организации",
                8,
                requirements=["Описать структуру отдела продаж"],
                subsections=[subsection("Общие сведения", 3), subsection("Структура", 5)],
            ),
            section("Анализ бизнес-процессов", 8),
            section("Заключение", 6),
        ]
    }


def plan_result(*sections: dict[str, Any]) -> Any:
    return result(json.dumps(plan_payload(*sections), ensure_ascii=False))


GOOD_PLAN = plan_result()


async def preparing_order(
    session: AsyncSession,
    *,
    tg_id: int = 1,
    content: dict[str, Any] | None = None,
    material: str = MATERIAL,
) -> int:
    """Заказ в PREPARING_PLAN: материал с текстом, подтверждённое ТЗ v1 (зафиксировано)."""
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
        order = await orders.change_status(session, order.id, status)
    item = await materials.add_material(
        session, order.id, filename="secret-file-name.txt", mime_type=None, size_bytes=10
    )
    await materials.mark_processing(session, item.id)
    await materials.mark_processed(session, item.id)
    session.add(
        OrderMaterialText(
            material_id=item.id, text=material, char_count=len(material), extractor="t"
        )
    )
    await session.flush()
    await materials.finish_materials(session, order.id)
    version = await orders.create_requirements_version(
        session, order.id, REQUIREMENTS if content is None else content
    )
    await orders.change_status(session, order.id, S.WAITING_FOR_DATA_CONFIRMATION)
    await orders.confirm_requirements_version(session, order.id, version.id)
    await orders.change_status(session, order.id, S.PREPARING_PLAN)
    order_id = order.id
    await session.commit()
    return order_id
