"""Уточнение ТЗ без БД: текущий вопрос, проверка ответов, даты, изменение поля, отпечаток."""

from typing import Any

import pytest

from app.domain.errors import RequirementFieldNotEditableError
from app.domain.requirements import REQUIRED_FIELDS
from app.domain.requirements_dialog import (
    DATE_FIELDS,
    EDITABLE_FIELDS,
    TEXT_ANSWER_LIMITS,
    AnswerError,
    apply_answer,
    clear_for_edit,
    content_digest,
    current_field,
    parse_answer,
)
from dialog_samples import AFTER_ANALYSIS, EMPTY, FULL

E = AnswerError

# --- Текущий вопрос ----------------------------------------------------------------


def test_questions_follow_required_fields_order() -> None:
    content = dict(EMPTY)
    asked = []
    while (field := current_field(content)) is not None:
        asked.append(field)
        content[field] = "2026-07-01" if field in DATE_FIELDS else "значение"

    assert (
        tuple(asked)
        == REQUIRED_FIELDS
        == (
            "topic",
            "practice_type",
            "practice_place",
            "practice_start",
            "practice_end",
            "university",
            "student_name",
        )
    )


def test_after_analysis_first_question_is_university() -> None:
    assert current_field(AFTER_ANALYSIS) == "university"


def test_optional_fields_are_never_asked() -> None:
    # supervisor, specialty, student_group и прочее пустые — вопросов нет.
    assert FULL["supervisor"] is None and FULL["student_group"] is None
    assert current_field(FULL) is None
    assert set(EDITABLE_FIELDS) == set(REQUIRED_FIELDS)
    assert "supervisor" not in EDITABLE_FIELDS


# --- Текстовые ответы --------------------------------------------------------------


@pytest.mark.parametrize("field", list(TEXT_ANSWER_LIMITS))
def test_text_answer_is_normalized(field: str) -> None:
    assert parse_answer(field, "  Иванов \n Иван\t Иванович ") == "Иванов Иван Иванович"


@pytest.mark.parametrize("field", list(TEXT_ANSWER_LIMITS))
@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_answer_is_rejected(field: str, text: str) -> None:
    assert parse_answer(field, text) is E.EMPTY


@pytest.mark.parametrize(("field", "limit"), list(TEXT_ANSWER_LIMITS.items()))
def test_length_limit(field: str, limit: int) -> None:
    assert parse_answer(field, "я" * limit) == "я" * limit
    assert parse_answer(field, "я" * (limit + 1)) is E.TOO_LONG


def test_limits() -> None:
    assert TEXT_ANSWER_LIMITS == {
        "topic": 500,
        "practice_type": 255,
        "practice_place": 255,
        "university": 255,
        "student_name": 255,
    }


@pytest.mark.parametrize("text", ["123", ".", "👍", "1а", "-", "12.07", "?!"])
def test_answer_without_letters_is_rejected(text: str) -> None:
    assert parse_answer("topic", text) is E.NO_LETTERS


@pytest.mark.parametrize("text", ["Тема​", "Тема\x00", "Тема­", "Те\x1bма"])
def test_invisible_and_control_characters_are_rejected(text: str) -> None:
    assert parse_answer("topic", text) is E.INVALID_CHARS


def test_digits_are_not_allowed_in_name_only() -> None:
    assert parse_answer("student_name", "Иванов Иван 2") is E.DIGITS_IN_NAME
    assert parse_answer("university", "Школа № 1 им. Пушкина") == "Школа № 1 им. Пушкина"
    assert parse_answer("practice_place", "ООО «Ёлка-2»") == "ООО «Ёлка-2»"


def test_minimal_meaningful_answers_are_accepted() -> None:
    assert parse_answer("practice_type", "учебная") == "учебная"
    assert parse_answer("university", "ВШЭ") == "ВШЭ"
    assert parse_answer("topic", "ИИ") == "ИИ"


@pytest.mark.parametrize("field", ["supervisor", "specialty", "target_pages", "bogus"])
def test_not_asked_fields_cannot_be_answered(field: str) -> None:
    with pytest.raises(RequirementFieldNotEditableError):
        parse_answer(field, "значение")


# --- Даты --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("01.07.2026", "2026-07-01"),
        ("1.7.2026", "2026-07-01"),
        ("  28.07.2026 ", "2026-07-28"),
        ("29.02.2028", "2028-02-29"),
        ("01.01.2000", "2000-01-01"),
        ("31.12.2100", "2100-12-31"),
    ],
)
@pytest.mark.parametrize("field", DATE_FIELDS)
def test_valid_dates(field: str, text: str, expected: str) -> None:
    assert parse_answer(field, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "завтра",
        "1 июля 2026",
        "2026-07-01",
        "01/07/2026",
        "01-07-2026",
        "01.07.26",
        "01.07.2026 г.",
        "01.07.2026.",
        "001.07.2026",
        "01.007.2026",
        "31.02.2026",
        "29.02.2027",
        "00.07.2026",
        "01.13.2026",
        "01.07.20266",
        "٠١.٠٧.٢٠٢٦",  # арабские цифры: isdigit() их пропустил бы
        "01. 07.2026",
        "+1.07.2026",
    ],
)
def test_invalid_dates(text: str) -> None:
    assert parse_answer("practice_start", text) is E.INVALID_DATE


@pytest.mark.parametrize("text", ["31.12.1999", "01.01.2101", "01.07.1026"])
def test_dates_out_of_range(text: str) -> None:
    assert parse_answer("practice_end", text) is E.DATE_OUT_OF_RANGE


def test_answer_keeps_other_fields() -> None:
    content, reset = apply_answer(AFTER_ANALYSIS, "university", "НИУ ВШЭ")

    assert not reset
    assert content == {**AFTER_ANALYSIS, "university": "НИУ ВШЭ"}
    assert AFTER_ANALYSIS["university"] is None  # исходное содержимое не изменено


def test_same_start_and_end_are_allowed() -> None:
    content, reset = apply_answer(
        {**EMPTY, "practice_start": "2026-07-01"}, "practice_end", "2026-07-01"
    )

    assert not reset
    assert content["practice_end"] == "2026-07-01"


@pytest.mark.parametrize(
    ("existing", "field", "value"),
    [
        ({"practice_start": "2026-07-10"}, "practice_end", "2026-07-01"),
        ({"practice_end": "2026-07-01"}, "practice_start", "2026-07-10"),
    ],
    ids=["end-before-start", "start-after-end-from-analysis"],
)
def test_conflicting_dates_reset_both(existing: dict[str, Any], field: str, value: str) -> None:
    content, reset = apply_answer({**FULL, **existing}, field, value)

    assert reset
    assert content["practice_start"] is None and content["practice_end"] is None
    assert current_field(content) == "practice_start"
    assert content["topic"] == FULL["topic"]


# --- Изменение поля ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["topic", "practice_type", "practice_place", "university", "student_name"]
)
def test_edit_clears_only_that_field(field: str) -> None:
    content = clear_for_edit(FULL, field)

    assert content == {**FULL, field: None}
    assert current_field(content) == field


@pytest.mark.parametrize("field", DATE_FIELDS)
def test_edit_of_a_date_clears_both(field: str) -> None:
    content = clear_for_edit(FULL, field)

    assert content == {**FULL, "practice_start": None, "practice_end": None}
    assert current_field(content) == "practice_start"


@pytest.mark.parametrize(
    "field", ["specialty", "supervisor", "target_pages", "extra_requirements", "x"]
)
def test_optional_fields_are_not_editable(field: str) -> None:
    with pytest.raises(RequirementFieldNotEditableError):
        clear_for_edit(FULL, field)


# --- Отпечаток ---------------------------------------------------------------------


def test_digest_is_stable_and_sensitive() -> None:
    reordered = dict(reversed(list(FULL.items())))

    assert content_digest(FULL) == content_digest(reordered)
    assert len(content_digest(FULL)) == 12
    assert content_digest(FULL) != content_digest({**FULL, "topic": FULL["topic"] + "."})
    assert content_digest(FULL) != content_digest({**FULL, "extra_requirements": []})
