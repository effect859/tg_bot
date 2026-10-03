"""Уточнение ТЗ: какой вопрос задать, как проверить ответ, что менять при изменении.

Модуль чистый (без БД и Telegram). Текущий вопрос не хранится: это первое
незаполненное поле из REQUIRED_FIELDS в черновике ТЗ. Ответ пользователя
принимается только после детерминированной проверки; смысл текста не
проверяется — от ошибок защищают эхо принятого ответа и экран подтверждения.
"""

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Any

from app.domain.errors import RequirementFieldNotEditableError
from app.domain.requirements import (
    LLM_TEXT_FIELDS,
    MAX_PRACTICE_DATE,
    MIN_PRACTICE_DATE,
    REQUIRED_FIELDS,
    missing_fields,
    normalize_whitespace,
)

# Из диалога меняются только обязательные поля; необязательные — только анализом.
EDITABLE_FIELDS: tuple[str, ...] = REQUIRED_FIELDS
DATE_FIELDS: tuple[str, ...] = ("practice_start", "practice_end")
# Предел длины ответа — как у значений, которые находит анализ.
TEXT_ANSWER_LIMITS: dict[str, int] = {
    "topic": LLM_TEXT_FIELDS["topic"],
    "practice_type": LLM_TEXT_FIELDS["practice_type"],
    "practice_place": LLM_TEXT_FIELDS["practice_place"],
    "university": 255,
    "student_name": 255,
}
# Ответ без букв («123», «.», «👍») — не значение поля.
MIN_ANSWER_LETTERS = 2
DIGEST_LENGTH = 12


class AnswerError(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    # Управляющие и невидимые символы (в том числе пробел нулевой ширины).
    INVALID_CHARS = "invalid_chars"
    NO_LETTERS = "no_letters"
    DIGITS_IN_NAME = "digits_in_name"
    INVALID_DATE = "invalid_date"
    DATE_OUT_OF_RANGE = "date_out_of_range"


def current_field(content: dict[str, Any]) -> str | None:
    """Поле текущего вопроса: первое незаполненное обязательное; None — всё заполнено."""
    missing = missing_fields(content)
    return missing[0] if missing else None


def parse_answer(field: str, text: str) -> str | AnswerError:
    """Значение поля из ответа пользователя (даты — в ISO) или причина отказа.

    Raises RequirementFieldNotEditableError — поле не задаётся вопросом.
    """
    if field in DATE_FIELDS:
        return _parse_date(text)
    if field not in TEXT_ANSWER_LIMITS:
        raise RequirementFieldNotEditableError(field)
    value = normalize_whitespace(text)
    if not value:
        return AnswerError.EMPTY
    if not value.isprintable():
        return AnswerError.INVALID_CHARS
    if len(value) > TEXT_ANSWER_LIMITS[field]:
        return AnswerError.TOO_LONG
    if sum(char.isalpha() for char in value) < MIN_ANSWER_LETTERS:
        return AnswerError.NO_LETTERS
    if field == "student_name" and any(char.isdigit() for char in value):
        return AnswerError.DIGITS_IN_NAME
    return value


def apply_answer(content: dict[str, Any], field: str, value: str) -> tuple[dict[str, Any], bool]:
    """Новое содержимое ТЗ с ответом; True — даты противоречили друг другу и очищены обе.

    Одна неверная дата (в том числе найденная анализом) не должна блокировать ввод
    другой: при start > end обе даты спрашиваются заново, начиная с даты начала.
    """
    updated = {**content, field: value}
    start, end = updated.get("practice_start"), updated.get("practice_end")
    if start and end and date.fromisoformat(start) > date.fromisoformat(end):
        for name in DATE_FIELDS:
            updated[name] = None
        return updated, True
    return updated, False


def clear_for_edit(content: dict[str, Any], field: str) -> dict[str, Any]:
    """Содержимое ТЗ без поля, которое пользователь хочет изменить (даты — обе сразу).

    Raises RequirementFieldNotEditableError — поле не обязательное.
    """
    if field not in EDITABLE_FIELDS:
        raise RequirementFieldNotEditableError(field)
    cleared = DATE_FIELDS if field in DATE_FIELDS else (field,)
    return {**content, **dict.fromkeys(cleared)}


def content_digest(content: dict[str, Any]) -> str:
    """Короткий отпечаток содержимого ТЗ: «Подтвердить» — именно то, что видел пользователь."""
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:DIGEST_LENGTH]


def _parse_date(text: str) -> str | AnswerError:
    parts = text.strip().split(".")
    if len(parts) != 3:
        return AnswerError.INVALID_DATE
    day, month, year = parts
    if not (_digits(day, 1, 2) and _digits(month, 1, 2) and _digits(year, 4, 4)):
        return AnswerError.INVALID_DATE
    try:
        parsed = date(int(year), int(month), int(day))
    except ValueError:
        return AnswerError.INVALID_DATE
    if not MIN_PRACTICE_DATE <= parsed <= MAX_PRACTICE_DATE:
        return AnswerError.DATE_OUT_OF_RANGE
    return parsed.isoformat()


def _digits(text: str, min_len: int, max_len: int) -> bool:
    # Только ASCII-цифры: str.isdigit() пропускает и другие цифры Юникода.
    return min_len <= len(text) <= max_len and text.isascii() and text.isdigit()
