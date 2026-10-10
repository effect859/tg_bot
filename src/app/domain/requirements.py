"""Правила ТЗ отчёта по практике: обязательные поля и проверка результата анализа.

Модуль чистый (без БД, Telegram и LLM-клиента). Модель только предлагает значения
полей с цитатами из материалов; что обязательно, что принято и чего не хватает,
решает этот модуль.

* REQUIRED_FIELDS — без них ТЗ не подтверждается; порядок — порядок вопросов.
* LLM_FIELDS — что запрашивается у модели. ФИО студента, вуз и руководитель
  сюда намеренно не входят: их спросят у пользователя, а не извлекут из
  материалов. Сами материалы при этом могут содержать персональные данные —
  отдельная задача (минимизация данных) остаётся до публичного запуска.
* build_analysis_input — что и в каком объёме уходит модели: материалы целиком по
  порядку загрузки, пока помещаются в лимит, без молчаливой обрезки.
* validate_extraction — значение принимается, только если оно проходит проверку,
  его цитата действительно есть в отправленном тексте материалов и сама цитата
  подтверждает это значение (текст — дословно, дата — та же дата, страницы — то же
  число). Реальная, но не относящаяся к значению цитата его не подтверждает.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.domain.schemas import PracticeReportRequirements

REQUIRED_FIELDS: tuple[str, ...] = (
    "topic",
    "practice_type",
    "practice_place",
    "practice_start",
    "practice_end",
    "university",
    "student_name",
)

# Поля, которые модель ищет в материалах. Строковые — с ограничением длины.
LLM_TEXT_FIELDS: dict[str, int] = {
    "topic": 500,
    "practice_type": 255,
    "practice_place": 255,
    "specialty": 255,
    "student_group": 255,
}
LLM_DATE_FIELDS: tuple[str, ...] = ("practice_start", "practice_end")
LLM_FIELDS: tuple[str, ...] = (
    "topic",
    "practice_type",
    "practice_place",
    "practice_start",
    "practice_end",
    "specialty",
    "student_group",
    "target_pages",
    "extra_requirements",
)
# Никогда не запрашиваются у модели.
NOT_FOR_LLM: frozenset[str] = frozenset({"student_name", "university", "supervisor"})

# Сколько символов текста материалов уходит модели за одну попытку анализа.
MAX_ANALYSIS_INPUT_CHARS = 400_000
# Попыток в одном запуске анализа (автоматические повторы).
MAX_ANALYSIS_ATTEMPTS = 3
# Вызовов модели на анализ одного заказа за всё время (с ручными перезапусками).
MAX_ANALYSIS_LLM_CALLS = 9

MIN_EVIDENCE_CHARS = 10
MAX_EVIDENCE_CHARS = 500
MAX_EXTRA_REQUIREMENTS = 20
MAX_EXTRA_REQUIREMENT_CHARS = 500
MIN_PRACTICE_DATE = date(2000, 1, 1)
MAX_PRACTICE_DATE = date(2100, 12, 31)


def missing_fields(content: dict[str, Any]) -> list[str]:
    """Обязательные поля без значения — в порядке вопросов."""
    return [name for name in REQUIRED_FIELDS if content.get(name) in (None, "", [])]


# --- Вход анализа ------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisMaterial:
    upload_order: int
    text: str


@dataclass(frozen=True)
class AnalysisInput:
    """Что фактически отправляется модели.

    ``texts`` — тексты материалов в порядке загрузки (последний может быть обрезан).
    ``truncated_material`` — номер обрезанного материала, ``omitted`` — не вошедшие.
    """

    texts: tuple[AnalysisMaterial, ...]
    truncated_material: int | None
    omitted: tuple[int, ...]

    @property
    def truncated(self) -> bool:
        return self.truncated_material is not None or bool(self.omitted)

    @property
    def chars(self) -> int:
        return sum(len(m.text) for m in self.texts)

    def source_text(self) -> str:
        """Текст, по которому проверяются цитаты: ровно то, что ушло модели."""
        return "\n".join(m.text for m in self.texts)


def build_analysis_input(
    materials: Sequence[AnalysisMaterial], limit: int = MAX_ANALYSIS_INPUT_CHARS
) -> AnalysisInput:
    """Материалы по порядку загрузки, пока помещаются в ``limit`` символов.

    Первый не поместившийся материал обрезается по границе лимита, следующие не
    включаются. Результат зависит только от входа; факт обрезки — в самом результате.
    """
    texts: list[AnalysisMaterial] = []
    truncated: int | None = None
    omitted: list[int] = []
    room = limit
    for material in sorted(materials, key=lambda m: m.upload_order):
        if room <= 0:
            omitted.append(material.upload_order)
        elif len(material.text) <= room:
            texts.append(material)
            room -= len(material.text)
        else:
            texts.append(AnalysisMaterial(material.upload_order, material.text[:room]))
            truncated = material.upload_order
            room = 0
    return AnalysisInput(tuple(texts), truncated, tuple(omitted))


# --- Схема ответа модели -----------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _field(value_schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": {
            "value": _nullable(value_schema),
            "evidence": _nullable({"type": "string"}),
        },
        "required": ["value", "evidence"],
        "additionalProperties": False,
    }


_FIELD_DESCRIPTIONS = {
    "topic": "Тема практики или индивидуального задания.",
    "practice_type": "Вид практики (например, учебная, производственная, преддипломная).",
    "practice_place": "Место прохождения практики: организация или подразделение.",
    "practice_start": "Дата начала практики в формате YYYY-MM-DD.",
    "practice_end": "Дата окончания практики в формате YYYY-MM-DD.",
    "specialty": "Направление подготовки или специальность.",
    "student_group": "Учебная группа студента.",
    "target_pages": "Требуемый объём отчёта в страницах (целое число).",
}

ANALYSIS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "properties": {
                **{
                    name: _field({"type": "string"}, _FIELD_DESCRIPTIONS[name])
                    for name in (*LLM_TEXT_FIELDS, *LLM_DATE_FIELDS)
                },
                "target_pages": _field({"type": "integer"}, _FIELD_DESCRIPTIONS["target_pages"]),
                "extra_requirements": {
                    "type": "array",
                    "description": "Дополнительные требования к отчёту из материалов.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["value", "evidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": list(LLM_FIELDS),
            "additionalProperties": False,
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}


# --- Проверка ответа модели --------------------------------------------------------


class InvalidAnalysisResponse(Exception):
    """Ответ модели не соответствует схеме: повтор имеет смысл."""


@dataclass(frozen=True)
class ExtractionResult:
    """Принятые значения (в формате content версии ТЗ) и причины отказа по полям."""

    content: dict[str, Any]
    rejected: dict[str, str]

    @property
    def found(self) -> list[str]:
        return [name for name in LLM_FIELDS if self.content.get(name) not in (None, [])]


def normalize_whitespace(text: str) -> str:
    """Все последовательности пробельных символов (в том числе \\n и \\f) — один пробел."""
    return " ".join(text.split())


def validate_extraction(raw: Any, source_text: str) -> ExtractionResult:
    """Принимает только проверенные значения с цитатами из ``source_text``.

    Raises InvalidAnalysisResponse — ответ не той структуры.
    """
    fields = _structure(raw)
    source = normalize_whitespace(source_text)
    accepted: dict[str, Any] = {}
    rejected: dict[str, str] = {}

    def accept(name: str, entry: dict[str, Any], parse: Any, supports: Any) -> None:
        if entry["value"] is None:
            return  # модель не нашла: не ошибка
        value = parse(entry["value"])
        if value is None:
            rejected[name] = "invalid_value"
        elif not _evidence_found(entry["evidence"], source):
            rejected[name] = "evidence_not_found"
        elif not supports(value, normalize_whitespace(entry["evidence"])):
            rejected[name] = "evidence_mismatch"
        else:
            accepted[name] = value

    for name, max_chars in LLM_TEXT_FIELDS.items():
        accept(name, fields[name], lambda v, m=max_chars: _text(v, m), _text_in_evidence)
    for name in LLM_DATE_FIELDS:
        accept(name, fields[name], _date, _date_in_evidence)
    accept("target_pages", fields["target_pages"], _pages, _number_in_evidence)

    start, end = accepted.get("practice_start"), accepted.get("practice_end")
    if start is not None and end is not None and start > end:
        for name in LLM_DATE_FIELDS:
            del accepted[name]
            rejected[name] = "date_order"

    requirements: list[str] = []
    for item in fields["extra_requirements"]:
        value = _text(item["value"], MAX_EXTRA_REQUIREMENT_CHARS)
        if (
            value is None
            or not _evidence_found(item["evidence"], source)
            or not _text_in_evidence(value, normalize_whitespace(item["evidence"]))
        ):
            rejected["extra_requirements"] = "some_items_rejected"
        elif len(requirements) == MAX_EXTRA_REQUIREMENTS:
            rejected["extra_requirements"] = "too_many_items"
            break
        else:
            requirements.append(value)
    if requirements:
        accepted["extra_requirements"] = requirements

    try:
        content = PracticeReportRequirements.model_validate(accepted).model_dump(mode="json")
    except ValidationError as exc:  # не должно случаться после проверок выше
        raise InvalidAnalysisResponse("requirements schema rejected the extraction") from exc
    return ExtractionResult(content=content, rejected=rejected)


def _structure(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
        raise InvalidAnalysisResponse("no 'fields' object")
    fields = raw["fields"]
    for name in LLM_FIELDS:
        if name not in fields:
            raise InvalidAnalysisResponse(f"field {name!r} is missing")
    for name in (*LLM_TEXT_FIELDS, *LLM_DATE_FIELDS, "target_pages"):
        entry = fields[name]
        if not isinstance(entry, dict) or "value" not in entry or "evidence" not in entry:
            raise InvalidAnalysisResponse(f"field {name!r} has no value/evidence")
    items = fields["extra_requirements"]
    if not isinstance(items, list) or not all(
        isinstance(i, dict) and "value" in i and "evidence" in i for i in items
    ):
        raise InvalidAnalysisResponse("extra_requirements is not a list of value/evidence")
    return fields


def _evidence_found(evidence: Any, source: str) -> bool:
    if not isinstance(evidence, str):
        return False
    quote = normalize_whitespace(evidence)
    if not MIN_EVIDENCE_CHARS <= len(quote) <= MAX_EVIDENCE_CHARS:
        return False
    return quote in source


# Связь значения с цитатой; цитата к этому моменту уже найдена в тексте.
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_RU_DATE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")
_NUMBER = re.compile(r"(?<!\d)\d+(?!\d)")


def _text_in_evidence(value: str, quote: str) -> bool:
    """Значение дословно входит в цитату (пробелы нормализованы, регистр не важен)."""
    return value.casefold() in quote.casefold()


def _date_in_evidence(value: date, quote: str) -> bool:
    """В цитате есть та же календарная дата: YYYY-MM-DD или DD.MM.YYYY."""
    found = [(int(y), int(m), int(d)) for y, m, d in _ISO_DATE.findall(quote)]
    found += [(int(y), int(m), int(d)) for d, m, y in _RU_DATE.findall(quote)]
    return (value.year, value.month, value.day) in found


def _number_in_evidence(value: int, quote: str) -> bool:
    """В цитате есть то же целое число — отдельное, а не часть другого числа."""
    return any(int(number) == value for number in _NUMBER.findall(quote))


def _text(value: Any, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_whitespace(value)
    return text if 0 < len(text) <= max_chars else None


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if MIN_PRACTICE_DATE <= parsed <= MAX_PRACTICE_DATE else None


def _pages(value: Any) -> int | None:
    """Объём из материалов: любое целое от 1 (bool — не число).

    Верхней границы нет: требование, которое план не выполнит, не отбрасывается, а
    показывается пользователю (plan.split_volume). Длину числа ограничивает цитата
    (не больше MAX_EVIDENCE_CHARS): то же число должно в ней стоять.
    """
    if type(value) is not int:
        return None
    return value if value >= 1 else None
