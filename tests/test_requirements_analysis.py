"""Анализ материалов: черновик ТЗ, повторы, лимит вызовов, устаревший результат, восстановление.

БД — настоящая PostgreSQL (одно соединение, SAVEPOINT — см. conftest); гонки между
независимыми сессиями — в test_analysis_concurrency.py. LLM — FakeLLMClient, сеть не
используется.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import anthropic
import httpx2
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LLMCall, Order, OrderMaterialText, OrderRequirementsVersion
from app.domain.enums import LLMCallStatus, OrderStatus, VersionStatus
from app.domain.errors import AnalysisLimitReachedError, InvalidStatusTransitionError
from app.domain.requirements import MAX_ANALYSIS_LLM_CALLS
from app.llm.anthropic import AnthropicLLMClient
from app.llm.base import LLMPermanentError, LLMTemporaryError, LLMUsage, StopReason
from app.services import order_materials as materials
from app.services import orders
from app.services import requirements_analysis as analysis
from app.services.requirements_analysis import (
    NOT_COMPLETED,
    AnalysisOutcome,
    AnalysisOutcomeKind,
    AnalysisRecovery,
    analyze_requirements,
    find_orders_for_analysis,
    restart_analysis,
    run_analysis_pass,
)
from app.services.users import register_or_update_user
from fake_llm import PRICED_MODEL, FakeLLMClient, priced, response_json, result

S = OrderStatus
K = AnalysisOutcomeKind

MATERIAL = (
    "Задание на производственную практику.\n"
    "Тема: Анализ бизнес-процессов отдела продаж.\n"
    "Место прохождения: ООО «Ёлка». Сроки практики: с 01.07.2026 по 28.07.2026."
)
GOOD = result(
    response_json(
        topic=(
            "Анализ бизнес-процессов отдела продаж",
            "Тема: Анализ бизнес-процессов отдела продаж.",
        ),
        practice_type=("производственную", "на производственную практику"),
        practice_place=("ООО «Ёлка»", "Место прохождения: ООО «Ёлка»"),
        practice_start=("2026-07-01", "с 01.07.2026"),
        practice_end=("2026-07-28", "по 28.07.2026"),
    )
)


async def _order(session: AsyncSession, *texts: str, tg_id: int = 1) -> int:
    """Заказ в ANALYZING_MATERIALS с обработанными материалами (зафиксировано)."""
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    order = await orders.create_order(session, user_id=user.id)
    for status in (S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES):
        order = await orders.change_status(session, order.id, status)
    for text in texts or (MATERIAL,):
        material = await materials.add_material(
            session, order.id, filename="task.txt", mime_type=None, size_bytes=10
        )
        await materials.mark_processing(session, material.id)
        await materials.mark_processed(session, material.id)
        session.add(
            OrderMaterialText(
                material_id=material.id, text=text, char_count=len(text), extractor="t"
            )
        )
        await session.flush()
    await materials.finish_materials(session, order.id)
    order_id = order.id
    await session.commit()
    return order_id


async def _fresh(session: AsyncSession, order_id: int) -> Order:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    return order


async def _calls(session: AsyncSession, order_id: int) -> list[LLMCall]:
    stmt = select(LLMCall).where(LLMCall.order_id == order_id).order_by(LLMCall.id)
    return list(await session.scalars(stmt.execution_options(populate_existing=True)))


async def _versions(session: AsyncSession, order_id: int) -> list[OrderRequirementsVersion]:
    return await orders.get_requirements_versions(session, order_id)


# --- Успешный анализ ----------------------------------------------------------------


async def test_analysis_creates_draft_and_asks_for_missing_data(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(GOOD)

    outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.COMPLETED
    assert outcome.status is S.WAITING_FOR_MISSING_DATA
    # Вуз и ФИО у модели не запрашиваются: их всегда нужно уточнить.
    assert outcome.missing == ("university", "student_name")
    order = await _fresh(session, order_id)
    assert order.status is S.WAITING_FOR_MISSING_DATA
    assert (order.step_started_at, order.step_attempts) == (None, 1)
    [draft] = await _versions(session, order_id)
    assert (draft.version, draft.status) == (1, VersionStatus.DRAFT)
    assert draft.content["topic"] == "Анализ бизнес-процессов отдела продаж"
    assert draft.content["practice_start"] == "2026-07-01"
    assert draft.content["student_name"] is None
    [call] = await _calls(session, order_id)
    assert (call.status, call.error, call.step) == (LLMCallStatus.SUCCESS, None, analysis.LLM_STEP)
    assert (call.provider, call.model) == ("fake", "fake-model-2026")
    assert (call.input_tokens, call.output_tokens, call.cached_tokens) == (1200, 150, 100)
    assert call.latency_ms == 850


async def test_request_contains_materials_and_schema_without_personal_fields(
    session: AsyncSession,
) -> None:
    order_id = await _order(session, MATERIAL, "Второй материал: методические указания.")
    llm = FakeLLMClient(GOOD)

    await analyze_requirements(session, llm, order_id)

    [request] = llm.calls
    assert request["max_tokens"] == 16_000
    assert MATERIAL in request["prompt"] and "методические указания" in request["prompt"]
    assert request["prompt"].index('number="1"') < request["prompt"].index('number="2"')
    assert "task.txt" not in request["prompt"]  # имена файлов модели не передаются
    schema_fields = request["json_schema"]["properties"]["fields"]["properties"]
    assert {"student_name", "university", "supervisor"}.isdisjoint(schema_fields)


async def test_value_without_evidence_in_materials_is_missing(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(
        result(
            response_json(
                topic=("Анализ", "Тема: Анализ бизнес-процессов"),
                practice_place=("ООО «Сосна»", "Место прохождения: ООО «Сосна»"),
            )
        )
    )

    outcome = await analyze_requirements(session, llm, order_id)

    assert "practice_place" in outcome.missing
    [draft] = await _versions(session, order_id)
    assert draft.content["practice_place"] is None
    assert draft.content["topic"] == "Анализ"


async def test_nothing_found_still_creates_draft(session: AsyncSession) -> None:
    order_id = await _order(session)

    outcome = await analyze_requirements(session, FakeLLMClient(result()), order_id)

    assert outcome.kind is K.COMPLETED
    assert outcome.found == ()
    assert len(outcome.missing) == 7
    assert len(await _versions(session, order_id)) == 1


async def test_complete_data_goes_to_confirmation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ветка «недостающих нет» (при нынешнем REQUIRED_FIELDS недостижима: нет ФИО и вуза)."""
    monkeypatch.setattr(analysis, "missing_fields", lambda content: [])
    order_id = await _order(session)

    outcome = await analyze_requirements(session, FakeLLMClient(GOOD), order_id)

    assert (outcome.kind, outcome.status, outcome.missing) == (
        K.COMPLETED,
        S.WAITING_FOR_DATA_CONFIRMATION,
        (),
    )
    assert (await _fresh(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION


@pytest.mark.parametrize(
    ("pages", "target", "unsupported"),
    [
        (None, None, None),
        (3, 3, None),
        (25, 25, None),
        (120, 120, None),
        (1, None, 1),
        (2, None, 2),
        (121, None, 121),
        (500, None, 500),
        (501, None, 501),
        (10**499 + 7, None, 10**499 + 7),
    ],
)
async def test_volume_from_materials_is_kept_supported_or_not(
    session: AsyncSession, pages: int | None, target: int | None, unsupported: int | None
) -> None:
    """Stage 3.10: объём вне 3..120 не теряется — он в unsupported_target_pages."""
    quote = str(pages) if pages is not None and pages > 10**6 else f"Объём отчёта: {pages} стр."
    fields = {} if pages is None else {"target_pages": (pages, quote)}
    order_id = await _order(session, f"{MATERIAL}\n{quote}")

    outcome = await analyze_requirements(
        session, FakeLLMClient(result(response_json(**fields))), order_id
    )

    assert outcome.kind is K.COMPLETED
    [draft] = await _versions(session, order_id)
    assert draft.content["target_pages"] == target
    assert draft.content["unsupported_target_pages"] == unsupported


async def test_references_from_materials_are_saved_in_draft(session: AsyncSession) -> None:
    """Stage 3.11: источники — только с цитатой из материалов; выдуманный не сохраняется."""
    book = "Иванов И. И. Бизнес-процессы. — М.: Наука, 2020."
    order_id = await _order(session, f"{MATERIAL}\nЛитература: {book}")
    response = response_json(
        references=[(book, f"Литература: {book}"), ("Выдуманный источник, 2024.", "нет такого")]
    )

    outcome = await analyze_requirements(session, FakeLLMClient(result(response)), order_id)

    assert outcome.kind is K.COMPLETED
    [draft] = await _versions(session, order_id)
    assert draft.content["references"] == [book]
    prompt = " ".join(analysis.SYSTEM_PROMPT.split())
    assert "Не добавляй источники, которых в материалах нет" in prompt


async def test_oversized_materials_are_truncated_explicitly(session: AsyncSession) -> None:
    first = "А" * 300_000
    second = MATERIAL + " " + "Б" * 200_000  # не помещается целиком
    third = "Тема: Анализ складской логистики."  # не входит вовсе
    order_id = await _order(session, first, second, third)
    llm = FakeLLMClient(
        result(
            response_json(topic=("Анализ складской логистики", "Тема: Анализ складской логистики."))
        )
    )

    outcome = await analyze_requirements(session, llm, order_id)

    assert (outcome.truncated_material, outcome.omitted_materials) == (2, (3,))
    prompt = llm.calls[0]["prompt"]
    assert "Анализ складской логистики" not in prompt
    # Цитата из невошедшего материала не принимается.
    assert "topic" in outcome.missing


# --- Ошибки и повторы ---------------------------------------------------------------


async def test_invalid_response_is_retried_later(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(result("{ не JSON"), GOOD)

    first = await analyze_requirements(session, llm, order_id)

    assert (first.kind, first.error_code) == (K.RETRY_LATER, "invalid_response")
    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts, order.step_started_at) == (
        S.ANALYZING_MATERIALS,
        1,
        None,
    )
    assert await _versions(session, order_id) == []

    second = await analyze_requirements(session, llm, order_id)

    assert second.kind is K.COMPLETED
    assert (await _fresh(session, order_id)).step_attempts == 2
    calls = await _calls(session, order_id)
    assert [(c.status, c.error) for c in calls] == [
        (LLMCallStatus.ERROR, "invalid_response"),
        (LLMCallStatus.SUCCESS, None),
    ]


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (LLMTemporaryError("http_429"), "http_429"),
        (result(response_json(), StopReason.MAX_TOKENS), "max_tokens"),
        (result('{"fields": {}}'), "invalid_response"),
    ],
    ids=["rate-limit", "max-tokens", "wrong-structure"],
)
async def test_three_failed_attempts_lead_to_failed(
    session: AsyncSession, answer: object, code: str
) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(answer)  # type: ignore[arg-type]

    outcomes = [await analyze_requirements(session, llm, order_id) for _ in range(3)]

    assert [o.kind for o in outcomes] == [K.RETRY_LATER, K.RETRY_LATER, K.FAILED]
    assert (outcomes[-1].error_code, outcomes[-1].can_retry) == (code, True)
    order = await _fresh(session, order_id)
    assert (order.status, order.failed_from_status) == (S.FAILED, S.ANALYZING_MATERIALS)
    assert order.error == f"requirements_analysis: {code}"
    calls = await _calls(session, order_id)
    assert [(c.status, c.error) for c in calls] == [(LLMCallStatus.ERROR, code)] * 3
    if code == "max_tokens":  # ответ получен и оплачен: токены учтены
        assert all(c.output_tokens == 150 for c in calls)


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (LLMPermanentError("http_401"), "http_401"),
        (result(response_json(), StopReason.REFUSAL), "refusal"),
    ],
)
async def test_permanent_error_fails_at_once(
    session: AsyncSession, answer: object, code: str
) -> None:
    order_id = await _order(session)

    outcome = await analyze_requirements(session, FakeLLMClient(answer), order_id)  # type: ignore[arg-type]

    assert (outcome.kind, outcome.error_code) == (K.FAILED, code)
    assert len(await _calls(session, order_id)) == 1


async def test_order_error_has_no_material_text(session: AsyncSession) -> None:
    order_id = await _order(session)

    await analyze_requirements(
        session, FakeLLMClient(LLMPermanentError("http_400", MATERIAL)), order_id
    )

    order = await _fresh(session, order_id)
    assert order.error == "requirements_analysis: http_400"
    [call] = await _calls(session, order_id)
    assert call.error == "http_400"


async def test_restart_after_failure_runs_analysis_again(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(LLMPermanentError("http_400"), GOOD)
    await analyze_requirements(session, llm, order_id)

    order = await restart_analysis(session, order_id)
    await session.commit()

    assert (order.status, order.step_attempts, order.failed_from_status) == (
        S.ANALYZING_MATERIALS,
        0,
        None,
    )
    outcome = await analyze_requirements(session, llm, order_id)
    assert outcome.kind is K.COMPLETED
    assert len(await _calls(session, order_id)) == 2


@pytest.mark.parametrize("status", [S.ANALYZING_MATERIALS, S.WAITING_FOR_MISSING_DATA])
async def test_restart_requires_analysis_failure(session: AsyncSession, status: S) -> None:
    order_id = await _order(session)
    if status is S.WAITING_FOR_MISSING_DATA:
        await analyze_requirements(session, FakeLLMClient(GOOD), order_id)

    with pytest.raises(InvalidStatusTransitionError):
        await restart_analysis(session, order_id)
    assert (await _fresh(session, order_id)).status is status


async def test_llm_call_limit_across_restarts(session: AsyncSession) -> None:
    """3 запуска по 3 попытки = 9 вызовов; дальше перезапуск запрещён."""
    order_id = await _order(session)
    llm = FakeLLMClient(LLMTemporaryError("http_503"))

    outcomes: list[AnalysisOutcome] = []
    for run in range(3):
        if run:
            await restart_analysis(session, order_id)
            await session.commit()
        outcomes += [await analyze_requirements(session, llm, order_id) for _ in range(3)]

    assert [o.kind for o in outcomes].count(K.FAILED) == 3
    assert [o.can_retry for o in outcomes if o.kind is K.FAILED] == [True, True, False]
    assert len(llm.calls) == MAX_ANALYSIS_LLM_CALLS == 9
    assert len(await _calls(session, order_id)) == 9
    with pytest.raises(AnalysisLimitReachedError):
        await restart_analysis(session, order_id)
    assert (await _fresh(session, order_id)).status is S.FAILED


async def test_llm_call_limit_is_http_request_limit(session: AsyncSession) -> None:
    """Настоящий клиент на поддельном транспорте: 9 вызовов — ровно 9 HTTP-запросов.

    Ответ 503 SDK по умолчанию повторил бы; повторов нет, поэтому лимит вызовов
    модели на заказ равен числу запросов к Anthropic.
    """
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            503, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
        )

    llm = AnthropicLLMClient(
        api_key="test-key",
        model="claude-sonnet-5-5",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    order_id = await _order(session)

    for run in range(3):
        if run:
            await restart_analysis(session, order_id)
            await session.commit()
        for _ in range(3):
            await analyze_requirements(session, llm, order_id)
    # После исчерпания лимита новых запросов нет.
    with pytest.raises(AnalysisLimitReachedError):
        await restart_analysis(session, order_id)
    await session.rollback()
    assert (await analyze_requirements(session, llm, order_id)).kind is K.SKIPPED

    assert len(requests) == MAX_ANALYSIS_LLM_CALLS == 9
    assert len(await _calls(session, order_id)) == 9
    assert (await _fresh(session, order_id)).status is S.FAILED


async def test_limit_counts_interrupted_calls(session: AsyncSession) -> None:
    """Вызов, прерванный до записи результата, тоже расходует лимит."""
    order_id = await _order(session)
    session.add_all(
        LLMCall(
            order_id=order_id,
            step=analysis.LLM_STEP,
            provider="fake",
            model="m",
            status=LLMCallStatus.ERROR,
            error=NOT_COMPLETED,
        )
        for _ in range(MAX_ANALYSIS_LLM_CALLS)
    )
    await session.commit()
    llm = FakeLLMClient(GOOD)

    outcome = await analyze_requirements(session, llm, order_id)

    assert (outcome.kind, outcome.error_code, outcome.can_retry) == (
        K.FAILED,
        "llm_call_limit",
        False,
    )
    assert llm.calls == []


async def test_unexpected_error_leaves_attempt_to_recovery(session: AsyncSession) -> None:
    order_id = await _order(session)

    with pytest.raises(RuntimeError):
        await analyze_requirements(session, FakeLLMClient(RuntimeError("boom")), order_id)
    await session.rollback()

    order = await _fresh(session, order_id)
    assert (order.status, order.step_attempts) == (S.ANALYZING_MATERIALS, 1)
    assert order.step_started_at is not None  # будет признана прерванной
    [call] = await _calls(session, order_id)
    assert (call.status, call.error) == (LLMCallStatus.ERROR, NOT_COMPLETED)


# --- Устаревший результат, отмена, занято -------------------------------------------


async def test_result_of_taken_over_attempt_is_discarded(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(GOOD)

    async def taken_over() -> None:
        await session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(step_attempts=Order.step_attempts + 1, step_started_at=datetime.now(UTC))
        )
        await session.commit()

    llm.on_complete = taken_over
    outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    assert await _versions(session, order_id) == []
    assert (await _fresh(session, order_id)).status is S.ANALYZING_MATERIALS
    [call] = await _calls(session, order_id)
    assert call.status is LLMCallStatus.SUCCESS  # вызов состоялся и учтён


async def test_cancel_during_llm_call_discards_result(session: AsyncSession) -> None:
    order_id = await _order(session)
    llm = FakeLLMClient(GOOD)

    async def cancel() -> None:
        await orders.change_status(session, order_id, S.CANCELLED)
        await session.commit()

    llm.on_complete = cancel
    outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.DISCARDED
    order = await _fresh(session, order_id)
    assert (order.status, order.step_started_at) == (S.CANCELLED, None)
    assert await _versions(session, order_id) == []
    assert len(await _calls(session, order_id)) == 1


async def test_running_attempt_makes_second_start_busy(session: AsyncSession) -> None:
    order_id = await _order(session)
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(step_attempts=1, step_started_at=datetime.now(UTC))
    )
    await session.commit()
    llm = FakeLLMClient(GOOD)

    outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.BUSY
    assert llm.calls == []


@pytest.mark.parametrize("status", [S.WAITING_FOR_MISSING_DATA, S.CANCELLED])
async def test_order_not_in_analysis_is_skipped(session: AsyncSession, status: S) -> None:
    order_id = await _order(session)
    await orders.change_status(
        session,
        order_id,
        S.CANCELLED if status is S.CANCELLED else S.WAITING_FOR_MISSING_DATA,
    )
    await session.commit()
    llm = FakeLLMClient(GOOD)

    outcome = await analyze_requirements(session, llm, order_id)

    assert outcome.kind is K.SKIPPED
    assert llm.calls == []


async def test_stale_attempt_is_counted_and_retried(session: AsyncSession) -> None:
    order_id = await _order(session)
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(step_attempts=1, step_started_at=datetime.now(UTC) - timedelta(minutes=20))
    )
    await session.commit()

    outcome = await analyze_requirements(session, FakeLLMClient(GOOD), order_id)

    assert outcome.kind is K.COMPLETED
    assert (await _fresh(session, order_id)).step_attempts == 2


async def test_stale_third_attempt_fails_without_new_call(session: AsyncSession) -> None:
    order_id = await _order(session)
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(step_attempts=3, step_started_at=datetime.now(UTC) - timedelta(minutes=20))
    )
    await session.commit()
    llm = FakeLLMClient(GOOD)

    outcome = await analyze_requirements(session, llm, order_id)

    assert (outcome.kind, outcome.error_code) == (K.FAILED, "attempts_exhausted")
    assert llm.calls == []


# --- Поля шага в change_status ------------------------------------------------------


async def test_step_fields_reset_on_status_change(session: AsyncSession) -> None:
    order_id = await _order(session)
    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(step_attempts=2, step_started_at=datetime.now(UTC))
    )
    await session.commit()

    order = await orders.change_status(session, order_id, S.FAILED, error="x")
    assert (order.step_attempts, order.step_started_at) == (2, None)  # для диагностики
    order = await orders.change_status(session, order_id, S.ANALYZING_MATERIALS)
    assert (order.step_attempts, order.step_started_at) == (0, None)


# --- Восстановление -----------------------------------------------------------------


async def _set(session: AsyncSession, order_id: int, **values: object) -> None:
    await session.execute(update(Order).where(Order.id == order_id).values(**values))
    await session.commit()


async def test_recovery_picks_waiting_orders_only(session: AsyncSession) -> None:
    fresh = await _order(session, tg_id=1)
    old = await _order(session, tg_id=2)
    retry = await _order(session, tg_id=3)
    stale = await _order(session, tg_id=4)
    running = await _order(session, tg_id=5)
    await _set(session, old, updated_at=func.now() - timedelta(minutes=5))
    await _set(session, retry, step_attempts=1)
    await _set(
        session,
        stale,
        step_attempts=1,
        step_started_at=datetime.now(UTC) - timedelta(minutes=11),
    )
    await _set(session, running, step_attempts=1, step_started_at=datetime.now(UTC))

    found = await find_orders_for_analysis(session, limit=10)

    assert set(found) == {old, retry, stale}
    assert fresh not in found and running not in found


class ByMarker(FakeLLMClient):
    """Ответ выбирается по метке в тексте материалов: у каждого заказа своя."""

    def __init__(self, answers: dict[str, object]) -> None:
        super().__init__()
        self.by_marker = answers

    async def complete(self, **kwargs: object) -> object:  # type: ignore[override]
        prompt = str(kwargs["prompt"])
        answer = next(a for marker, a in self.by_marker.items() if marker in prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def test_recovery_pass_notifies_final_outcomes(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    done = await _order(session, MATERIAL + " [done]", tg_id=11)
    failing = await _order(session, MATERIAL + " [failing]", tg_id=12)
    retrying = await _order(session, MATERIAL + " [retrying]", tg_id=13)
    for order_id in (done, failing, retrying):
        await _set(session, order_id, updated_at=func.now() - timedelta(minutes=5))
    sent: list[tuple[int, int, K]] = []

    async def notify(chat_id: int, outcome: AnalysisOutcome) -> None:
        sent.append((chat_id, outcome.order_id, outcome.kind))

    llm = ByMarker(
        {
            "[done]": GOOD,
            "[failing]": LLMPermanentError("http_400"),
            "[retrying]": LLMTemporaryError("http_429"),
        }
    )

    outcomes = await run_analysis_pass(sessionmaker, llm, notify, batch=5)

    assert {(o.order_id, o.kind) for o in outcomes} == {
        (done, K.COMPLETED),
        (failing, K.FAILED),
        (retrying, K.RETRY_LATER),
    }
    # Повтор позже не сообщается; итоговые — пользователю заказа.
    assert sorted(sent) == sorted([(11, done, K.COMPLETED), (12, failing, K.FAILED)])


async def test_recovery_runs_first_pass_at_start(
    session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """После «рестарта»: заказ, оставшийся в анализе, разбирает первый проход."""
    order_id = await _order(session)
    await _set(
        session,
        order_id,
        step_attempts=1,
        step_started_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    sent: list[AnalysisOutcome] = []

    async def notify(chat_id: int, outcome: AnalysisOutcome) -> None:
        sent.append(outcome)

    recovery = AnalysisRecovery(sessionmaker, FakeLLMClient(GOOD), notify, interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(200):
            if sent:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert [o.kind for o in sent] == [K.COMPLETED]
    assert not recovery.running
    assert (await _fresh(session, order_id)).status is S.WAITING_FOR_MISSING_DATA


def test_recovery_constants() -> None:
    assert analysis.RECOVERY_INTERVAL_SECONDS == 60.0
    assert analysis.RECOVERY_BATCH == 5
    assert timedelta(minutes=10) == analysis.STEP_STALE_AFTER
    assert timedelta(minutes=1) == analysis.NEW_ORDER_GRACE
    assert analysis.MAX_TOKENS == 16_000


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


async def test_call_cost_and_cache_write_recorded(session: AsyncSession) -> None:
    order_id = await _order(session)

    await analyze_requirements(session, FakeLLMClient(priced(GOOD, PRICED_USAGE)), order_id)

    [call] = await _calls(session, order_id)
    assert call.model == PRICED_MODEL
    assert (call.cached_tokens, call.cache_creation_tokens) == (2000, 3000)
    assert call.cost_usd == PRICED_COST


async def test_unknown_model_cost_is_null(session: AsyncSession) -> None:
    order_id = await _order(session)

    await analyze_requirements(session, FakeLLMClient(GOOD), order_id)

    [call] = await _calls(session, order_id)
    assert call.model == "fake-model-2026"
    assert call.cost_usd is None  # NULL, а не 0


async def test_call_without_response_has_no_cost(session: AsyncSession) -> None:
    """Ответа нет — usage неизвестен: стоимость NULL, хотя провайдер мог вызов оплатить."""
    order_id = await _order(session)

    await analyze_requirements(session, FakeLLMClient(LLMTemporaryError("http_529")), order_id)

    [call] = await _calls(session, order_id)
    assert (call.status, call.error) == (LLMCallStatus.ERROR, "http_529")
    assert (call.input_tokens, call.output_tokens, call.cost_usd) == (0, 0, None)
