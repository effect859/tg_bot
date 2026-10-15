"""Генерация отчёта (сервис): разделы по одному, повторы, регенерация, бюджет 3N, восстановление.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_report_generation_concurrency.py. LLM — FakeLLMClient,
сеть не используется (кроме MockTransport в тесте тела запроса).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMCall, Order, OrderPlanVersion, OrderReportSection
from app.domain.enums import LLMCallStatus, OrderStatus, VersionStatus
from app.domain.errors import VersionNotFoundError
from app.domain.report_generation import MAX_SECTION_PAGES, SECTION_JSON_SCHEMA, ReportOutcomeKind
from app.domain.title_page import build_title_page
from app.llm.anthropic import AnthropicLLMClient
from app.llm.base import LLMPermanentError, LLMTemporaryError, LLMUsage, StopReason
from app.services import orders
from app.services import report_generation as service
from app.services.plan_generation import generate_plan
from app.services.report_generation import (
    NOT_COMPLETED,
    ReportGenerationRecovery,
    assemble_document,
    find_orders_for_generation,
    generate_next_section,
    run_generation_pass,
)
from billing_samples import pay_for_order
from fake_llm import PRICED_MODEL, FakeLLMClient, priced, result
from plan_samples import PERSONAL_VALUES, REQUIREMENTS, plan_result, preparing_order, section
from report_samples import (
    ALL_GOOD,
    REPORT_PLAN,
    answer,
    generating_order,
    good,
    too_long,
    too_short,
)

S = OrderStatus
K = ReportOutcomeKind


async def _fresh(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _calls(session: AsyncSession, order_id: int) -> list[LLMCall]:
    stmt = select(LLMCall).where(LLMCall.order_id == order_id, LLMCall.step == service.LLM_STEP)
    return list(
        await session.scalars(stmt.order_by(LLMCall.id).execution_options(populate_existing=True))
    )


async def _sections(session: AsyncSession, order_id: int) -> list[OrderReportSection]:
    stmt = (
        select(OrderReportSection)
        .where(OrderReportSection.order_id == order_id)
        .order_by(OrderReportSection.section_index)
    )
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _set(session: AsyncSession, order_id: int, **values: object) -> None:
    await session.execute(update(Order).where(Order.id == order_id).values(**values))
    await session.commit()


async def _generate_all(session: AsyncSession, order_id: int) -> FakeLLMClient:
    llm = FakeLLMClient(*ALL_GOOD)
    for _ in ALL_GOOD:
        assert (await generate_next_section(session, llm, order_id)).kind is K.SAVED
    return llm


def _feedback(call: dict[str, Any]) -> bool:
    return "Предыдущий вариант" in call["prompt"]


async def _stale(session: AsyncSession, order_id: int) -> None:
    await _set(session, order_id, step_started_at=datetime.now(UTC) - timedelta(minutes=11))


# --- Успех ----------------------------------------------------------------------------


async def test_first_section_saved(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(good(0, cache_read=300, cache_write=5000))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.section_index) == (K.SAVED, 0)
    [saved] = await _sections(session, order_id)
    assert (saved.section_index, saved.char_count) == (0, 1800)
    assert saved.content == {"paragraphs": ["а" * 1800], "subsections": []}
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (S.GENERATING, 0, None)
    [call] = await _calls(session, order_id)
    assert (call.status, call.error) == (LLMCallStatus.SUCCESS, None)
    assert (call.input_tokens, call.output_tokens) == (1000, 800)
    assert (call.cached_tokens, call.cache_creation_tokens) == (300, 5000)
    [request] = llm.calls
    assert request["json_schema"] is SECTION_JSON_SCHEMA
    assert request["max_tokens"] == 16_000
    assert request["system"] == service.REPORT_PROMPT


async def test_cache_blocks_grow_section_by_section(session: AsyncSession) -> None:
    """[A●] → [A●][S1●] → [A●][S1][S2●]: отдельный блок на раздел, метка на последнем."""
    order_id = await generating_order(session)
    llm = FakeLLMClient(*ALL_GOOD)

    for _ in range(3):
        await generate_next_section(session, llm, order_id)

    first, second, third = (call["prefix"] for call in llm.calls)
    assert [b.cache_ttl for b in first] == ["1h"]
    assert [b.cache_ttl for b in second] == ["1h", "1h"]
    assert [b.cache_ttl for b in third] == ["1h", None, "1h"]
    assert first[0].text == second[0].text == third[0].text  # общий префикс неизменен
    assert second[1].text == third[1].text  # раздел 1 — тот же блок
    assert third[2].text.startswith('<written_section number="2">\n2. Характеристика')
    assert "раздел 3 из 4" in llm.calls[2]["prompt"]
    assert "раздел 3" not in third[0].text


async def test_all_sections_then_done(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await _generate_all(session, order_id)
    llm = FakeLLMClient(good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert outcome.kind is K.DONE
    assert llm.calls == []
    assert [s.section_index for s in await _sections(session, order_id)] == [0, 1, 2, 3]
    assert (await _fresh(session, order_id)).status is S.GENERATING  # COMPLETED — позже


async def test_continue_after_failure_from_first_missing_section(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(good(0), good(1), LLMTemporaryError("http_529"), good(2))
    for _ in range(2):
        await generate_next_section(session, llm, order_id)
    before = {s.section_index: (s.id, s.content) for s in await _sections(session, order_id)}

    retry = await generate_next_section(session, llm, order_id)
    resumed = await generate_next_section(session, llm, order_id)

    assert (retry.kind, retry.section_index, retry.error_code) == (K.RETRY_LATER, 2, "http_529")
    assert (resumed.kind, resumed.section_index) == (K.SAVED, 2)
    after = {s.section_index: (s.id, s.content) for s in await _sections(session, order_id)}
    assert {i: after[i] for i in (0, 1)} == before  # 0–1 не перегенерированы
    assert len(llm.calls) == 4


async def test_section_without_target_pages_skips_size_check(session: AsyncSession) -> None:
    order_id = await generating_order(session, target_pages=None, plan=_plan_pages(None))

    outcome = await generate_next_section(session, FakeLLMClient(answer(1, chars=5)), order_id)

    assert outcome.kind is K.SAVED


# --- MAX_SECTION_PAGES ----------------------------------------------------------------


def _plan_pages(first: int | None) -> Any:
    return plan_result(section("Основная часть", first), section("Вывод", 1), section("Итог", 1))


async def test_section_of_max_pages_is_generated(session: AsyncSession) -> None:
    order_id = await generating_order(session, target_pages=None, plan=_plan_pages(8))
    llm = FakeLLMClient(answer(1, chars=8 * 1800))

    assert (await generate_next_section(session, llm, order_id)).kind is K.SAVED
    assert len(llm.calls) == 1


async def _legacy_generating_order(session: AsyncSession, pages: int) -> int:
    """Заказ в GENERATING с подтверждённым планом, первый раздел которого ``pages``
    страниц (план, подтверждённый до Stage 3.9). Черновик правится до подтверждения
    (триггер защищает только подтверждённые версии) и подтверждается шагами
    confirm_plan без проверки D1."""
    order_id = await preparing_order(session, content={**REQUIREMENTS, "target_pages": None})
    generated = await generate_plan(session, FakeLLMClient(_plan_pages(8)), order_id)
    assert generated.plan is not None, generated
    [draft] = await orders.get_plan_versions(session, order_id)
    assert draft.status is VersionStatus.DRAFT
    content = dict(draft.content)
    content["sections"] = [dict(s) for s in content["sections"]]
    content["sections"][0]["target_pages"] = pages
    content["total_pages"] = sum(s["target_pages"] for s in content["sections"])
    await session.execute(
        update(OrderPlanVersion).where(OrderPlanVersion.id == draft.id).values(content=content)
    )
    await orders.lock_order(session, order_id)
    await orders.confirm_plan_version(session, order_id, draft.id)
    await orders.change_status(session, order_id, S.PAYMENT_PENDING)
    await session.commit()
    await pay_for_order(session, order_id)
    return order_id


async def test_section_over_max_pages_fails_without_llm_call(session: AsyncSession) -> None:
    """Последняя защита генератора: новый план с таким разделом до оплаты не доходит."""
    order_id = await _legacy_generating_order(session, MAX_SECTION_PAGES + 1)
    [plan] = await orders.get_plan_versions(session, order_id)
    assert plan.status is VersionStatus.CONFIRMED
    assert plan.content["sections"][0]["target_pages"] == MAX_SECTION_PAGES + 1
    llm = FakeLLMClient(answer(1, chars=9 * 1800))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "section_too_large")
    assert llm.calls == [] and await _calls(session, order_id) == []
    order = await _fresh(session, order_id)
    assert (order.status, order.failed_from_status) == (S.FAILED, S.GENERATING)
    assert order.error == "report_generation: section_too_large"


# --- Ошибки ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer_", "code"),
    [
        (LLMTemporaryError("timeout"), "timeout"),
        (LLMTemporaryError("http_429"), "http_429"),
        (result("не JSON"), "invalid_response"),
        (answer(2, chars=1800), "invalid_response"),  # ответ на другой раздел
        (result("{", stop_reason=StopReason.MAX_TOKENS), "max_tokens"),
    ],
)
async def test_temporary_error_is_retried_later(
    session: AsyncSession, answer_: object, code: str
) -> None:
    order_id = await generating_order(session)

    outcome = await generate_next_section(session, FakeLLMClient(answer_), order_id)  # type: ignore[arg-type]

    assert (outcome.kind, outcome.error_code) == (K.RETRY_LATER, code)
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (S.GENERATING, 1, None)
    assert await _sections(session, order_id) == []
    [call] = await _calls(session, order_id)
    assert (call.status, call.error) == (LLMCallStatus.ERROR, code)


@pytest.mark.parametrize(
    ("answer_", "code"),
    [
        (LLMPermanentError("http_400"), "http_400"),
        (LLMPermanentError("client_error"), "client_error"),
        (result("", stop_reason=StopReason.REFUSAL), "refusal"),
    ],
)
async def test_permanent_error_fails_at_once(
    session: AsyncSession, answer_: object, code: str
) -> None:
    order_id = await generating_order(session)

    outcome = await generate_next_section(session, FakeLLMClient(answer_), order_id)  # type: ignore[arg-type]

    assert (outcome.kind, outcome.error_code) == (K.FAILED, code)
    order = await _fresh(session, order_id)
    assert (order.status, order.error) == (S.FAILED, f"report_generation: {code}")


async def test_three_failed_attempts_on_section_lead_to_failed(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(LLMTemporaryError("http_500"))

    kinds = [(await generate_next_section(session, llm, order_id)).kind for _ in range(4)]

    assert kinds == [K.RETRY_LATER, K.RETRY_LATER, K.FAILED, K.SKIPPED]
    assert len(llm.calls) == 3  # не больше 3 вызовов на раздел


async def test_attempts_reset_after_saved_section(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(
        LLMTemporaryError("http_500"), LLMTemporaryError("http_500"), good(0), good(1)
    )

    kinds = [(await generate_next_section(session, llm, order_id)).kind for _ in range(4)]

    assert kinds == [K.RETRY_LATER, K.RETRY_LATER, K.SAVED, K.SAVED]
    assert (await _fresh(session, order_id)).step_attempts == 0


async def test_order_error_and_logs_have_no_text_or_personal_data(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    order_id = await generating_order(session, material="СЕКРЕТНЫЙ МАТЕРИАЛ")
    secret = result(
        json.dumps({"section_number": 1, "paragraphs": ["СЕКРЕТНЫЙ ТЕКСТ"], "subsections": [1]})
    )

    await generate_next_section(session, FakeLLMClient(secret), order_id)
    await generate_next_section(session, FakeLLMClient(good(0)), order_id)

    for marker in ("СЕКРЕТНЫЙ", "аааа", *PERSONAL_VALUES):
        assert marker not in caplog.text


# --- Регенерация по объёму ------------------------------------------------------------


async def test_out_of_range_then_regeneration_succeeds(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_long(0), good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.section_index) == (K.SAVED, 0)
    first, second = llm.calls
    assert not _feedback(first)
    assert "Предыдущий вариант этого раздела был 2251 символов" in second["prompt"]
    assert "1440–2250" in second["prompt"]
    calls = await _calls(session, order_id)
    assert [c.error for c in calls] == ["out_of_range", None]


async def test_regeneration_out_of_range_fails(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_short(0), too_long(0), good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "size_out_of_range")
    assert len(llm.calls) == 2  # третьего вызова нет
    assert (await _fresh(session, order_id)).error == "report_generation: size_out_of_range"
    assert await _sections(session, order_id) == []


async def test_crash_after_regeneration_claim(session: AsyncSession) -> None:
    """Регенерация захвачена, вызов прерван; хранимое состояние её не помнит."""
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_long(0), RuntimeError("process died"), too_long(0))

    with pytest.raises(RuntimeError):
        await generate_next_section(session, llm, order_id)
    await session.rollback()
    await _stale(session, order_id)
    outcome = await generate_next_section(session, llm, order_id)

    # Попытка 3 — обычная, без отзыва; объём снова вне допуска, а попыток больше нет.
    assert (outcome.kind, outcome.error_code) == (K.FAILED, "size_out_of_range")
    assert len(llm.calls) == 3
    assert [_feedback(call) for call in llm.calls] == [False, True, False]
    calls = await _calls(session, order_id)
    assert [c.error for c in calls] == ["out_of_range", NOT_COMPLETED, "out_of_range"]


async def test_crash_before_regeneration_claim(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_long(0), too_long(0), good(0))
    original_claim = service._claim
    claims = 0

    async def claim_then_die(*args: Any) -> Any:
        nonlocal claims
        claims += 1
        if claims == 2:
            raise RuntimeError("process died")
        return await original_claim(*args)

    monkeypatch.setattr(service, "_claim", claim_then_die)
    with pytest.raises(RuntimeError):
        await generate_next_section(session, llm, order_id)
    await session.rollback()
    outcome = await generate_next_section(session, llm, order_id)

    # Попытка 2 — обычная; её объём вне допуска → регенерация (попытка 3) → успех.
    assert (outcome.kind, outcome.section_index) == (K.SAVED, 0)
    assert [_feedback(call) for call in llm.calls] == [False, False, True]
    assert len(await _calls(session, order_id)) == 3


async def test_regeneration_temporary_error_then_attempt_without_feedback(
    session: AsyncSession,
) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_long(0), LLMTemporaryError("http_503"), good(0))

    first = await generate_next_section(session, llm, order_id)
    second = await generate_next_section(session, llm, order_id)

    assert (first.kind, first.error_code) == (K.RETRY_LATER, "http_503")
    assert second.kind is K.SAVED
    assert [_feedback(call) for call in llm.calls] == [False, True, False]


async def test_no_regeneration_when_report_budget_is_exhausted(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    _add_calls(session, order_id, 11)  # бюджет 3 × 4 = 12: остался один вызов
    await session.commit()
    llm = FakeLLMClient(too_long(0), good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "size_out_of_range")
    assert len(llm.calls) == 1
    assert len(await _calls(session, order_id)) == 12


async def test_at_most_one_regeneration_per_section(session: AsyncSession) -> None:
    """Обычная → регенерация → (новый вызов сервиса) обычная: лимит 3 на раздел."""
    order_id = await generating_order(session)
    llm = FakeLLMClient(too_long(0), LLMTemporaryError("timeout"), too_long(0), good(0))

    await generate_next_section(session, llm, order_id)
    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "size_out_of_range")
    assert len(llm.calls) == 3
    assert sum(_feedback(call) for call in llm.calls) == 1


# --- Бюджет 3N ------------------------------------------------------------------------


def _add_calls(session: AsyncSession, order_id: int, count: int) -> None:
    session.add_all(
        LLMCall(
            order_id=order_id,
            step=service.LLM_STEP,
            provider="fake",
            model="m",
            status=LLMCallStatus.ERROR,
            error=NOT_COMPLETED,
        )
        for _ in range(count)
    )


async def test_report_call_budget_counts_interrupted_calls(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    _add_calls(session, order_id, 12)
    await session.commit()
    llm = FakeLLMClient(good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "llm_call_limit")
    assert llm.calls == []


async def test_report_never_exceeds_3n_calls(session: AsyncSession) -> None:
    """Каждый раздел расходует 3 вызова: ровно 12 на 4 раздела, затем — ни одного."""
    order_id = await generating_order(session)
    answers: list[object] = []
    for index in range(4):
        answers += [LLMTemporaryError("http_500"), LLMTemporaryError("http_500"), good(index)]
    llm = FakeLLMClient(*answers)  # type: ignore[arg-type]

    for _ in range(12):
        await generate_next_section(session, llm, order_id)
    done = await generate_next_section(session, llm, order_id)

    assert done.kind is K.DONE
    assert len(llm.calls) == 12 and len(await _calls(session, order_id)) == 12


async def test_budget_exhausted_mid_report_fails(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(good(0), good(1))
    await generate_next_section(session, llm, order_id)
    _add_calls(session, order_id, 11)
    await session.commit()

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "llm_call_limit")
    assert len(llm.calls) == 1


# --- Устаревший результат, занято, не тот статус -----------------------------------------


async def test_running_attempt_makes_second_start_busy(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await _set(session, order_id, step_attempts=1, step_started_at=datetime.now(UTC))
    llm = FakeLLMClient(good(0))

    assert (await generate_next_section(session, llm, order_id)).kind is K.BUSY
    assert llm.calls == []


async def test_stale_attempt_is_counted_and_retried(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await _set(
        session,
        order_id,
        step_attempts=1,
        step_started_at=datetime.now(UTC) - timedelta(minutes=11),
    )

    outcome = await generate_next_section(session, FakeLLMClient(good(0)), order_id)

    assert outcome.kind is K.SAVED


async def test_stale_third_attempt_fails_without_call(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await _set(
        session,
        order_id,
        step_attempts=3,
        step_started_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    llm = FakeLLMClient(good(0))

    outcome = await generate_next_section(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "attempts_exhausted")
    assert llm.calls == []


async def test_result_of_taken_over_attempt_is_discarded(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    llm = FakeLLMClient(good(0))

    async def taken_over() -> None:
        await _set(
            session,
            order_id,
            step_attempts=Order.step_attempts + 1,
            step_started_at=datetime.now(UTC),
        )

    llm.on_complete = taken_over
    outcome = await generate_next_section(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    assert await _sections(session, order_id) == []
    [call] = await _calls(session, order_id)
    assert call.status is LLMCallStatus.SUCCESS  # вызов состоялся и учтён


async def test_unexpected_error_leaves_attempt_to_recovery(session: AsyncSession) -> None:
    order_id = await generating_order(session)

    with pytest.raises(RuntimeError):
        await generate_next_section(session, FakeLLMClient(RuntimeError("boom")), order_id)
    await session.rollback()

    order = await _fresh(session, order_id)
    assert (order.step_attempts, order.status) == (1, S.GENERATING)
    assert order.step_started_at is not None


@pytest.mark.parametrize("status", [S.PAID, S.FAILED])
async def test_order_not_generating_is_skipped(session: AsyncSession, status: S) -> None:
    order_id = await generating_order(session)
    failed_from = S.GENERATING if status is S.FAILED else None
    await _set(session, order_id, status=status, failed_from_status=failed_from)
    llm = FakeLLMClient(good(0))

    assert (await generate_next_section(session, llm, order_id)).kind is K.SKIPPED
    assert llm.calls == []


# --- Сборка документа -----------------------------------------------------------------


async def test_assemble_document(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await _generate_all(session, order_id)

    document = await assemble_document(session, order_id)

    assert document.title == "Анализ бизнес-процессов отдела продаж"
    # Титульный лист — из подтверждённого ТЗ; источников в этом ТЗ нет (Stage 3.11).
    assert document.title_page == build_title_page(REQUIREMENTS)
    assert document.title_page["student_name"] == "Иванов Иван Иванович"
    assert document.title_page["year"] == "2026"
    assert document.references == []
    assert [s.title for s in document.sections] == [
        "Введение",
        "Характеристика организации",
        "Анализ бизнес-процессов",
        "Заключение",
    ]
    org = document.sections[1]
    assert [s.title for s in org.subsections] == ["Общие сведения", "Структура"]
    assert org.subsections[0].paragraphs and org.subsections[0].subsections == []
    assert document.sections[0].paragraphs == ["а" * 1800]


async def test_assemble_requires_all_sections(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await generate_next_section(session, FakeLLMClient(good(0)), order_id)

    with pytest.raises(VersionNotFoundError):
        await assemble_document(session, order_id)


# --- Тело запроса: ПДн и кэш ----------------------------------------------------------


async def test_http_body_has_cache_blocks_and_no_personal_data(session: AsyncSession) -> None:
    order_id = await generating_order(session)
    await generate_next_section(session, FakeLLMClient(good(0)), order_id)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": good(1).text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 900,
                    "cache_read_input_tokens": 4000,
                    "cache_creation_input_tokens": 700,
                },
            },
        )

    llm = AnthropicLLMClient(
        api_key="test-key",
        model="claude-test",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )

    assert (await generate_next_section(session, llm, order_id)).kind is K.SAVED
    [body] = bodies
    content = body["messages"][0]["content"]
    assert [block.get("cache_control") for block in content] == [
        {"type": "ephemeral", "ttl": "1h"},
        {"type": "ephemeral", "ttl": "1h"},
        None,
    ]
    raw = json.dumps(body, ensure_ascii=False)
    for value in PERSONAL_VALUES:
        assert value not in raw
    assert "secret-file-name" not in raw
    call = (await _calls(session, order_id))[-1]
    assert (call.input_tokens, call.cached_tokens, call.cache_creation_tokens) == (50, 4000, 700)


# --- Восстановление -------------------------------------------------------------------


async def test_find_orders_for_generation(session: AsyncSession) -> None:
    waiting = await generating_order(session, tg_id=1)
    running = await generating_order(session, tg_id=2)
    stale = await generating_order(session, tg_id=3)
    done = await generating_order(session, tg_id=4)
    await _set(session, running, step_attempts=1, step_started_at=datetime.now(UTC))
    await _set(
        session, stale, step_attempts=1, step_started_at=datetime.now(UTC) - timedelta(minutes=11)
    )
    await _generate_all(session, done)

    found = await find_orders_for_generation(session, limit=10)

    assert set(found) == {waiting, stale}
    assert len(await find_orders_for_generation(session, limit=1)) == 1


async def test_pass_generates_one_section_per_order(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    first = await generating_order(session, tg_id=11)
    second = await generating_order(session, tg_id=12)

    outcomes = await run_generation_pass(sessionmaker, FakeLLMClient(good(0)))

    assert sorted((o.order_id, o.kind, o.section_index) for o in outcomes) == sorted(
        [(first, K.SAVED, 0), (second, K.SAVED, 0)]
    )
    assert len(await _sections(session, first)) == len(await _sections(session, second)) == 1


async def test_pass_continues_after_order_exception(
    session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = await generating_order(session, tg_id=21, material="Материал [broken]")
    ok = await generating_order(session, tg_id=22, material="Материал [ok]")

    class ByMarker(FakeLLMClient):
        async def complete(self, **kwargs: Any) -> Any:  # type: ignore[override]
            if "[broken]" in kwargs["prefix"][0].text:
                raise RuntimeError("secret details")
            return good(0)

    outcomes = await run_generation_pass(sessionmaker, ByMarker())

    assert [(o.order_id, o.kind) for o in outcomes] == [(ok, K.SAVED)]
    assert f"order_id={broken} error=RuntimeError" in caplog.text
    assert "secret details" not in caplog.text


async def test_recovery_runs_first_pass_at_start(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    order_id = await generating_order(session)
    passes: list[list[Any]] = []

    class Recording(ReportGenerationRecovery):
        async def run_pass(self) -> list[Any]:
            outcomes = await super().run_pass()
            passes.append(outcomes)
            return outcomes

    recovery = Recording(sessionmaker, FakeLLMClient(good(0)), interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(200):
            if passes:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert [(o.order_id, o.kind) for o in passes[0]] == [(order_id, K.SAVED)]
    assert len(await _sections(session, order_id)) == 1
    assert not recovery.running


def test_recovery_constants() -> None:
    assert service.RECOVERY_INTERVAL_SECONDS == 60.0
    assert service.RECOVERY_BATCH == 5
    assert timedelta(minutes=10) == service.STEP_STALE_AFTER
    assert service.MAX_TOKENS == 16_000
    assert service.CACHE_TTL == "1h"
    assert service.LLM_STEP == "report_generation"


def test_report_plan_sample() -> None:
    assert json.loads(REPORT_PLAN.text)["sections"][1]["title"] == "Характеристика организации"


# --- Титульный лист и источники — из ТЗ, без модели (Stage 3.11) -------------------------

BOOK = "Иванов И. И. Бизнес-процессы. — М.: Наука, 2020."


async def test_assemble_document_takes_references_from_requirements(
    session: AsyncSession,
) -> None:
    order_id = await generating_order(session, references=[BOOK])
    llm = await _generate_all(session, order_id)

    document = await assemble_document(session, order_id)

    assert document.references == [BOOK]
    # Источники и данные титульного листа модели не передаются.
    for call in llm.calls:
        request = repr(call)
        assert BOOK not in request
        for value in PERSONAL_VALUES:
            assert value not in request


# --- Стоимость вызова (Stage 3.12) ----------------------------------------------------

PRICED_USAGE = LLMUsage(
    input_tokens=1000,
    output_tokens=500,
    cached_tokens=2000,
    cache_creation_tokens=3000,
    cache_write_5m_tokens=1000,
    cache_write_1h_tokens=2000,
)
# 1000·2 + 500·10 + 2000·0,10 + 1000·2,50 + 2000·4 = 17 700 (USD·10⁻⁶).
PRICED_COST = Decimal("0.017700")


async def test_call_cost_recorded(session: AsyncSession) -> None:
    order_id = await generating_order(session)

    await generate_next_section(session, FakeLLMClient(priced(good(0), PRICED_USAGE)), order_id)

    [call] = await _calls(session, order_id)
    assert call.model == PRICED_MODEL
    assert (call.cached_tokens, call.cache_creation_tokens) == (2000, 3000)
    assert call.cost_usd == PRICED_COST


async def test_cache_write_without_ttl_breakdown_cost_is_null(session: AsyncSession) -> None:
    """Запись в кэш без разбивки по сроку хранения: тариф неизвестен — NULL (OD-6)."""
    order_id = await generating_order(session)
    usage = LLMUsage(input_tokens=1000, output_tokens=800, cache_creation_tokens=5000)

    await generate_next_section(session, FakeLLMClient(priced(good(0), usage)), order_id)

    [call] = await _calls(session, order_id)
    assert call.cache_creation_tokens == 5000
    assert call.cost_usd is None


async def test_call_without_response_has_no_cost(session: AsyncSession) -> None:
    order_id = await generating_order(session)

    await generate_next_section(session, FakeLLMClient(LLMTemporaryError("timeout")), order_id)

    [call] = await _calls(session, order_id)
    assert (call.status, call.error) == (LLMCallStatus.ERROR, "timeout")
    assert (call.input_tokens, call.output_tokens, call.cost_usd) == (0, 0, None)


async def test_each_attempt_has_its_own_cost(session: AsyncSession) -> None:
    """Регенерация по объёму — отдельный вызов со своей стоимостью (не суммируется)."""
    order_id = await generating_order(session)
    llm = FakeLLMClient(priced(too_short(0), PRICED_USAGE), priced(good(0), PRICED_USAGE))

    await generate_next_section(session, llm, order_id)

    calls = await _calls(session, order_id)
    assert [call.cost_usd for call in calls] == [PRICED_COST, PRICED_COST]
