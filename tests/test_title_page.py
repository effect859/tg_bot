"""Титульный лист из подтверждённого ТЗ (Stage 3.11): только данные ТЗ, без модели."""

from typing import Any

import pytest

from app.domain.title_page import TITLE_PAGE_FIELDS, build_title_page
from plan_samples import REQUIREMENTS


def test_fields_from_requirements() -> None:
    page = build_title_page(REQUIREMENTS)

    assert list(page) == list(TITLE_PAGE_FIELDS)
    assert page == {
        "university": "НИУ ВШЭ",
        "faculty": "",
        "department": "",
        "practice_type": "производственная",
        "practice_place": "ООО «Ёлка»",
        "topic": "Анализ бизнес-процессов отдела продаж",
        "practice_period": "с 01.07.2026 по 28.07.2026",
        "student_name": "Иванов Иван Иванович",
        "student_group": "БИ-21-1",
        "specialty": "Бизнес-информатика",
        "supervisor": "Петров Пётр Петрович",
        "organization_supervisor": "",
        "city": "",
        "year": "2026",
    }


@pytest.mark.parametrize("missing", ["student_group", "specialty", "supervisor"])
def test_missing_optional_field_is_empty(missing: str) -> None:
    page = build_title_page({**REQUIREMENTS, missing: None})

    assert page[missing] == ""


def test_fields_absent_from_requirements_are_always_empty() -> None:
    """Кафедру, факультет, город и руководителя от организации не угадываем."""
    noisy: dict[str, Any] = {
        **REQUIREMENTS,
        "faculty": "Ф",
        "department": "К",
        "city": "Москва",
        "organization_supervisor": "Р",
    }

    page = build_title_page(noisy)

    for name in ("faculty", "department", "city", "organization_supervisor"):
        assert page[name] == ""


def test_dates_without_end_give_no_period_and_no_year() -> None:
    page = build_title_page({**REQUIREMENTS, "practice_end": None})

    assert (page["practice_period"], page["year"]) == ("", "")


def test_invalid_date_is_ignored() -> None:
    page = build_title_page({**REQUIREMENTS, "practice_end": "28.07.2026"})

    assert (page["practice_period"], page["year"]) == ("", "")


def test_whitespace_is_normalized_and_non_strings_ignored() -> None:
    page = build_title_page({**REQUIREMENTS, "topic": "  Тема \n отчёта ", "university": 5})

    assert page["topic"] == "Тема отчёта"
    assert page["university"] == ""


def test_empty_requirements_give_all_blank_fields() -> None:
    assert build_title_page({}) == dict.fromkeys(TITLE_PAGE_FIELDS, "")
