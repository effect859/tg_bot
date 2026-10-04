"""Генерация отчёта (домен): схема ответа, проверка раздела, объём, текст запроса."""

import copy
from typing import Any

import pytest

from app.domain import report_generation as domain
from app.domain.plan import NOT_FOR_PLAN, plan_requirements
from app.domain.report_generation import (
    SECTION_JSON_SCHEMA,
    InvalidSectionResponse,
    SectionSizeOutOfRange,
    check_size,
    count_chars,
    max_report_calls,
    render_context,
    render_task,
    render_written_section,
    size_range,
    validate_section,
)
from app.domain.requirements import AnalysisMaterial, build_analysis_input
from plan_samples import PERSONAL_VALUES, REQUIREMENTS, section, subsection

SECTION = section("Характеристика", 2, subsections=[subsection("Общие"), subsection("Структура")])
FLAT = section("Введение", 1)


def _raw(number: int = 2, intro: list[Any] | None = None, subs: Any = None) -> dict[str, Any]:
    return {
        "section_number": number,
        "paragraphs": ["Вводный абзац."] if intro is None else intro,
        "subsections": [{"paragraphs": ["Первый."]}, {"paragraphs": ["Второй."]}]
        if subs is None
        else subs,
    }


# --- Константы и объём ------------------------------------------------------------


def test_constants() -> None:
    assert domain.CHARS_PER_PAGE == 1800
    assert domain.MAX_SECTION_PAGES == 8
    assert domain.MAX_SECTION_ATTEMPTS == 3
    assert max_report_calls(4) == 12


def test_size_range_for_max_section_pages() -> None:
    assert size_range(8) == (11_520, 18_000)


@pytest.mark.parametrize(("pages", "expected"), [(1, (1440, 2250)), (2, (2880, 4500))])
def test_size_range(pages: int, expected: tuple[int, int]) -> None:
    assert size_range(pages) == expected


@pytest.mark.parametrize(
    ("chars", "valid"), [(2879, False), (2880, True), (4500, True), (4501, False)]
)
def test_check_size_bounds(chars: int, valid: bool) -> None:
    if valid:
        check_size(chars, 2)
    else:
        with pytest.raises(SectionSizeOutOfRange) as error:
            check_size(chars, 2)
        assert (error.value.actual, error.value.low, error.value.high) == (chars, 2880, 4500)


def test_size_not_checked_without_target_pages() -> None:
    check_size(1, None)
    check_size(10**6, None)


# --- Схема -----------------------------------------------------------------------


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


def test_schema_is_strict_and_without_titles() -> None:
    objects = _objects(SECTION_JSON_SCHEMA)
    assert len(objects) == 2
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert list(SECTION_JSON_SCHEMA["properties"]) == [
        "section_number",
        "paragraphs",
        "subsections",
    ]
    assert "$ref" not in str(SECTION_JSON_SCHEMA) and "title" not in str(SECTION_JSON_SCHEMA)


# --- Проверка ответа --------------------------------------------------------------


def test_valid_section() -> None:
    text = validate_section(_raw(), SECTION, 2)

    assert text.content == {
        "paragraphs": ["Вводный абзац."],
        "subsections": [{"paragraphs": ["Первый."]}, {"paragraphs": ["Второй."]}],
    }
    assert text.char_count == len("Вводный абзац.") + len("Первый.") + len("Второй.")


def test_whitespace_normalized_before_counting() -> None:
    raw = {"section_number": 1, "paragraphs": ["  Два\n\n слова  "], "subsections": []}

    text = validate_section(raw, FLAT, 1)

    assert text.content["paragraphs"] == ["Два слова"]
    assert text.char_count == 9


def test_section_with_subsections_may_have_no_intro() -> None:
    assert validate_section(_raw(intro=[]), SECTION, 2).content["paragraphs"] == []


def _set(key: str, value: Any) -> Any:
    def change(raw: dict[str, Any]) -> None:
        raw[key] = value

    return change


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda r: r.pop("subsections"), id="missing-key"),
        pytest.param(_set("title", "Новое название"), id="extra-key"),
        pytest.param(_set("section_number", 3), id="another-section"),
        pytest.param(_set("section_number", "2"), id="number-string"),
        pytest.param(_set("paragraphs", "абзац"), id="paragraphs-string"),
        pytest.param(_set("paragraphs", [1]), id="paragraph-not-string"),
        pytest.param(_set("paragraphs", ["   "]), id="blank-paragraph"),
        pytest.param(_set("paragraphs", [""]), id="empty-paragraph"),
        pytest.param(_set("paragraphs", ["a\x00b"]), id="unprintable"),
        pytest.param(_set("subsections", [{"paragraphs": ["x"]}]), id="fewer-subsections"),
        pytest.param(_set("subsections", [{"paragraphs": ["x"]}] * 3), id="more-subsections"),
        pytest.param(_set("subsections", [{"paragraphs": []}] * 2), id="empty-subsection"),
        pytest.param(
            _set("subsections", [{"paragraphs": ["x"], "title": "y"}] * 2),
            id="subsection-extra-key",
        ),
        pytest.param(_set("subsections", None), id="subsections-null"),
    ],
)
def test_invalid_section(change: Any) -> None:
    raw = copy.deepcopy(_raw())
    change(raw)

    with pytest.raises(InvalidSectionResponse):
        validate_section(raw, SECTION, 2)


def test_bool_is_not_section_number() -> None:
    raw = {"section_number": True, "paragraphs": ["Текст."], "subsections": []}

    with pytest.raises(InvalidSectionResponse):
        validate_section(raw, FLAT, 1)


def test_flat_section_needs_text() -> None:
    raw = {"section_number": 1, "paragraphs": [], "subsections": []}

    with pytest.raises(InvalidSectionResponse):
        validate_section(raw, FLAT, 1)


@pytest.mark.parametrize("raw", [None, [], "текст", {}])
def test_non_object(raw: Any) -> None:
    with pytest.raises(InvalidSectionResponse):
        validate_section(raw, FLAT, 1)


def test_count_chars_without_separators() -> None:
    content = {"paragraphs": ["ab", "c d"], "subsections": [{"paragraphs": ["efg"]}]}

    assert count_chars(content) == 2 + 3 + 3


# --- Текст запроса -----------------------------------------------------------------


PLAN = {
    "sections": [FLAT, SECTION, section("Заключение", 1)],
    "total_pages": 4,
}


def test_context_has_no_personal_data_and_is_stable() -> None:
    materials = build_analysis_input([AnalysisMaterial(1, "Методичка.")])
    requirements = plan_requirements(REQUIREMENTS)

    first = render_context(requirements, PLAN, materials)
    second = render_context(dict(reversed(requirements.items())), PLAN, materials)

    assert first == second  # ключи отсортированы: префикс кэша не меняется
    for value in PERSONAL_VALUES:
        assert value not in first
    for name in NOT_FOR_PLAN:
        assert f'"{name}"' not in first
    assert '<material number="1">' in first and "Методичка." in first


def test_written_section_uses_plan_titles() -> None:
    content = {
        "paragraphs": ["Вводный."],
        "subsections": [{"paragraphs": ["Первый."]}, {"paragraphs": ["Второй."]}],
    }

    text = render_written_section(2, SECTION, content)

    assert text.startswith('<written_section number="2">\n2. Характеристика')
    assert "2.1. Общие" in text and "2.2. Структура" in text
    assert text.endswith("</written_section>")


def test_task_has_section_range_and_no_feedback_by_default() -> None:
    task = render_task(PLAN, 1)

    assert "раздел 2 из 3 — «Характеристика»" in task
    assert "section_number = 2" in task
    assert "ровно 2" in task
    assert "от 2880 до 4500" in task
    assert "Предыдущий вариант" not in task


def test_task_with_size_feedback() -> None:
    task = render_task(PLAN, 1, previous_size=5000)

    assert "Предыдущий вариант этого раздела был 5000 символов" in task
    assert "2880–4500" in task


def test_task_without_target_pages() -> None:
    plan = {"sections": [section("Введение", None)], "total_pages": None}

    task = render_task(plan, 0)

    assert "Объём — по содержанию раздела." in task
    assert "Подразделов нет" in task
