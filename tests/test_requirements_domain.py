"""Правила ТЗ: обязательные поля, вход анализа с лимитом, проверка ответа модели."""

import json
from typing import Any

import pytest

from app.domain.requirements import (
    ANALYSIS_JSON_SCHEMA,
    LLM_FIELDS,
    MAX_ANALYSIS_INPUT_CHARS,
    MAX_EXTRA_REQUIREMENTS,
    MIN_EVIDENCE_CHARS,
    NOT_FOR_LLM,
    REQUIRED_FIELDS,
    AnalysisMaterial,
    InvalidAnalysisResponse,
    build_analysis_input,
    missing_fields,
    normalize_whitespace,
    validate_extraction,
)
from app.domain.schemas import PracticeReportRequirements
from fake_llm import response_json

SOURCE = (
    "Задание на производственную практику.\n"
    "Тема: Анализ  бизнес-процессов\nотдела продаж.\f"
    "Место прохождения: ООО «Ёлка». Сроки практики: с 01.07.2026 по 28.07.2026.\n"
    "Объём отчёта — 25 страниц. Отчёт оформляется по ГОСТ 7.32."
)


def _validate(**fields: tuple[Any, Any]) -> Any:
    return validate_extraction(json.loads(response_json(**fields)), SOURCE)


# --- Обязательные поля -------------------------------------------------------------


def test_required_fields_and_their_order() -> None:
    assert REQUIRED_FIELDS == (
        "topic",
        "practice_type",
        "practice_place",
        "practice_start",
        "practice_end",
        "university",
        "student_name",
    )


def test_missing_fields_in_question_order() -> None:
    content = {"topic": "Т", "practice_place": "", "practice_end": "2026-07-28"}

    assert missing_fields(content) == [
        "practice_type",
        "practice_place",
        "practice_start",
        "university",
        "student_name",
    ]


def test_personal_fields_are_never_requested_from_llm() -> None:
    assert {"student_name", "university", "supervisor"} == NOT_FOR_LLM
    assert NOT_FOR_LLM.isdisjoint(LLM_FIELDS)
    properties = ANALYSIS_JSON_SCHEMA["properties"]["fields"]["properties"]
    assert set(properties) == set(LLM_FIELDS)
    assert NOT_FOR_LLM.isdisjoint(json.dumps(ANALYSIS_JSON_SCHEMA))


def _objects(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _objects(value)
    elif isinstance(schema, list):
        for item in schema:
            found += _objects(item)
    return found


def test_schema_is_strict_for_structured_outputs() -> None:
    objects = _objects(ANALYSIS_JSON_SCHEMA)
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    # Ограничения длины и диапазона API не поддерживает — их проверяет Python.
    text = json.dumps(ANALYSIS_JSON_SCHEMA)
    for keyword in ("minLength", "maxLength", "minimum", "maximum"):
        assert keyword not in text


# --- Вход анализа ------------------------------------------------------------------


def _materials(*lengths: int) -> list[AnalysisMaterial]:
    return [AnalysisMaterial(i + 1, chr(ord("a") + i) * n) for i, n in enumerate(lengths)]


def test_input_below_limit_is_complete() -> None:
    result = build_analysis_input(_materials(10, 20), limit=100)

    assert [m.text for m in result.texts] == ["a" * 10, "b" * 20]
    assert not result.truncated
    assert result.chars == 30


def test_input_exactly_at_limit_is_complete() -> None:
    result = build_analysis_input(_materials(60, 40), limit=100)

    assert not result.truncated
    assert result.chars == 100


def test_input_over_limit_is_cut_deterministically() -> None:
    result = build_analysis_input(_materials(60, 60, 10), limit=100)

    assert [(m.upload_order, len(m.text)) for m in result.texts] == [(1, 60), (2, 40)]
    assert result.truncated_material == 2
    assert result.omitted == (3,)
    assert result.truncated
    assert result.chars == 100


def test_input_order_is_upload_order() -> None:
    reversed_input = list(reversed(_materials(5, 5)))

    result = build_analysis_input(reversed_input, limit=100)

    assert [m.upload_order for m in result.texts] == [1, 2]


def test_default_limit_is_400k() -> None:
    assert MAX_ANALYSIS_INPUT_CHARS == 400_000
    result = build_analysis_input([AnalysisMaterial(1, "x" * 400_001)])

    assert result.chars == 400_000
    assert result.truncated_material == 1


# --- Проверка ответа модели --------------------------------------------------------


def test_values_with_found_evidence_are_accepted() -> None:
    result = _validate(
        topic=(
            "Анализ бизнес-процессов отдела продаж",
            "Тема: Анализ бизнес-процессов отдела продаж.",
        ),
        practice_type=("производственную", "на производственную практику"),
        practice_place=("ООО «Ёлка»", "Место прохождения: ООО «Ёлка»"),
        practice_start=("2026-07-01", "с 01.07.2026"),
        practice_end=("2026-07-28", "по 28.07.2026"),
        target_pages=(25, "Объём отчёта — 25 страниц"),
    )

    assert result.content["topic"] == "Анализ бизнес-процессов отдела продаж"
    assert result.content["practice_type"] == "производственную"
    assert result.content["practice_start"] == "2026-07-01"
    assert result.content["target_pages"] == 25
    assert result.rejected == {}
    assert set(result.found) == {
        "topic",
        "practice_type",
        "practice_place",
        "practice_start",
        "practice_end",
        "target_pages",
    }
    PracticeReportRequirements.model_validate(result.content)


def test_evidence_matches_across_line_breaks_and_page_separators() -> None:
    # В тексте: «Анализ  бизнес-процессов\nотдела продаж.\f» — пробелы и переводы иные.
    result = _validate(topic=("Анализ", "Анализ бизнес-процессов отдела продаж."))

    assert result.content["topic"] == "Анализ"


@pytest.mark.parametrize(
    "evidence",
    [None, "Анализ финансовых потоков", "Тема: Ана", "  ", 42],
    ids=["none", "not-in-text", "too-short", "blank", "not-a-string"],
)
def test_value_without_found_evidence_is_rejected(evidence: Any) -> None:
    result = _validate(topic=("Анализ", evidence))

    assert result.content["topic"] is None
    assert result.rejected == {"topic": "evidence_not_found"}


def test_shortest_allowed_evidence_is_accepted() -> None:
    assert MIN_EVIDENCE_CHARS == 10
    assert _validate(topic=("Анализ", "ма: Анализ")).content["topic"] == "Анализ"
    assert _validate(topic=("Анализ", "а: Анализ")).rejected == {"topic": "evidence_not_found"}


# --- Связь значения с цитатой ------------------------------------------------------


def test_text_value_is_matched_case_and_whitespace_insensitively() -> None:
    result = _validate(
        topic=(
            "анализ БИЗНЕС-процессов\nотдела   продаж",
            "Тема: Анализ бизнес-процессов отдела продаж.",
        ),
    )

    assert result.content["topic"] == "анализ БИЗНЕС-процессов отдела продаж"
    assert result.rejected == {}


@pytest.mark.parametrize(
    ("field", "value", "evidence"),
    [
        # Реальная цитата + выдуманная тема.
        ("topic", "Анализ ИИ в банковском секторе", "Тема: Анализ бизнес-процессов"),
        # Реальная цитата + выдуманное место практики.
        ("practice_place", "ООО «Ромашка»", "Место прохождения: ООО «Ёлка»"),
        # Реальная цитата + вид практики, которого в ней нет.
        ("practice_type", "преддипломная", "Задание на производственную практику"),
        # Значение длиннее цитаты: цитата подтверждает только его часть.
        ("topic", "Анализ бизнес-процессов отдела закупок", "Анализ бизнес-процессов"),
    ],
    ids=["invented-topic", "invented-place", "invented-type", "partial-quote"],
)
def test_invented_text_with_real_evidence_is_rejected(
    field: str, value: str, evidence: str
) -> None:
    result = _validate(**{field: (value, evidence)})

    assert result.content[field] is None
    assert result.rejected == {field: "evidence_mismatch"}


@pytest.mark.parametrize(
    ("value", "evidence"),
    [
        ("2026-07-02", "с 01.07.2026"),  # другая дата
        ("2026-01-07", "с 01.07.2026"),  # день и месяц переставлены
        ("2025-07-01", "с 01.07.2026"),  # другой год
        ("2026-07-01", "Сроки практики:"),  # даты в цитате нет
        ("2026-07-01", "по 28.07.2026"),  # в цитате другая дата
    ],
)
def test_date_not_in_evidence_is_rejected(value: str, evidence: str) -> None:
    result = _validate(practice_start=(value, evidence))

    assert result.content["practice_start"] is None
    assert result.rejected == {"practice_start": "evidence_mismatch"}


@pytest.mark.parametrize(
    ("value", "evidence", "source"),
    [
        ("2026-07-01", "с 01.07.2026", SOURCE),
        ("2026-07-01", "начало 2026-07-01", "Сроки: начало 2026-07-01, конец 2026-07-28."),
    ],
    ids=["dd.mm.yyyy", "iso"],
)
def test_same_date_in_evidence_is_accepted(value: str, evidence: str, source: str) -> None:
    raw = json.loads(response_json(practice_start=(value, evidence)))

    assert validate_extraction(raw, source).content["practice_start"] == value


def test_dates_inside_longer_numbers_do_not_count() -> None:
    source = "Номер приказа 101.07.20261 от 2026-07-015"
    raw = json.loads(
        response_json(
            practice_start=("2026-07-01", "Номер приказа 101.07.20261"),
            practice_end=("2026-07-01", "от 2026-07-015"),
        )
    )

    result = validate_extraction(raw, source)

    assert result.rejected == {
        "practice_start": "evidence_mismatch",
        "practice_end": "evidence_mismatch",
    }


@pytest.mark.parametrize(
    "value",
    [24, 2, 5, 250, 7],
    ids=["other", "digit-of-25", "digit-of-25-b", "prefix", "gost-not-near"],
)
def test_other_page_count_is_rejected(value: int) -> None:
    result = _validate(target_pages=(value, "Объём отчёта — 25 страниц"))

    assert result.content["target_pages"] is None
    assert result.rejected == {"target_pages": "evidence_mismatch"}


def test_extra_requirement_unrelated_to_evidence_is_rejected() -> None:
    result = validate_extraction(
        json.loads(
            response_json(
                extra_requirements=[
                    ("Отчёт оформляется по ГОСТ 7.32", "Отчёт оформляется по ГОСТ 7.32"),
                    # Реальная цитата, но требование выдумано.
                    ("Приложить дневник практики", "Отчёт оформляется по ГОСТ 7.32"),
                ]
            )
        ),
        SOURCE,
    )

    assert result.content["extra_requirements"] == ["Отчёт оформляется по ГОСТ 7.32"]
    assert result.rejected == {"extra_requirements": "some_items_rejected"}


def test_overlong_evidence_is_rejected() -> None:
    long_source = "Тема: " + "очень длинная " * 60
    raw = json.loads(response_json(topic=("Тема", long_source)))

    result = validate_extraction(raw, long_source)

    assert result.rejected == {"topic": "evidence_not_found"}


def test_null_value_is_just_not_found() -> None:
    result = _validate(topic=(None, "Тема"))

    assert result.content["topic"] is None
    assert result.rejected == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("practice_start", "01.07.2026"),
        ("practice_start", "2026-13-01"),
        ("practice_start", "1999-07-01"),
        ("practice_start", "2026-07-01T00:00:00"),
        ("target_pages", 0),
        ("target_pages", -25),
        ("target_pages", True),
        ("target_pages", "25"),
        ("target_pages", 25.0),
        ("topic", "x" * 501),
        ("practice_place", "   "),
        ("practice_type", 7),
    ],
)
def test_invalid_values_are_rejected(field: str, value: Any) -> None:
    result = _validate(**{field: (value, "Тема: Анализ")})

    assert result.content[field] is None
    assert result.rejected == {field: "invalid_value"}


def test_reversed_dates_are_both_dropped() -> None:
    result = _validate(
        practice_start=("2026-07-28", "по 28.07.2026"),
        practice_end=("2026-07-01", "с 01.07.2026"),
    )

    assert result.content["practice_start"] is None
    assert result.content["practice_end"] is None
    assert result.rejected == {"practice_start": "date_order", "practice_end": "date_order"}


def test_extra_requirements_keep_only_verified_items() -> None:
    result = validate_extraction(
        json.loads(
            response_json(
                extra_requirements=[
                    ("оформляется по ГОСТ 7.32", "Отчёт оформляется по ГОСТ 7.32"),
                    ("Приложить дневник практики", "Приложить дневник практики"),
                ]
            )
        ),
        SOURCE,
    )

    assert result.content["extra_requirements"] == ["оформляется по ГОСТ 7.32"]
    assert result.rejected == {"extra_requirements": "some_items_rejected"}


def test_extra_requirements_are_capped() -> None:
    items = [("по ГОСТ 7.32", "оформляется по ГОСТ 7.32")] * (MAX_EXTRA_REQUIREMENTS + 3)
    raw = json.loads(response_json(extra_requirements=items))

    result = validate_extraction(raw, SOURCE)

    assert len(result.content["extra_requirements"]) == MAX_EXTRA_REQUIREMENTS
    assert result.rejected == {"extra_requirements": "too_many_items"}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {},
        {"fields": []},
        {"fields": {}},
        {"fields": {**json.loads(response_json())["fields"], "topic": "Анализ"}},
        {"fields": {**json.loads(response_json())["fields"], "topic": {"value": "x"}}},
        {"fields": {**json.loads(response_json())["fields"], "extra_requirements": {}}},
        {"fields": {**json.loads(response_json())["fields"], "extra_requirements": ["x"]}},
    ],
    ids=lambda raw: str(raw)[:40],
)
def test_wrong_structure_is_invalid_response(raw: Any) -> None:
    with pytest.raises(InvalidAnalysisResponse):
        validate_extraction(raw, SOURCE)


def test_normalize_whitespace() -> None:
    assert normalize_whitespace(" a\n\tb\f\xa0 c  ") == "a b c"


# --- Объём без верхней границы (Stage 3.10) -----------------------------------------
# Объём, под который план не составить, не отбрасывается: его увидит пользователь.
# Длину числа ограничивает только цитата (MAX_EVIDENCE_CHARS): число должно в ней быть.


@pytest.mark.parametrize("pages", [1, 2, 121, 500, 501, 10**6, 10**499 + 7])
def test_any_positive_page_count_with_evidence_is_accepted(pages: int) -> None:
    # 500-значное число занимает всю цитату целиком.
    quote = str(pages) if len(str(pages)) == 500 else f"Объём отчёта — {pages} страниц"
    source = f"{SOURCE}\n{quote}."
    raw = json.loads(response_json(target_pages=(pages, quote)))

    result = validate_extraction(raw, source)

    assert result.content["target_pages"] == pages
    assert result.rejected == {}


def test_page_count_longer_than_evidence_limit_cannot_be_quoted() -> None:
    pages = 10**500  # 501 цифра: в цитату до 500 символов не помещается
    raw = json.loads(response_json(target_pages=(pages, str(pages))))

    result = validate_extraction(raw, f"{SOURCE}\n{pages}")

    assert result.content["target_pages"] is None
    assert result.rejected == {"target_pages": "evidence_not_found"}
