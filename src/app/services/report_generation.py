"""Генерация текста отчёта по разделам (заказ в GENERATING).

Устроено как анализ материалов и составление плана: одна попытка — один раздел.

1. Захват — короткая транзакция под блокировкой заказа: статус, идущая или
   зависшая попытка, следующий несгенерированный раздел, лимиты; номер попытки,
   начало попытки и строка llm_calls (до вызова: бюджет вызовов не обойти сбоем).
2. Чтение входа и вызов модели — без открытой транзакции и блокировок.
3. Сохранение — снова под блокировкой: текст принимается, только если заказ всё
   ещё в той же попытке и раздел ещё не сохранён.

Лимиты: попыток подряд на раздел — MAX_SECTION_ATTEMPTS (счётчик обнуляется, когда
раздел сохранён); вызовов модели на весь отчёт — max_report_calls(N), считаются по
llm_calls под блокировкой заказа, включая прерванные. Объём вне допуска — не больше
одной немедленной регенерации в одном вызове generate_next_section; хранимое
состояние её не помнит, а две регенерации на разделе невозможны из-за лимита
попыток. Когда все разделы сохранены, заказ остаётся в GENERATING: рендеринг и
доставка — следующие этапы.

В модель уходят поля ТЗ без персональных (белый список плана), план и материалы.
В логи, order.error и llm_calls — только коды: без текста разделов, ТЗ и материалов.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    LLMCall,
    Order,
    OrderMaterial,
    OrderMaterialText,
    OrderPlanVersion,
    OrderReportSection,
)
from app.domain.enums import LLMCallStatus, MaterialProcessingStatus, OrderStatus, VersionStatus
from app.domain.errors import VersionNotFoundError
from app.domain.plan import plan_requirements
from app.domain.report_generation import (
    MAX_SECTION_ATTEMPTS,
    MAX_SECTION_PAGES,
    REPORT_PROMPT,
    SECTION_JSON_SCHEMA,
    InvalidSectionResponse,
    ReportOutcome,
    ReportOutcomeKind,
    SectionSizeOutOfRange,
    SectionText,
    check_size,
    max_report_calls,
    render_context,
    render_task,
    render_written_section,
    validate_section,
)
from app.domain.requirements import AnalysisMaterial, build_analysis_input
from app.generation.schemas import DocumentContent, Section
from app.llm.base import LLMClient, LLMError, LLMResult, PromptBlock, StopReason
from app.services.orders import (
    change_status,
    get_plan_versions,
    get_requirements_versions,
    lock_order,
)
from app.services.periodic import PeriodicPass

logger = logging.getLogger(__name__)

S = OrderStatus
K = ReportOutcomeKind

LLM_STEP = "report_generation"
MAX_TOKENS = 16_000
# Попытка дольше этого считается прерванной: тайм-аут запроса 300 с без повторов
# SDK, чтение данных и сохранение — с запасом.
STEP_STALE_AFTER = timedelta(minutes=10)
RECOVERY_INTERVAL_SECONDS = 60.0
RECOVERY_BATCH = 5
NOT_COMPLETED = "not_completed"
# Разделы одного заказа идут с паузами (один раздел за проход, в проходе — другие
# заказы): кэш промпта на час, а не на 5 минут.
CACHE_TTL = "1h"
OUT_OF_RANGE = "out_of_range"


@dataclass(frozen=True)
class _Claim:
    order_id: int
    attempt: int
    # Вместе с номером однозначно определяет попытку.
    started_at: datetime
    call_id: int
    plan_version_id: int
    section_index: int
    section_count: int


@dataclass(frozen=True)
class _Failure:
    code: str
    temporary: bool
    # Для out_of_range: объём отклонённого ответа.
    actual_size: int | None = None


@dataclass
class _Attempt:
    """Итог внешней части попытки: что получено и чем закончилось."""

    result: LLMResult | None = None
    text: SectionText | None = None
    failure: _Failure | None = None


@dataclass(frozen=True)
class _Regenerate:
    """Объём вне допуска: регенерировать раздел с указанием объёма."""

    section_index: int
    actual_size: int


async def generate_next_section(
    session: AsyncSession, llm: LLMClient, order_id: int
) -> ReportOutcome:
    """Следующий несгенерированный раздел заказа в GENERATING (захват → модель → сохранение).

    Объём вне допуска — одна немедленная регенерация с новым захватом (новая строка
    llm_calls, все лимиты проверяются заново); её объём вне допуска — FAILED.
    """
    claimed = await _claim(session, llm, order_id)
    if isinstance(claimed, ReportOutcome):
        return claimed
    attempt = await _run_attempt(session, llm, claimed, previous_size=None)
    finished = await _finish(session, claimed, attempt, regeneration=False)
    if not isinstance(finished, _Regenerate):
        return finished

    claimed = await _claim(session, llm, order_id)
    if isinstance(claimed, ReportOutcome):
        return claimed
    if claimed.section_index != finished.section_index:
        # Раздел успели сохранить в другом месте: это обычная попытка другого раздела.
        attempt = await _run_attempt(session, llm, claimed, previous_size=None)
        result = await _finish(session, claimed, attempt, regeneration=False)
    else:
        attempt = await _run_attempt(session, llm, claimed, previous_size=finished.actual_size)
        result = await _finish(session, claimed, attempt, regeneration=True)
    # Регенерация регенерации невозможна: после неё — FAILED.
    assert not isinstance(result, _Regenerate)
    return result


async def count_report_calls(session: AsyncSession, order_id: int) -> int:
    """Начатые вызовы модели на генерацию отчёта (включая прерванные)."""
    stmt = select(func.count()).where(LLMCall.order_id == order_id, LLMCall.step == LLM_STEP)
    return int(await session.scalar(stmt) or 0)


async def find_orders_for_generation(session: AsyncSession, *, limit: int) -> list[int]:
    """Заказы в GENERATING без идущей попытки, у которых сохранены не все разделы плана."""
    stale_before = datetime.now(UTC) - STEP_STALE_AFTER
    latest_version = (
        select(func.max(OrderPlanVersion.version))
        .where(OrderPlanVersion.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    saved = (
        select(func.count())
        .where(OrderReportSection.plan_version_id == OrderPlanVersion.id)
        .correlate(OrderPlanVersion)
        .scalar_subquery()
    )
    stmt = (
        select(Order.id)
        .join(OrderPlanVersion, OrderPlanVersion.order_id == Order.id)
        .where(
            Order.status == S.GENERATING,
            or_(Order.step_started_at.is_(None), Order.step_started_at < stale_before),
            OrderPlanVersion.version == latest_version,
            OrderPlanVersion.status == VersionStatus.CONFIRMED,
            saved < func.jsonb_array_length(OrderPlanVersion.content["sections"]),
        )
        .order_by(Order.updated_at, Order.id)
        .limit(limit)
    )
    order_ids = list(await session.scalars(stmt))
    await session.commit()
    return order_ids


async def assemble_document(session: AsyncSession, order_id: int) -> DocumentContent:
    """Документ из сохранённых разделов: названия и порядок — из плана, текст — из таблицы.

    Только чтение. Титульного листа и списка литературы на этом этапе нет.

    Raises VersionNotFoundError — нет подтверждённого плана или раздела.
    """
    order = await session.get(Order, order_id)
    if order is None:
        raise VersionNotFoundError(f"Order {order_id} not found")
    plan = await _confirmed_plan(session, order_id)
    if plan is None:
        raise VersionNotFoundError(f"Order {order_id} has no confirmed plan")
    texts = await _saved_sections(session, plan.id)
    sections = []
    for index, planned in enumerate(plan.content["sections"]):
        content = texts.get(index)
        if content is None:
            raise VersionNotFoundError(f"Order {order_id}: section {index} is not generated")
        subsections = [
            Section(title=sub["title"], paragraphs=list(text["paragraphs"]))
            for sub, text in zip(
                planned.get("subsections") or [], content["subsections"], strict=True
            )
        ]
        sections.append(
            Section(
                title=planned["title"],
                paragraphs=list(content["paragraphs"]),
                subsections=subsections,
            )
        )
    requirements = await _confirmed_requirements(session, order_id)
    topic = (requirements or {}).get("topic") or ""
    return DocumentContent(
        doc_type=order.doc_type, title=topic, title_page={}, sections=sections, references=[]
    )


# --- Попытка ------------------------------------------------------------------------


async def _claim(session: AsyncSession, llm: LLMClient, order_id: int) -> _Claim | ReportOutcome:
    order = await lock_order(session, order_id)
    if order.status is not S.GENERATING:
        await session.rollback()
        return ReportOutcome(K.SKIPPED, order_id)

    if order.step_started_at is not None:
        if order.step_started_at > datetime.now(UTC) - STEP_STALE_AFTER:
            await session.rollback()
            return ReportOutcome(K.BUSY, order_id)
        # Попытка уже засчитана; её строка llm_calls остаётся «не завершён».
        logger.warning(
            "Report generation attempt interrupted: order_id=%s attempt=%s",
            order_id,
            order.step_attempts,
        )
        order.step_started_at = None

    plan = await _confirmed_plan(session, order_id)
    if plan is None:
        # После оплаты невозможно; защита от несогласованных данных.
        return await _fail(session, order_id, "no_confirmed_plan")
    sections = plan.content["sections"]
    saved = await _saved_sections(session, plan.id)
    index = next((i for i in range(len(sections)) if i not in saved), None)
    if index is None:
        await session.commit()
        return ReportOutcome(K.DONE, order_id)

    pages = sections[index].get("target_pages")
    if pages is not None and pages > MAX_SECTION_PAGES:
        # Не помещается в один ответ модели: вызов не тратится.
        return await _fail(session, order_id, "section_too_large", index)
    if await count_report_calls(session, order_id) >= max_report_calls(len(sections)):
        return await _fail(session, order_id, "llm_call_limit", index)
    if order.step_attempts >= MAX_SECTION_ATTEMPTS:
        return await _fail(session, order_id, "attempts_exhausted", index)

    started_at = datetime.now(UTC)
    order.step_attempts += 1
    order.step_started_at = started_at
    call = LLMCall(
        order_id=order_id,
        step=LLM_STEP,
        provider=llm.provider,
        model=llm.model,
        status=LLMCallStatus.ERROR,
        error=NOT_COMPLETED,
    )
    session.add(call)
    await session.flush()
    claim = _Claim(
        order_id=order_id,
        attempt=order.step_attempts,
        started_at=started_at,
        call_id=call.id,
        plan_version_id=plan.id,
        section_index=index,
        section_count=len(sections),
    )
    await session.commit()
    return claim


async def _run_attempt(
    session: AsyncSession, llm: LLMClient, claim: _Claim, *, previous_size: int | None
) -> _Attempt:
    loaded = await _load_input(session, claim)
    await session.commit()  # во время вызова модели транзакция не открыта
    attempt = _Attempt()
    if loaded is None:
        attempt.failure = _Failure("no_requirements", temporary=False)
        return attempt
    plan, prefix = loaded
    section = plan["sections"][claim.section_index]

    try:
        result = await llm.complete(
            system=REPORT_PROMPT,
            prompt=render_task(plan, claim.section_index, previous_size=previous_size),
            prefix=prefix,
            max_tokens=MAX_TOKENS,
            json_schema=SECTION_JSON_SCHEMA,
        )
    except LLMError as exc:
        attempt.failure = _Failure(exc.code, temporary=exc.temporary)
        return attempt
    attempt.result = result

    if result.stop_reason is StopReason.REFUSAL:
        attempt.failure = _Failure("refusal", temporary=False)
    elif result.stop_reason is StopReason.MAX_TOKENS:
        attempt.failure = _Failure("max_tokens", temporary=True)
    else:
        try:
            text = validate_section(json.loads(result.text), section, claim.section_index + 1)
            check_size(text.char_count, section.get("target_pages"))
        except SectionSizeOutOfRange as exc:
            attempt.failure = _Failure(OUT_OF_RANGE, temporary=True, actual_size=exc.actual)
        except (ValueError, InvalidSectionResponse) as exc:  # JSONDecodeError — ValueError
            logger.warning(
                "Invalid report section response: order_id=%s section=%s error=%s",
                claim.order_id,
                claim.section_index,
                type(exc).__name__,
            )
            attempt.failure = _Failure("invalid_response", temporary=True)
        else:
            attempt.text = text
    return attempt


async def _finish(
    session: AsyncSession, claim: _Claim, attempt: _Attempt, *, regeneration: bool
) -> ReportOutcome | _Regenerate:
    order = await lock_order(session, claim.order_id)
    await _record_call(session, claim.call_id, attempt)
    if (
        order.status is not S.GENERATING
        or order.step_attempts != claim.attempt
        or order.step_started_at != claim.started_at
        or await _section_saved(session, claim.plan_version_id, claim.section_index)
    ):
        logger.warning(
            "Stale report section result discarded: order_id=%s section=%s attempt=%s "
            "current=%s/%s",
            claim.order_id,
            claim.section_index,
            claim.attempt,
            order.status,
            order.step_attempts,
        )
        await session.commit()
        return ReportOutcome(K.DISCARDED, claim.order_id, claim.section_index)

    if attempt.failure is not None:
        failure = attempt.failure
        budget_left = await count_report_calls(session, claim.order_id) < max_report_calls(
            claim.section_count
        )
        attempts_left = claim.attempt < MAX_SECTION_ATTEMPTS
        logger.warning(
            "Report section attempt failed: order_id=%s section=%s attempt=%s code=%s temporary=%s",
            claim.order_id,
            claim.section_index,
            claim.attempt,
            failure.code,
            failure.temporary,
        )
        if failure.code == OUT_OF_RANGE:
            if regeneration or not attempts_left or not budget_left:
                return await _fail(
                    session, claim.order_id, "size_out_of_range", claim.section_index
                )
            assert failure.actual_size is not None
            order.step_started_at = None
            await session.commit()
            return _Regenerate(claim.section_index, failure.actual_size)
        if not failure.temporary or not attempts_left or not budget_left:
            return await _fail(session, claim.order_id, failure.code, claim.section_index)
        order.step_started_at = None
        await session.commit()
        return ReportOutcome(
            K.RETRY_LATER, claim.order_id, claim.section_index, error_code=failure.code
        )

    text = attempt.text
    assert text is not None
    session.add(
        OrderReportSection(
            order_id=claim.order_id,
            plan_version_id=claim.plan_version_id,
            section_index=claim.section_index,
            content=text.content,
            char_count=text.char_count,
        )
    )
    # Раздел сохранён — это прогресс: попытки следующего раздела считаются заново.
    order.step_attempts = 0
    order.step_started_at = None
    await session.flush()
    await session.commit()
    return ReportOutcome(K.SAVED, claim.order_id, claim.section_index)


async def _fail(
    session: AsyncSession, order_id: int, code: str, section_index: int | None = None
) -> ReportOutcome:
    # В order.error — только код: без текста разделов, ТЗ и материалов.
    await change_status(session, order_id, S.FAILED, error=f"{LLM_STEP}: {code}")
    await session.commit()
    return ReportOutcome(K.FAILED, order_id, section_index, error_code=code)


async def _record_call(session: AsyncSession, call_id: int, attempt: _Attempt) -> None:
    call = await session.get(LLMCall, call_id, populate_existing=True)
    if call is None:  # строку учёта удалили вручную: обновлять нечего
        return
    result = attempt.result
    if result is not None:
        call.model = result.model[:64]
        call.input_tokens = result.usage.input_tokens
        call.output_tokens = result.usage.output_tokens
        call.cached_tokens = result.usage.cached_tokens
        call.cache_creation_tokens = result.usage.cache_creation_tokens
        call.latency_ms = result.latency_ms
    if attempt.failure is None:
        call.status, call.error = LLMCallStatus.SUCCESS, None
    else:
        call.status, call.error = LLMCallStatus.ERROR, attempt.failure.code
    await session.flush()


# --- Данные -------------------------------------------------------------------------


async def _confirmed_plan(session: AsyncSession, order_id: int) -> OrderPlanVersion | None:
    """Действующий план — последняя версия, и она подтверждена."""
    versions = await get_plan_versions(session, order_id)
    if not versions or versions[-1].status is not VersionStatus.CONFIRMED:
        return None
    return versions[-1]


async def _confirmed_requirements(session: AsyncSession, order_id: int) -> dict[str, Any] | None:
    versions = await get_requirements_versions(session, order_id)
    if not versions or versions[-1].status is not VersionStatus.CONFIRMED:
        return None
    return dict(versions[-1].content)


async def _saved_sections(session: AsyncSession, plan_version_id: int) -> dict[int, Any]:
    stmt = select(OrderReportSection.section_index, OrderReportSection.content).where(
        OrderReportSection.plan_version_id == plan_version_id
    )
    return {index: content for index, content in (await session.execute(stmt)).all()}


async def _section_saved(session: AsyncSession, plan_version_id: int, index: int) -> bool:
    stmt = select(OrderReportSection.id).where(
        and_(
            OrderReportSection.plan_version_id == plan_version_id,
            OrderReportSection.section_index == index,
        )
    )
    return await session.scalar(stmt) is not None


async def _load_input(
    session: AsyncSession, claim: _Claim
) -> tuple[dict[str, Any], list[PromptBlock]] | None:
    """План и блоки запроса: общий (ТЗ, план, материалы) и уже написанные разделы.

    Точки кэша — на общем блоке и на последнем написанном разделе: префикс
    следующего раздела продолжает префикс предыдущего. None — нет подтверждённого ТЗ.
    """
    requirements = await _confirmed_requirements(session, claim.order_id)
    plan_version = await session.get(OrderPlanVersion, claim.plan_version_id)
    if requirements is None or plan_version is None:
        return None
    plan = dict(plan_version.content)
    stmt = (
        select(OrderMaterial.upload_order, OrderMaterialText.text)
        .join(OrderMaterialText, OrderMaterialText.material_id == OrderMaterial.id)
        .where(
            OrderMaterial.order_id == claim.order_id,
            OrderMaterial.processing_status == MaterialProcessingStatus.PROCESSED,
            OrderMaterialText.char_count > 0,
        )
        .order_by(OrderMaterial.upload_order)
    )
    rows = (await session.execute(stmt)).all()
    materials = build_analysis_input([AnalysisMaterial(number, text) for number, text in rows])
    prefix = [
        PromptBlock(
            render_context(plan_requirements(requirements), plan, materials), cache_ttl=CACHE_TTL
        )
    ]
    saved = await _saved_sections(session, claim.plan_version_id)
    written = [i for i in range(claim.section_index) if i in saved]
    for i in written:
        text = render_written_section(i + 1, plan["sections"][i], saved[i])
        prefix.append(PromptBlock(text, cache_ttl=CACHE_TTL if i == written[-1] else None))
    return plan, prefix


# --- Восстановление -----------------------------------------------------------------


async def run_generation_pass(
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    *,
    batch: int = RECOVERY_BATCH,
) -> list[ReportOutcome]:
    """Один проход: по одному следующему разделу для ``batch`` ждущих заказов."""
    async with sessionmaker() as session:
        order_ids = await find_orders_for_generation(session, limit=batch)

    outcomes: list[ReportOutcome] = []
    for order_id in order_ids:
        try:
            async with sessionmaker() as session:
                outcomes.append(await generate_next_section(session, llm, order_id))
        except Exception as exc:
            # Без traceback: текст исключения БД может содержать параметры SQL
            # (текст раздела). Зависшую попытку подхватит следующий проход.
            logger.error(
                "Report generation recovery failed: order_id=%s error=%s",
                order_id,
                type(exc).__name__,
            )
    return outcomes


class ReportGenerationRecovery(PeriodicPass):
    """Периодический проход генерации отчёта: первый — сразу после start()."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        *,
        interval_seconds: float = RECOVERY_INTERVAL_SECONDS,
        batch: int = RECOVERY_BATCH,
    ) -> None:
        super().__init__(name="report-generation", interval_seconds=interval_seconds)
        self._sessionmaker = sessionmaker
        self._llm = llm
        self._batch = batch

    async def run_pass(self) -> list[ReportOutcome]:
        return await run_generation_pass(self._sessionmaker, self._llm, batch=self._batch)
