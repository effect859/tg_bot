"""Сборка и рендеринг готового отчёта: БД (Stage 3.1) → assemble_document → DOCX.

Настоящая PostgreSQL, разделы генерируются FakeLLMClient через generate_next_section.
Ожидаемый текст строится независимо от assemble_document — по плану и строкам
order_report_sections; DOCX читается экстрактором материалов (Stage 2.4).
"""

import io
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderPlanVersion, OrderReportSection
from app.domain.enums import VersionStatus
from app.domain.report_document import validate_document_content
from app.domain.report_generation import ReportOutcomeKind
from app.rendering.docx import GOST_7_32, PracticeReportDocxRenderer
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


async def _expected_from_db(session: AsyncSession, order_id: int) -> list[str]:
    """Заголовок оглавления, затем по плану: «N. Название», абзацы, подразделы «N.M.»."""
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

    lines = [GOST_7_32.toc_title]
    for index, section in enumerate(plan.content["sections"]):
        number = index + 1
        content = texts[index]
        lines.append(f"{number}. {section['title']}")
        lines += content["paragraphs"]
        planned = section.get("subsections") or []
        pairs = zip(planned, content["subsections"], strict=True)
        for sub_number, (sub, text) in enumerate(pairs, start=1):
            lines.append(f"{number}.{sub_number}. {sub['title']}")
            lines += text["paragraphs"]
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

    headings = [line for line in extract_lines(data) if line[:1].isdigit()]
    assert headings == [
        "1. Введение",
        "2. Характеристика организации",
        "2.1. Общие сведения",
        "2.2. Структура",
        "3. Анализ бизнес-процессов",
        "4. Заключение",
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
