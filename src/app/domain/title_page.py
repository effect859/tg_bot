"""Титульный лист отчёта по практике: поля из подтверждённого ТЗ.

Модуль чистый (без БД, сети и LLM). Модель эти данные не видит и не дополняет:
поле, которого нет в ТЗ (кафедра, город, руководитель от организации), остаётся
пустым — рендерер выводит на его месте линию для заполнения от руки.
"""

from datetime import date
from typing import Any

# Поля титульного листа в порядке вывода. Ключи знает рендерер.
TITLE_PAGE_FIELDS: tuple[str, ...] = (
    "university",
    "faculty",
    "department",
    "practice_type",
    "practice_place",
    "topic",
    "practice_period",
    "student_name",
    "student_group",
    "specialty",
    "supervisor",
    "organization_supervisor",
    "city",
    "year",
)

# Текстовые поля, которые берутся из ТЗ как есть. Факультета, кафедры, города и
# руководителя от организации в ТЗ нет: они всегда пустые (заполняет студент).
_FROM_REQUIREMENTS = (
    "university",
    "practice_type",
    "practice_place",
    "topic",
    "student_name",
    "student_group",
    "specialty",
    "supervisor",
)


def build_title_page(requirements: dict[str, Any]) -> dict[str, str]:
    """Поля титульного листа (все ключи TITLE_PAGE_FIELDS; нет данных — пустая строка)."""
    page = dict.fromkeys(TITLE_PAGE_FIELDS, "")
    for name in _FROM_REQUIREMENTS:
        value = requirements.get(name)
        if isinstance(value, str):
            page[name] = " ".join(value.split())
    start = _date(requirements.get("practice_start"))
    end = _date(requirements.get("practice_end"))
    if start is not None and end is not None:
        page["practice_period"] = f"с {start:%d.%m.%Y} по {end:%d.%m.%Y}"
    if end is not None:
        page["year"] = str(end.year)
    return page


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
