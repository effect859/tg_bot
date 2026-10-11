"""Заготовки для тестов генерации отчёта: заказ в GENERATING и ответы модели по разделам."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.report_generation import size_range
from app.domain.requirements_dialog import content_digest
from app.llm.base import LLMResult, LLMUsage, StopReason
from app.services.plan_confirmation import confirm_plan
from app.services.plan_generation import generate_plan
from billing_samples import pay_for_order
from fake_llm import FakeLLMClient
from plan_samples import REQUIREMENTS, plan_result, preparing_order, section, subsection

# Объём ТЗ 6 страниц: распределение плана даёт ровно [1, 2, 2, 1].
TARGET_PAGES = 6
REPORT_PLAN = plan_result(
    section("Введение", 1),
    section(
        "Характеристика организации",
        2,
        subsections=[subsection("Общие сведения"), subsection("Структура")],
    ),
    section("Анализ бизнес-процессов", 2),
    section("Заключение", 1),
)
SECTION_PAGES = (1, 2, 2, 1)
SUBSECTIONS = (0, 2, 0, 0)


async def payment_pending_order(
    session: AsyncSession,
    *,
    tg_id: int = 1,
    plan: LLMResult = REPORT_PLAN,
    target_pages: int | None = TARGET_PAGES,
    material: str | None = None,
    references: list[str] | None = None,
) -> int:
    """Заказ в PAYMENT_PENDING: подтверждённые ТЗ и план, цена зафиксирована (зафиксировано)."""
    content = {**REQUIREMENTS, "target_pages": target_pages, "references": references or []}
    kwargs: dict[str, Any] = {"tg_id": tg_id, "content": content}
    if material is not None:
        kwargs["material"] = material
    order_id = await preparing_order(session, **kwargs)
    outcome = await generate_plan(session, FakeLLMClient(plan), order_id)
    assert outcome.plan is not None, outcome
    await confirm_plan(session, order_id, content_digest(outcome.plan))
    await session.commit()
    return order_id


async def generating_order(
    session: AsyncSession,
    *,
    tg_id: int = 1,
    plan: LLMResult = REPORT_PLAN,
    target_pages: int | None = TARGET_PAGES,
    material: str | None = None,
    references: list[str] | None = None,
) -> int:
    """Заказ в GENERATING: подтверждённые ТЗ и план, оплата пройдена (зафиксировано)."""
    order_id = await payment_pending_order(
        session,
        tg_id=tg_id,
        plan=plan,
        target_pages=target_pages,
        material=material,
        references=references,
    )
    # Оплата — только списанием кредитов: PAYMENT_PENDING → PAID → GENERATING.
    await pay_for_order(session, order_id)
    return order_id


def text_of(length: int, letter: str = "а") -> str:
    """Абзац ровно из ``length`` символов (без пробелов по краям)."""
    return letter * length


def answer(
    number: int,
    *,
    chars: int,
    subsections: int = 0,
    stop_reason: StopReason = StopReason.END_TURN,
    cache_read: int = 0,
    cache_write: int = 0,
) -> LLMResult:
    """Ответ модели на раздел ``number``: ``chars`` символов поровну между частями."""
    parts = subsections + 1 if subsections else 1
    sizes = [chars // parts] * parts
    sizes[0] += chars - sum(sizes)
    if subsections:
        body = {
            "section_number": number,
            "paragraphs": [text_of(sizes[0])],
            "subsections": [{"paragraphs": [text_of(size)]} for size in sizes[1:]],
        }
    else:
        body = {"section_number": number, "paragraphs": [text_of(chars)], "subsections": []}
    return LLMResult(
        text=json.dumps(body, ensure_ascii=False),
        model="fake-model-2026",
        usage=LLMUsage(
            input_tokens=1000,
            output_tokens=800,
            cached_tokens=cache_read,
            cache_creation_tokens=cache_write,
        ),
        latency_ms=900,
        stop_reason=stop_reason,
    )


def good(index: int, **kwargs: Any) -> LLMResult:
    """Корректный ответ на раздел ``index`` (с нуля) плана REPORT_PLAN: целевой объём."""
    pages = SECTION_PAGES[index]
    return answer(index + 1, chars=pages * 1800, subsections=SUBSECTIONS[index], **kwargs)


def too_short(index: int) -> LLMResult:
    low, _ = size_range(SECTION_PAGES[index])
    return answer(index + 1, chars=low - 1, subsections=SUBSECTIONS[index])


def too_long(index: int) -> LLMResult:
    _, high = size_range(SECTION_PAGES[index])
    return answer(index + 1, chars=high + 1, subsections=SUBSECTIONS[index])


ALL_GOOD = tuple(good(i) for i in range(len(SECTION_PAGES)))
