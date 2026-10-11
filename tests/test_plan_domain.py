"""План отчёта (домен): схема ответа модели, проверка ответа, распределение страниц."""

import copy
import random
from typing import Any

import pytest

from app.domain import plan as domain
from app.domain import report_generation
from app.domain.plan import (
    MAX_PAGE_WEIGHT,
    MAX_PLAN_ATTEMPTS,
    MAX_PLAN_LLM_CALLS,
    MAX_REPORT_PAGES,
    MAX_SECTION_PAGES,
    MAX_SECTIONS,
    MIN_REPORT_PAGES,
    MIN_SECTIONS,
    NOT_FOR_PLAN,
    PLAN_JSON_SCHEMA,
    PLAN_REQUIREMENT_FIELDS,
    InvalidPlanResponse,
    distribute_pages,
    is_references_title,
    oversized_sections,
    plan_requirements,
    report_pages_supported,
    split_volume,
    unsupported_volume,
    validate_plan,
)
from app.domain.schemas import PlanContent
from plan_samples import REQUIREMENTS, plan_payload, section, subsection

# --- Схема ------------------------------------------------------------------------


def _objects(schema: Any) -> list[dict[str, Any]]:
    """Все объекты схемы (рекурсивно по properties, items, anyOf)."""
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


def _keys(schema: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(schema, dict):
        keys |= set(schema)
        for name, value in schema.items():
            if name == "properties":
                keys |= set(value)
            keys |= _keys(value)
    elif isinstance(schema, list):
        for item in schema:
            keys |= _keys(item)
    return keys


def test_schema_objects_are_strict() -> None:
    objects = _objects(PLAN_JSON_SCHEMA)
    assert len(objects) == 3  # ответ, раздел, подраздел — без рекурсии
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


def test_schema_shape() -> None:
    assert list(PLAN_JSON_SCHEMA["properties"]) == ["sections"]
    item = PLAN_JSON_SCHEMA["properties"]["sections"]["items"]
    assert list(item["properties"]) == [
        "title",
        "description",
        "requirements",
        "target_pages",
        "subsections",
    ]
    sub = item["properties"]["subsections"]["items"]
    assert list(sub["properties"]) == ["title", "description", "target_pages"]
    assert item["properties"]["target_pages"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]


def test_schema_has_no_unsupported_or_personal_keys() -> None:
    keys = _keys(PLAN_JSON_SCHEMA)
    # Structured outputs не принимают ограничения длины/количества и рекурсию.
    assert not keys & {"$ref", "$defs", "minItems", "maxItems", "minLength", "maxLength"}
    assert not keys & {"minimum", "maximum"}
    assert "total_pages" not in keys
    assert not keys & NOT_FOR_PLAN


def test_plan_requirements_drop_personal_fields() -> None:
    fields = plan_requirements(REQUIREMENTS)

    assert set(fields) == set(PLAN_REQUIREMENT_FIELDS)
    assert not set(fields) & NOT_FOR_PLAN
    assert {"student_name", "university", "supervisor", "student_group"} == NOT_FOR_PLAN


def test_plan_requirements_skip_empty_fields() -> None:
    content = {**REQUIREMENTS, "specialty": None, "extra_requirements": []}

    assert "specialty" not in plan_requirements(content)
    assert "extra_requirements" not in plan_requirements(content)


def test_limits() -> None:
    assert (MAX_PLAN_ATTEMPTS, MAX_PLAN_LLM_CALLS) == (3, 6)
    assert (domain.MIN_SECTIONS, domain.MAX_SECTIONS, domain.MAX_SUBSECTIONS) == (3, 15, 10)


# --- Проверка ответа ----------------------------------------------------------------


def test_valid_plan_without_target_keeps_model_pages() -> None:
    plan = validate_plan(plan_payload(), None)

    assert [s["target_pages"] for s in plan["sections"]] == [3, 8, 8, 6]
    assert plan["total_pages"] == 25
    second = plan["sections"][1]
    assert second["requirements"] == ["Описать структуру отдела продаж"]
    assert [s["title"] for s in second["subsections"]] == ["Общие сведения", "Структура"]
    # Подраздел — PlanSection без своих подразделов и требований.
    assert second["subsections"][0]["subsections"] == []
    assert second["subsections"][0]["requirements"] == []
    PlanContent.model_validate(plan)


def test_target_pages_are_distributed_exactly() -> None:
    plan = validate_plan(plan_payload(), 20)

    pages = [s["target_pages"] for s in plan["sections"]]
    assert sum(pages) == plan["total_pages"] == 20
    assert all(p >= 1 for p in pages)
    # Подразделы — внутри страниц раздела.
    second = plan["sections"][1]
    assert sum(s["target_pages"] for s in second["subsections"]) == second["target_pages"]


def test_null_pages_without_target_give_no_total() -> None:
    payload = plan_payload(section("А", None), section("Б", 3), section("В", 4))

    plan = validate_plan(payload, None)

    assert plan["total_pages"] is None
    assert plan["sections"][0]["target_pages"] is None


def test_whitespace_is_normalized() -> None:
    payload = plan_payload(section("  Введение \n  в тему "), section("Б"), section("В"))

    plan = validate_plan(payload, None)

    assert plan["sections"][0]["title"] == "Введение в тему"


@pytest.mark.parametrize("count", [3, 15])
def test_section_count_bounds_accepted(count: int) -> None:
    payload = plan_payload(*(section(f"Раздел {i}") for i in range(count)))

    assert len(validate_plan(payload, None)["sections"]) == count


@pytest.mark.parametrize("count", [0, 2, 16])
def test_section_count_out_of_range(count: int) -> None:
    payload = {"sections": [section(f"Раздел {i}") for i in range(count)]}

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_subsection_limit() -> None:
    ok = [subsection(f"П {i}") for i in range(10)]
    validate_plan(plan_payload(section("А", subsections=ok), section("Б"), section("В")), None)

    too_many = [subsection(f"П {i}") for i in range(11)]
    with pytest.raises(InvalidPlanResponse):
        validate_plan(
            plan_payload(section("А", subsections=too_many), section("Б"), section("В")), None
        )


def _broken(change: Any) -> dict[str, Any]:
    payload = copy.deepcopy(plan_payload())
    change(payload)
    return payload


def _set_title(p: dict[str, Any], value: Any) -> None:
    p["sections"][0]["title"] = value


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda p: _set_title(p, ""), id="empty-title"),
        pytest.param(lambda p: _set_title(p, "   "), id="blank-title"),
        pytest.param(lambda p: _set_title(p, "x" * 201), id="long-title"),
        pytest.param(lambda p: _set_title(p, "a\x00b"), id="control-char"),
        pytest.param(lambda p: _set_title(p, 5), id="title-not-string"),
        pytest.param(
            lambda p: p["sections"][0].update(description="x" * 1001), id="long-description"
        ),
        pytest.param(
            lambda p: p["sections"][0].update(requirements=["r"] * 11), id="many-requirements"
        ),
        pytest.param(lambda p: p["sections"][0].update(requirements=[""]), id="empty-requirement"),
        pytest.param(
            lambda p: p["sections"][0].update(requirements=["x" * 301]), id="long-requirement"
        ),
        pytest.param(lambda p: p["sections"][0].update(requirements="r"), id="requirements-str"),
        pytest.param(lambda p: p["sections"][0].update(target_pages=True), id="bool-pages"),
        pytest.param(lambda p: p["sections"][0].update(target_pages=0), id="zero-pages"),
        pytest.param(lambda p: p["sections"][0].update(target_pages=501), id="too-many-pages"),
        pytest.param(lambda p: p["sections"][0].update(target_pages=2.5), id="float-pages"),
        pytest.param(lambda p: p["sections"][0].update(target_pages="3"), id="string-pages"),
        pytest.param(
            lambda p: p["sections"][1]["subsections"][0].update(target_pages=False),
            id="bool-subsection-pages",
        ),
        pytest.param(lambda p: p["sections"][0].update(extra=1), id="extra-key"),
        pytest.param(lambda p: p["sections"][0].pop("subsections"), id="missing-key"),
        pytest.param(
            lambda p: p["sections"][1]["subsections"][0].update(subsections=[]), id="third-level"
        ),
        pytest.param(lambda p: p.update(total_pages=10), id="total-from-model"),
        pytest.param(lambda p: p.update(sections=None), id="sections-null"),
        pytest.param(lambda p: p["sections"].__setitem__(0, "Введение"), id="section-not-object"),
    ],
)
def test_invalid_responses(change: Any) -> None:
    with pytest.raises(InvalidPlanResponse):
        validate_plan(_broken(change), None)


@pytest.mark.parametrize("raw", [None, [], "план", {}])
def test_non_object_or_empty_response(raw: Any) -> None:
    with pytest.raises(InvalidPlanResponse):
        validate_plan(raw, None)


def test_duplicate_section_titles_case_insensitive() -> None:
    payload = plan_payload(section("Введение"), section("ВВЕДЕНИЕ"), section("Итог"))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_duplicate_titles_after_whitespace_normalization() -> None:
    payload = plan_payload(section("Глава  1"), section("глава 1"), section("Итог"))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_duplicate_subsection_titles() -> None:
    subs = [subsection("Обзор"), subsection("обзор")]
    payload = plan_payload(section("А", subsections=subs), section("Б"), section("В"))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_same_title_on_different_levels_allowed() -> None:
    payload = plan_payload(
        section("Анализ", subsections=[subsection("Анализ")]), section("Б"), section("В")
    )

    validate_plan(payload, None)


def test_boundary_lengths_accepted() -> None:
    payload = plan_payload(
        section(
            "x" * 200,
            MAX_SECTION_PAGES,
            description="d" * 1000,
            requirements=["r" * 300] * 10,
        ),
        section("Б", 1, description=""),
        section("В"),
    )

    plan = validate_plan(payload, None)

    assert plan["sections"][0]["target_pages"] == MAX_SECTION_PAGES


def test_boundary_page_weight_accepted_with_target() -> None:
    """Сырое число от модели до MAX_PAGE_WEIGHT — вес: с объёмом ТЗ он масштабируется."""
    payload = plan_payload(section("А", MAX_PAGE_WEIGHT), section("Б", 1), section("В", 1))

    plan = validate_plan(payload, 3)

    assert [s["target_pages"] for s in plan["sections"]] == [1, 1, 1]


# --- Номер в названии (Stage 2.9.1, M1) ----------------------------------------------
# Нумерацию разделов добавляет система; номер, поставленный моделью, убирается, иначе
# пользователь видит «1. 1. Введение». Ожидаемые строки заданы явно.

NUMBERED_TITLES = [
    ("1. Введение", "Введение"),
    ("1) Введение", "Введение"),
    ("2.3. Структура", "Структура"),
    ("2.3 Структура", "Структура"),
    ("1.Введение", "Введение"),
    ("  1.  Введение  ", "Введение"),
    ("10. Заключение", "Заключение"),
    ("1.2.3 Итоги", "Итоги"),
]
NUMBERED_IDS = [
    "dot",
    "paren",
    "multi-dot",
    "multi-no-dot",
    "no-space",
    "spaces",
    "two-digit",
    "3-level",
]
# Числа в самом тексте названия — не номер.
TEXT_WITH_NUMBERS = [
    "2024 год",
    "ISO 9001",
    "3D-модель",
    "5S-система",
    "3.5 млн",
    "Анализ 2024 года",
    "2.5D-печать",
    "Введение",
]


def _section_titles(plan: dict[str, Any]) -> list[str]:
    return [s["title"] for s in plan["sections"]]


def _subsection_titles(plan: dict[str, Any], index: int = 0) -> list[str]:
    return [s["title"] for s in plan["sections"][index]["subsections"]]


def _with_section_title(title: str) -> dict[str, Any]:
    return plan_payload(section(title, 2), section("Анализ", 2), section("Выводы", 2))


def _with_subsection_title(title: str) -> dict[str, Any]:
    subs = [subsection(title), subsection("Другой подраздел")]
    return plan_payload(section("Раздел", 4, subsections=subs), section("А", 2), section("Б", 2))


@pytest.mark.parametrize(("raw", "expected"), NUMBERED_TITLES, ids=NUMBERED_IDS)
def test_section_title_number_removed(raw: str, expected: str) -> None:
    plan = validate_plan(_with_section_title(raw), None)

    assert _section_titles(plan) == [expected, "Анализ", "Выводы"]


@pytest.mark.parametrize(("raw", "expected"), NUMBERED_TITLES, ids=NUMBERED_IDS)
def test_subsection_title_number_removed(raw: str, expected: str) -> None:
    plan = validate_plan(_with_subsection_title(raw), None)

    assert _subsection_titles(plan) == [expected, "Другой подраздел"]


@pytest.mark.parametrize("title", TEXT_WITH_NUMBERS)
def test_numbers_inside_title_are_kept(title: str) -> None:
    """Регулярное выражение не обрезает числа, которые являются частью названия."""
    section_plan = validate_plan(_with_section_title(title), None)
    subsection_plan = validate_plan(_with_subsection_title(title), None)

    assert _section_titles(section_plan)[0] == title
    assert _subsection_titles(subsection_plan)[0] == title


@pytest.mark.parametrize("title", ["1.", "1)", "2.3.", "1.   ", "  7)  "])
@pytest.mark.parametrize("level", ["section", "subsection"])
def test_title_of_only_a_number_is_invalid(title: str, level: str) -> None:
    payload = _with_section_title(title) if level == "section" else _with_subsection_title(title)

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_duplicate_section_titles_after_number_removal() -> None:
    payload = plan_payload(section("1. Введение", 2), section("Введение", 2), section("Б", 2))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_duplicate_section_titles_after_number_removal_case_insensitive() -> None:
    payload = plan_payload(section("1. Введение", 2), section("2) введение", 2), section("Б", 2))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_duplicate_subsection_titles_after_number_removal() -> None:
    subs = [subsection("2.1. Сведения"), subsection("2.2 Сведения")]
    payload = plan_payload(section("Раздел", 4, subsections=subs), section("А", 2), section("Б", 2))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_schema_asks_for_titles_without_numbers() -> None:
    sections = PLAN_JSON_SCHEMA["properties"]["sections"]["items"]
    subsections = sections["properties"]["subsections"]["items"]

    for title in (sections["properties"]["title"], subsections["properties"]["title"]):
        assert title["description"] == (
            "Название без номера: нумерацию разделов добавляет система."
        )


# --- Распределение страниц ----------------------------------------------------------


def test_distribution_proportional() -> None:
    assert distribute_pages([2, 8, 10, 2], 22) == [2, 8, 10, 2]
    assert distribute_pages([1, 1, 1], 30) == [10, 10, 10]


def test_distribution_without_weights_is_even() -> None:
    assert distribute_pages([None, None, None], 10) == [4, 3, 3]


def test_distribution_unknown_weight_gets_average() -> None:
    assert distribute_pages([None, 10, 20], 45) == [15, 10, 20]


def test_distribution_minimum_one_page_is_taken_from_largest() -> None:
    # Доли 0.1 / 0.1 / 9.8: малым разделам — по 1, недостача снимается с большого.
    assert distribute_pages([1, 1, 98], 10) == [1, 1, 8]


def test_distribution_ties_go_to_earlier_sections() -> None:
    assert distribute_pages([1, 1, 1, 1], 6) == [2, 2, 1, 1]


@pytest.mark.parametrize(
    ("weights", "total"),
    [
        ([1, 1, 1], 3),
        ([500, 1, 1], 3),
        ([1, 500, 1, 7, None], 6),
        ([3] * 15, 500),
        ([500] * 15, 16),
        ([None] * 15, 17),
        ([1, 2, 3], 500),
    ],
)
def test_distribution_sum_and_minimum(weights: list[int | None], total: int) -> None:
    pages = distribute_pages(weights, total)

    assert sum(pages) == total
    assert min(pages) >= 1
    assert len(pages) == len(weights)
    # Детерминированность: тот же вход — тот же результат.
    assert distribute_pages(list(weights), total) == pages


@pytest.mark.parametrize(("count", "total"), [(3, 2), (15, 14), (5, 3), (15, 1), (3, 0)])
def test_distribution_below_section_count_is_refused(count: int, total: int) -> None:
    """Минимум 1 страница на раздел и точная сумма несовместимы: явная ошибка."""
    with pytest.raises(ValueError):
        distribute_pages([7] * count, total)


@pytest.mark.parametrize("count", [3, 4, 15])
def test_distribution_equal_to_section_count(count: int) -> None:
    assert distribute_pages([7] * count, count) == [1] * count
    assert distribute_pages([None] * count, count) == [1] * count


@pytest.mark.parametrize("total", [4, 5])
def test_distribution_just_above_section_count(total: int) -> None:
    pages = distribute_pages([1, 500, None], total)

    assert sum(pages) == total
    assert min(pages) >= 1


def _sections(count: int) -> dict[str, Any]:
    return plan_payload(*(section(f"Раздел {i}", 3) for i in range(count)))


@pytest.mark.parametrize(("count", "target"), [(3, 2), (15, 14), (4, 1)])
def test_plan_with_more_sections_than_target_pages_is_invalid(count: int, target: int) -> None:
    with pytest.raises(InvalidPlanResponse):
        validate_plan(_sections(count), target)


@pytest.mark.parametrize(("count", "target"), [(3, 3), (3, 4), (3, 5), (15, 15), (15, 16)])
def test_plan_at_and_above_boundary_is_valid(count: int, target: int) -> None:
    plan = validate_plan(_sections(count), target)

    pages = [s["target_pages"] for s in plan["sections"]]
    assert sum(pages) == plan["total_pages"] == target
    assert min(pages) >= 1
    PlanContent.model_validate(plan)


def test_plan_at_boundary_gives_one_page_each() -> None:
    plan = validate_plan(_sections(3), 3)

    assert [s["target_pages"] for s in plan["sections"]] == [1, 1, 1]


def test_subsection_pages_dropped_when_section_too_small() -> None:
    subs = [subsection("П1", 2), subsection("П2", 2), subsection("П3", 2)]
    payload = plan_payload(section("А", 1, subsections=subs), section("Б", 7), section("В", 8))

    plan = validate_plan(payload, 16)

    first = plan["sections"][0]
    assert first["target_pages"] == 1
    assert [s["target_pages"] for s in first["subsections"]] == [None, None, None]


def test_validation_is_deterministic() -> None:
    assert validate_plan(plan_payload(), 23) == validate_plan(plan_payload(), 23)


def test_distribution_sum_exact_over_many_inputs() -> None:
    rng = random.Random(29)
    for _ in range(2000):
        count = rng.randint(3, 15)
        weights = [rng.choice([None, *range(1, 501)]) for _ in range(count)]
        # Обе стороны границы total = count и большие значения.
        total = rng.choice([count - 1, count, count + 1, rng.randint(0, 500)])

        if total < count:
            with pytest.raises(ValueError):
                distribute_pages(weights, total)
            with pytest.raises(InvalidPlanResponse):
                validate_plan(
                    plan_payload(*(section(f"Р{i}", w) for i, w in enumerate(weights))), total
                )
            continue
        pages = distribute_pages(weights, total)

        assert sum(pages) == total
        assert min(pages) >= 1
        assert len(pages) == count


# --- Предел раздела генератора (Stage 3.9, S1) ---------------------------------------
# Раздел больше MAX_SECTION_PAGES генератор не пишет: такой план не принимается, иначе
# он доходит до оплаты и падает уже в генерации.


def test_section_limit_is_generator_limit() -> None:
    assert MAX_SECTION_PAGES is report_generation.MAX_SECTION_PAGES
    assert MAX_SECTION_PAGES == 8
    assert MAX_PAGE_WEIGHT == 500


@pytest.mark.parametrize("target", [None, 3 * MAX_SECTION_PAGES])
def test_sections_of_max_pages_are_accepted(target: int | None) -> None:
    payload = plan_payload(*(section(name, MAX_SECTION_PAGES) for name in "АБВ"))

    plan = validate_plan(payload, target)

    assert [s["target_pages"] for s in plan["sections"]] == [MAX_SECTION_PAGES] * 3
    assert plan["total_pages"] == 3 * MAX_SECTION_PAGES


def test_section_over_max_pages_without_target_is_invalid() -> None:
    payload = plan_payload(section("А", MAX_SECTION_PAGES + 1), section("Б", 1), section("В", 1))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


@pytest.mark.parametrize("oversized", [0, 2, 4])
def test_one_oversized_section_among_several_is_invalid(oversized: int) -> None:
    pages: list[int | None] = [3, None, 8, 1, 5]
    pages[oversized] = MAX_SECTION_PAGES + 1
    payload = plan_payload(*(section(f"Раздел {i}", p) for i, p in enumerate(pages)))

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_skewed_weights_distributed_over_limit_are_invalid() -> None:
    """25 страниц на [2, 8, 10, 2] дают [2, 9, 12, 2]: распределение не меняется, план — нет."""
    payload = plan_payload(
        section("Введение", 2), section("А", 8), section("Б", 10), section("Заключение", 2)
    )
    assert distribute_pages([2, 8, 10, 2], 25) == [2, 9, 12, 2]

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, 25)


@pytest.mark.parametrize("count", [3, 4, MAX_SECTIONS])
def test_target_of_max_pages_per_section_is_accepted(count: int) -> None:
    target = MAX_SECTION_PAGES * count
    plan = validate_plan(_sections(count), target)

    pages = [s["target_pages"] for s in plan["sections"]]
    assert pages == [MAX_SECTION_PAGES] * count
    assert sum(pages) == plan["total_pages"] == target


@pytest.mark.parametrize("count", [3, 4, MAX_SECTIONS])
def test_target_over_max_pages_per_section_is_invalid(count: int) -> None:
    """8·n + 1 страниц на n разделов: какой-то раздел больше 8 при любых весах."""
    with pytest.raises(InvalidPlanResponse):
        validate_plan(_sections(count), MAX_SECTION_PAGES * count + 1)


@pytest.mark.parametrize("target", [3, 4, 10, 24])
def test_accepted_plans_respect_section_limit_and_sum(target: int) -> None:
    plan = validate_plan(_sections(3), target)

    pages = [s["target_pages"] for s in plan["sections"]]
    assert sum(pages) == plan["total_pages"] == target
    assert all(1 <= p <= MAX_SECTION_PAGES for p in pages)
    assert oversized_sections(plan) == []


def test_oversized_sections_lists_indexes() -> None:
    plan = {
        "sections": [
            {"title": "А", "target_pages": MAX_SECTION_PAGES},
            {"title": "Б", "target_pages": MAX_SECTION_PAGES + 1},
            {"title": "В", "target_pages": None},
            {"title": "Г"},
            {"title": "Д", "target_pages": 500},
        ]
    }

    assert oversized_sections(plan) == [1, 4]


# --- Объём отчёта из ТЗ (Stage 3.10) -------------------------------------------------
# План составляется только под объём MIN_REPORT_PAGES..MAX_REPORT_PAGES: не меньше
# страницы на раздел, не больше MAX_SECTION_PAGES на каждый из MAX_SECTIONS разделов.

HUGE = 10**499 + 7  # 500 цифр — длиннее не бывает: число должно стоять в цитате


def test_report_page_limits_are_derived() -> None:
    assert MIN_REPORT_PAGES == MIN_SECTIONS == 3
    assert MAX_REPORT_PAGES == MAX_SECTIONS * MAX_SECTION_PAGES == 120


@pytest.mark.parametrize(
    ("pages", "supported"),
    [
        (-1, False),
        (0, False),
        (1, False),
        (2, False),
        (3, True),
        (8, True),
        (9, True),
        (25, True),
        (120, True),
        (121, False),
        (500, False),
        (501, False),
        (HUGE, False),
    ],
)
def test_report_pages_supported(pages: int, supported: bool) -> None:
    assert report_pages_supported(pages) is supported


@pytest.mark.parametrize("pages", [3, 25, 120])
def test_supported_target_stays_in_target_pages(pages: int) -> None:
    content = {**REQUIREMENTS, "target_pages": pages, "unsupported_target_pages": None}

    assert split_volume(content) == content
    assert unsupported_volume(content) is None


@pytest.mark.parametrize("pages", [1, 2, 121, 500, 501, HUGE])
def test_unsupported_target_moves_to_its_own_field(pages: int) -> None:
    content = {**REQUIREMENTS, "target_pages": pages, "unsupported_target_pages": None}

    split = split_volume(content)

    assert split == {**content, "target_pages": None, "unsupported_target_pages": pages}
    assert content["target_pages"] == pages  # исходный словарь не изменён
    assert unsupported_volume(split) == pages
    assert unsupported_volume(content) == pages  # черновик до unsupported_target_pages


def test_missing_target_is_neither_supported_nor_unsupported() -> None:
    content = {**REQUIREMENTS, "target_pages": None, "unsupported_target_pages": None}
    legacy = {k: v for k, v in content.items() if k != "unsupported_target_pages"}

    assert split_volume(content) == content
    assert split_volume(legacy) == legacy  # старое содержимое без нового ключа
    assert unsupported_volume(content) is None
    assert unsupported_volume(legacy) is None


def test_unsupported_volume_is_not_sent_to_model() -> None:
    content = {**REQUIREMENTS, "target_pages": None, "unsupported_target_pages": 150}

    sent = plan_requirements(content)

    assert "unsupported_target_pages" not in sent
    assert "target_pages" not in sent
    assert "150" not in repr(sent)


# --- Раздел со списком источников добавляет система (Stage 3.11) ------------------------


@pytest.mark.parametrize(
    ("title", "is_references"),
    [
        ("Список использованных источников", True),
        ("СПИСОК ЛИТЕРАТУРЫ", True),
        ("Список  использованной литературы", True),
        ("Библиографический список", True),
        ("Библиография", True),
        ("Список сокращений", False),
        ("Источники финансирования", False),
        ("Анализ литературы", False),
        ("Введение", False),
    ],
)
def test_is_references_title(title: str, is_references: bool) -> None:
    assert is_references_title(title) is is_references


def test_plan_with_references_section_is_invalid() -> None:
    payload = plan_payload(
        section("Введение", 2), section("Анализ", 5), section("Список литературы", 1)
    )

    with pytest.raises(InvalidPlanResponse):
        validate_plan(payload, None)


def test_references_not_sent_to_model() -> None:
    sent = plan_requirements({**REQUIREMENTS, "references": ["Иванов И. И. Учебник."]})

    assert "references" not in sent
