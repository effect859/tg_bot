"""Сборка и рендеринг готового отчёта: БД (Stage 3.1) → assemble_document → DOCX.

Настоящая PostgreSQL, разделы генерируются FakeLLMClient через generate_next_section.
Ожидаемый текст строится независимо от assemble_document — по плану и строкам
order_report_sections; DOCX читается экстрактором материалов (Stage 2.4).
"""

import io
import zipfile
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderPlanVersion, OrderReportSection, OrderRequirementsVersion
from app.domain.enums import VersionStatus
from app.domain.report_document import validate_document_content
from app.domain.report_generation import ReportOutcomeKind
from app.rendering.docx import (
    BLANK_LINE,
    GOST_7_32,
    REFERENCES_TITLE,
    PracticeReportDocxRenderer,
)
from app.services.report_generation import assemble_document, generate_next_section
from fake_llm import FakeLLMClient
from report_samples import ALL_GOOD, generating_order
from test_docx_renderer import extract_lines


async def _generated_order(session: AsyncSession) -> int:
    order_id = await generating_order(session)
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        outcome = await generate_next_section(session, llm, order_id)
        assert outcome.kind is ReportOutcomeKind.SAVED
    return order_id


def _expected_title_page(requirements: dict[str, Any]) -> list[str]:
    """Титульный лист по данным подтверждённого ТЗ; нет данных — линия (Stage 3.11)."""

    def value(name: str) -> str:
        return requirements.get(name) or BLANK_LINE

    start = date.fromisoformat(requirements["practice_start"])
    end = date.fromisoformat(requirements["practice_end"])
    return [
        value("university"),
        f"Факультет (институт): {BLANK_LINE}",
        f"Кафедра: {BLANK_LINE}",
        "ОТЧЁТ",
        "о прохождении практики",
        f"Вид практики: {value('practice_type')}",
        f"Место прохождения практики: {value('practice_place')}",
        f"Тема: {value('topic')}",
        f"Сроки практики: с {start:%d.%m.%Y} по {end:%d.%m.%Y}",
        f"Студент: {value('student_name')}",
        f"Группа: {value('student_group')}",
        f"Направление подготовки: {value('specialty')}",
        f"Руководитель практики от вуза: {value('supervisor')}",
        f"Руководитель практики от организации: {BLANK_LINE}",
        f"{BLANK_LINE} {end.year}",
    ]


async def _expected_from_db(session: AsyncSession, order_id: int) -> list[str]:
    """Титульный лист, заголовок оглавления, затем по плану: «N. Название», абзацы,
    подразделы «N.M.»; «Введение» и «Заключение» — заглавными, без номера (ГОСТ)."""
    requirements = await session.scalar(
        select(OrderRequirementsVersion.content).where(
            OrderRequirementsVersion.order_id == order_id,
            OrderRequirementsVersion.status == VersionStatus.CONFIRMED,
        )
    )
    assert requirements is not None
    plan = await session.scalar(
        select(OrderPlanVersion).where(
            OrderPlanVersion.order_id == order_id,
            OrderPlanVersion.status == VersionStatus.CONFIRMED,
        )
    )
    assert plan is not None
    rows = await session.scalars(
        select(OrderReportSection).where(OrderReportSection.plan_version_id == plan.id)
    )
    texts = {row.section_index: row.content for row in rows}

    lines = [*_expected_title_page(requirements), GOST_7_32.toc_title]
    number = 0
    for index, section in enumerate(plan.content["sections"]):
        content = texts[index]
        structural = section["title"].casefold() in ("введение", "заключение")
        if structural:
            lines.append(section["title"].upper())
        else:
            number += 1
            lines.append(f"{number}. {section['title']}")
        lines += content["paragraphs"]
        planned = section.get("subsections") or []
        pairs = zip(planned, content["subsections"], strict=True)
        for sub_number, (sub, text) in enumerate(pairs, start=1):
            lines.append(sub["title"] if structural else f"{number}.{sub_number}. {sub['title']}")
            lines += text["paragraphs"]
    for item, reference in enumerate(requirements.get("references") or [], start=1):
        lines += [REFERENCES_TITLE] if item == 1 else []
        lines.append(f"{item}. {reference}")
    return lines


async def test_generated_report_renders_to_docx(session: AsyncSession) -> None:
    order_id = await _generated_order(session)

    document = await assemble_document(session, order_id)
    validate_document_content(document)
    data = PracticeReportDocxRenderer().render(document)

    package = zipfile.ZipFile(io.BytesIO(data))
    assert package.testzip() is None
    assert "word/document.xml" in package.namelist()
    # Порядок, названия из плана, нумерация и текст разделов — без потерь.
    assert extract_lines(data) == await _expected_from_db(session, order_id)


async def test_headings_match_plan_order(session: AsyncSession) -> None:
    order_id = await _generated_order(session)

    data = PracticeReportDocxRenderer().render(await assemble_document(session, order_id))

    lines = extract_lines(data)
    headings = [
        line
        for line in lines[lines.index(GOST_7_32.toc_title) :]
        if line[:1].isdigit() or line in ("ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ")
    ]

    # ГОСТ 7.32 (Stage 3.11): структурные элементы без номера, основные — с 1.
    assert headings == [
        "ВВЕДЕНИЕ",
        "1. Характеристика организации",
        "1.1. Общие сведения",
        "1.2. Структура",
        "2. Анализ бизнес-процессов",
        "ЗАКЛЮЧЕНИЕ",
    ]


async def test_rendering_is_read_only_and_repeatable(session: AsyncSession) -> None:
    order_id = await _generated_order(session)
    before = [
        (row.section_index, row.content)
        for row in await session.scalars(
            select(OrderReportSection)
            .where(OrderReportSection.order_id == order_id)
            .order_by(OrderReportSection.section_index)
        )
    ]

    first = PracticeReportDocxRenderer().render(await assemble_document(session, order_id))
    second = PracticeReportDocxRenderer().render(await assemble_document(session, order_id))

    assert first == second
    after = [
        (row.section_index, row.content)
        for row in await session.scalars(
            select(OrderReportSection)
            .where(OrderReportSection.order_id == order_id)
            .order_by(OrderReportSection.section_index)
            .execution_options(populate_existing=True)
        )
    ]
    assert after == before


async def test_report_with_references_renders_list_last(session: AsyncSession) -> None:
    """Stage 3.11: источники из подтверждённого ТЗ — последним разделом без номера."""
    book = "Иванов И. И. Бизнес-процессы. — М.: Наука, 2020."
    order_id = await generating_order(session, references=[book, "ГОСТ 7.32-2017."])
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        assert (await generate_next_section(session, llm, order_id)).kind is ReportOutcomeKind.SAVED

    data = PracticeReportDocxRenderer().render(await assemble_document(session, order_id))

    lines = extract_lines(data)
    assert lines == await _expected_from_db(session, order_id)
    assert lines[-3:] == [REFERENCES_TITLE, f"1. {book}", "2. ГОСТ 7.32-2017."]
