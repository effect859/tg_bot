"""План отчёта: что уходит модели, схема её ответа, проверка и распределение страниц.

Модуль чистый (без БД, Telegram и LLM-клиента). Модель только предлагает разделы;
что принять, как распределить страницы и в каком виде сохранить, решает этот
модуль. Итог всегда проходит PlanContent — ту же схему, что проверяет сохранение
версии плана.
"""

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any

from pydantic import ValidationError

from app.domain.enums import OrderStatus
from app.domain.requirements import MAX_TARGET_PAGES, AnalysisInput, normalize_whitespace
from app.domain.schemas import PlanContent

# Поля подтверждённого ТЗ, которые нужны для плана. ФИО, вуз, руководитель и группа
# на структуру плана не влияют и модели не передаются.
PLAN_REQUIREMENT_FIELDS: tuple[str, ...] = (
    "topic",
    "practice_type",
    "practice_place",
    "practice_start",
    "practice_end",
    "specialty",
    "target_pages",
    "extra_requirements",
)
NOT_FOR_PLAN: frozenset[str] = frozenset(
    {"student_name", "university", "supervisor", "student_group"}
)

# Генерация плана: попыток в одном запуске и вызовов модели на заказ за всё время.
MAX_PLAN_ATTEMPTS = 3
MAX_PLAN_LLM_CALLS = 6

MIN_SECTIONS = 3
MAX_SECTIONS = 15
MAX_SUBSECTIONS = 10
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 1000
MAX_REQUIREMENTS = 10
MAX_REQUIREMENT_CHARS = 300
MAX_SECTION_PAGES = MAX_TARGET_PAGES


# --- Вход модели -------------------------------------------------------------------


@dataclass(frozen=True)
class PlanInput:
    """Что уходит модели: поля подтверждённого ТЗ (без персональных) и материалы."""

    requirements: dict[str, Any]
    materials: AnalysisInput


def plan_requirements(content: dict[str, Any]) -> dict[str, Any]:
    """Поля ТЗ для модели: только PLAN_REQUIREMENT_FIELDS и только заполненные."""
    return {
        name: content[name]
        for name in PLAN_REQUIREMENT_FIELDS
        if content.get(name) not in (None, "", [])
    }


# --- Схема ответа модели -----------------------------------------------------------


def _object(properties: dict[str, Any], description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    if description:
        schema["description"] = description
    return schema


_PAGES = {
    "anyOf": [{"type": "integer"}, {"type": "null"}],
    "description": "Примерный объём в страницах или null.",
}

# Два уровня (раздел и подраздел) без рекурсии. Количество и длины в схеме не
# задаются — structured outputs их не поддерживают; их проверяет validate_plan.
PLAN_JSON_SCHEMA: dict[str, Any] = _object(
    {
        "sections": {
            "type": "array",
            "description": f"Разделы отчёта по порядку: от {MIN_SECTIONS} до {MAX_SECTIONS}.",
            "items": _object(
                {
                    "title": {"type": "string", "description": "Название раздела."},
                    "description": {
                        "type": "string",
                        "description": "Что раскрыть в разделе (кратко).",
                    },
                    "requirements": {
                        "type": "array",
                        "description": f"Требования из материалов, до {MAX_REQUIREMENTS}.",
                        "items": {"type": "string"},
                    },
                    "target_pages": _PAGES,
                    "subsections": {
                        "type": "array",
                        "description": f"Подразделы, до {MAX_SUBSECTIONS}; без вложенности.",
                        "items": _object(
                            {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "target_pages": _PAGES,
                            }
                        ),
                    },
                }
            ),
        }
    }
)


# --- Проверка ответа ---------------------------------------------------------------


class InvalidPlanResponse(Exception):
    """Ответ модели не принят (структура, ограничения): повтор имеет смысл."""


def validate_plan(raw: Any, target_pages: int | None) -> dict[str, Any]:
    """Проверенный план в формате content версии плана (PlanContent).

    Страницы: при заданном в ТЗ объёме распределяются детерминированно
    (distribute_pages), иначе сохраняются значения модели. Разделов больше, чем
    страниц в ТЗ, — план не принимается: по странице на раздел в объём не уложить.

    Raises InvalidPlanResponse.
    """
    if not isinstance(raw, dict) or set(raw) != {"sections"}:
        raise InvalidPlanResponse("expected an object with 'sections' only")
    items = raw["sections"]
    if not isinstance(items, list) or not MIN_SECTIONS <= len(items) <= MAX_SECTIONS:
        raise InvalidPlanResponse("section count out of range")

    sections = [_section(item) for item in items]
    _check_unique([s["title"] for s in sections], "section")

    if target_pages is not None:
        if target_pages < len(sections):
            raise InvalidPlanResponse("more sections than target pages")
        pages = distribute_pages([s["target_pages"] for s in sections], target_pages)
        for section, section_pages in zip(sections, pages, strict=True):
            section["target_pages"] = section_pages
            _fit_subsections(section)
    section_pages = [s["target_pages"] for s in sections]
    total = sum(section_pages) if all(p is not None for p in section_pages) else None

    try:
        plan = PlanContent.model_validate({"sections": sections, "total_pages": total})
    except ValidationError as exc:  # не должно случаться после проверок выше
        raise InvalidPlanResponse("plan schema rejected the result") from exc
    return plan.model_dump(mode="json")


def _section(item: Any) -> dict[str, Any]:
    keys = {"title", "description", "requirements", "target_pages", "subsections"}
    if not isinstance(item, dict) or set(item) != keys:
        raise InvalidPlanResponse("section has unexpected structure")
    requirements = item["requirements"]
    if not isinstance(requirements, list) or len(requirements) > MAX_REQUIREMENTS:
        raise InvalidPlanResponse("section requirements invalid")
    subsections = item["subsections"]
    if not isinstance(subsections, list) or len(subsections) > MAX_SUBSECTIONS:
        raise InvalidPlanResponse("subsection count out of range")
    subs = [_subsection(sub) for sub in subsections]
    _check_unique([s["title"] for s in subs], "subsection")
    return {
        "title": _text(item["title"], 1, MAX_TITLE_CHARS),
        "description": _text(item["description"], 0, MAX_DESCRIPTION_CHARS),
        "requirements": [_text(r, 1, MAX_REQUIREMENT_CHARS) for r in requirements],
        "target_pages": _pages(item["target_pages"]),
        "subsections": subs,
    }


def _subsection(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != {"title", "description", "target_pages"}:
        raise InvalidPlanResponse("subsection has unexpected structure")
    return {
        "title": _text(item["title"], 1, MAX_TITLE_CHARS),
        "description": _text(item["description"], 0, MAX_DESCRIPTION_CHARS),
        "requirements": [],
        "target_pages": _pages(item["target_pages"]),
        "subsections": [],
    }


def _text(value: Any, min_chars: int, max_chars: int) -> str:
    if not isinstance(value, str):
        raise InvalidPlanResponse("text field is not a string")
    text = normalize_whitespace(value)
    if not min_chars <= len(text) <= max_chars or not text.isprintable():
        raise InvalidPlanResponse("text field length or characters invalid")
    return text


def _pages(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_SECTION_PAGES:  # bool — не число
        raise InvalidPlanResponse("pages invalid")
    return value


def _check_unique(titles: list[str], level: str) -> None:
    folded = [title.casefold() for title in titles]
    if len(set(folded)) != len(folded):
        raise InvalidPlanResponse(f"duplicate {level} titles")


def _fit_subsections(section: dict[str, Any]) -> None:
    """Страницы подразделов — внутри страниц раздела, иначе не указываются."""
    subs = section["subsections"]
    if not subs:
        return
    weights = [sub["target_pages"] for sub in subs]
    if any(w is None for w in weights) or section["target_pages"] < len(subs):
        for sub in subs:
            sub["target_pages"] = None
        return
    for sub, pages in zip(subs, distribute_pages(weights, section["target_pages"]), strict=True):
        sub["target_pages"] = pages


def distribute_pages(weights: list[int | None], total: int) -> list[int]:
    """Целые страницы по разделам: каждому не меньше 1, в сумме ровно ``total``.

    Пропорционально весам (страницам, предложенным моделью); раздел без веса
    получает средний вес остальных, без весов вообще — поровну. Метод наибольших
    остатков: целые части долей (не меньше 1), недостающие страницы — разделам с
    наибольшей дробной частью, лишние (из-за минимума 1) снимаются с самых
    «переплаченных» разделов. Равенство решает порядок разделов. Только точная
    арифметика (Fraction): один и тот же вход — один и тот же результат.

    ``total`` равен числу разделов — каждому по 1 странице.

    Raises ValueError: ``total`` меньше числа разделов (минимум 1 страница на раздел
    и точная сумма несовместимы).
    """
    count = len(weights)
    if count == 0:
        return []
    if total < count:
        raise ValueError(f"cannot give {count} sections at least one page within {total}")
    if total == count:
        return [1] * count
    known = [w for w in weights if w is not None]
    default = Fraction(sum(known), len(known)) if known else Fraction(1)
    shares = [Fraction(w) if w is not None else default for w in weights]
    whole = sum(shares)
    quotas = [total * share / whole for share in shares]
    pages = [max(1, int(q)) for q in quotas]  # int() у неотрицательной дроби — floor
    leftover = total - sum(pages)
    if leftover > 0:
        # Сумма целых частей больше total - count: добавок не больше одной на раздел.
        by_remainder = sorted(range(count), key=lambda i: (-(quotas[i] - int(quotas[i])), i))
        for index in by_remainder[:leftover]:
            pages[index] += 1
    while leftover < 0:
        # Минимум 1 перебрал сумму: снимаем страницу с раздела, получившего больше
        # всего сверх доли (при равенстве — с более позднего). total > count: такой есть.
        index = max(
            (i for i in range(count) if pages[i] > 1), key=lambda i: (pages[i] - quotas[i], i)
        )
        pages[index] -= 1
        leftover += 1
    return pages


# --- Исход шага --------------------------------------------------------------------


class PlanOutcomeKind(StrEnum):
    COMPLETED = "completed"
    # Временная ошибка: следующая попытка — в проходе восстановления.
    RETRY_LATER = "retry_later"
    FAILED = "failed"
    # Генерация плана этого заказа уже идёт.
    BUSY = "busy"
    # Заказ не в PREPARING_PLAN: составлять нечего.
    SKIPPED = "skipped"
    # Результат устарел: заказ отменили или попытку признали прерванной.
    DISCARDED = "discarded"


@dataclass(frozen=True)
class PlanOutcome:
    kind: PlanOutcomeKind
    order_id: int
    # После успеха: WAITING_FOR_PLAN_CONFIRMATION и сохранённый план (content).
    status: OrderStatus | None = None
    plan: dict[str, Any] | None = None
    # Модели ушли не все материалы целиком (лимит MAX_ANALYSIS_INPUT_CHARS).
    truncated_material: int | None = None
    omitted_materials: tuple[int, ...] = ()
    # FAILED: можно ли перезапустить (лимит вызовов не исчерпан).
    can_retry: bool = False
    error_code: str | None = None

    @property
    def is_final(self) -> bool:
        """Исход, о котором стоит сообщить пользователю."""
        return self.kind in (PlanOutcomeKind.COMPLETED, PlanOutcomeKind.FAILED)
