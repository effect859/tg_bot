import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, User
from app.domain.enums import DocType, OrderStatus, VersionStatus
from app.domain.errors import (
    ActiveOrderExistsError,
    DraftVersionExistsError,
    InvalidStatusTransitionError,
    OrderNotEditableError,
    OrderNotPaidError,
    OutdatedVersionError,
    PlanNotConfirmedError,
    RequirementsNotConfirmedError,
    VersionAlreadyConfirmedError,
)
from app.domain.order_flow import is_active
from app.domain.schemas import PlanContent, PlanSection
from app.services import orders as svc
from app.services.users import register_or_update_user
from billing_samples import pay_for_order

S = OrderStatus

REQUIREMENTS = {
    "topic": "Анализ бизнес-процессов отдела продаж",
    "practice_type": "производственная",
    "practice_place": "ООО «Ромашка»",
    "practice_start": "2026-06-01",
    "practice_end": "2026-06-28",
    "target_pages": 25,
}
PLAN_V1 = PlanContent(
    sections=[
        PlanSection(title="Введение", description="Цели и задачи практики", target_pages=2),
        PlanSection(
            title="Характеристика предприятия",
            requirements=["структура", "виды деятельности"],
            target_pages=8,
        ),
        PlanSection(title="Заключение", target_pages=2),
    ],
    total_pages=12,
)
PLAN_V2 = {
    "sections": [
        {"title": "Введение", "target_pages": 2},
        {"title": "Анализ отдела продаж", "target_pages": 12},
        {"title": "Характеристика предприятия", "target_pages": 6},
        {"title": "Заключение", "target_pages": 2},
    ],
    "total_pages": 22,
}


async def _user(session: AsyncSession, tg_id: int = 1) -> User:
    return await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )


async def _order(session: AsyncSession, tg_id: int = 1) -> Order:
    user = await _user(session, tg_id)
    return await svc.create_order(session, user_id=user.id)


async def _advance(session: AsyncSession, order: Order, *statuses: S) -> Order:
    for status in statuses:
        order = await svc.change_status(session, order.id, status)
    return order


async def _to_data_confirmation(session: AsyncSession, order: Order) -> Order:
    return await _advance(
        session,
        order,
        S.WAITING_FOR_CONSENT,
        S.WAITING_FOR_FILES,
        S.ANALYZING_MATERIALS,
        S.WAITING_FOR_DATA_CONFIRMATION,
    )


async def _confirm_requirements(session: AsyncSession, order: Order, content: dict = REQUIREMENTS):
    version = await svc.create_requirements_version(session, order.id, content)
    return await svc.confirm_requirements_version(session, order.id, version.id)


async def _confirm_plan(session: AsyncSession, order: Order, content=PLAN_V1):
    version = await svc.create_plan_version(session, order.id, content)
    return await svc.confirm_plan_version(session, order.id, version.id)


async def _to_payment_pending(session: AsyncSession, order: Order) -> Order:
    order = await _to_data_confirmation(session, order)
    await _confirm_requirements(session, order)
    order = await _advance(session, order, S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION)
    await _confirm_plan(session, order)
    return await _advance(session, order, S.PAYMENT_PENDING)


async def _to_completed(session: AsyncSession, order: Order) -> Order:
    order = await _to_payment_pending(session, order)
    # PAID — только через списание кредитов (OD-4).
    await pay_for_order(session, order.id)
    return await _advance(session, order, S.COMPLETED)


async def _insert_order(session: AsyncSession, user_id: int, status: S) -> None:
    """INSERT в обход сервиса — проверка ограничений самой БД."""
    failed_from = S.GENERATING.value if status is S.FAILED else None
    await session.execute(
        text(
            "INSERT INTO orders (user_id, doc_type, status, failed_from_status) "
            "VALUES (:user_id, 'practice_report', :status, :failed_from)"
        ),
        {"user_id": user_id, "status": status.value, "failed_from": failed_from},
    )


# --- A–D: создание заказа и один активный заказ --------------------------------------


async def test_create_order(session: AsyncSession) -> None:
    order = await _order(session)

    assert order.id is not None
    assert order.status is S.NEW
    assert order.doc_type is DocType.PRACTICE_REPORT
    assert order.created_at is not None
    assert order.updated_at is not None
    assert order.failed_from_status is None
    assert await svc.get_active_order(session, order.user_id) == order


async def test_user_can_have_many_orders_sequentially(session: AsyncSession) -> None:
    user = await _user(session)
    first = await svc.create_order(session, user_id=user.id)
    first = await _advance(session, first, S.CANCELLED)
    assert await svc.can_create_order(session, user.id)

    second = await svc.create_order(session, user_id=user.id)
    second = await _to_completed(session, second)
    third = await svc.create_order(session, user_id=user.id)

    assert len({first.id, second.id, third.id}) == 3
    await session.refresh(first)
    await session.refresh(second)
    assert first.status is S.CANCELLED
    assert second.status is S.COMPLETED
    assert await svc.get_active_order(session, user.id) == third


async def test_second_active_order_is_rejected(session: AsyncSession) -> None:
    order = await _order(session)

    assert not await svc.can_create_order(session, order.user_id)
    with pytest.raises(ActiveOrderExistsError):
        await svc.create_order(session, user_id=order.user_id)


async def test_second_active_order_rejected_by_database(session: AsyncSession) -> None:
    order = await _order(session)

    with pytest.raises(IntegrityError, match="uq_orders_user_id_active"):
        async with session.begin_nested():
            await _insert_order(session, order.user_id, S.NEW)


@pytest.mark.parametrize("existing", list(S))
async def test_active_order_index_matches_domain(session: AsyncSession, existing: S) -> None:
    """Условие индекса в БД совпадает с доменным определением активного заказа."""
    user = await _user(session)
    await _insert_order(session, user.id, existing)

    if is_active(existing):
        with pytest.raises(IntegrityError, match="uq_orders_user_id_active"):
            async with session.begin_nested():
                await _insert_order(session, user.id, S.NEW)
    else:
        await _insert_order(session, user.id, S.NEW)


async def test_different_users_have_active_orders_simultaneously(session: AsyncSession) -> None:
    orders = [await _order(session, tg_id=tg_id) for tg_id in range(1, 11)]

    assert len({o.user_id for o in orders}) == 10
    for order in orders:
        assert await svc.get_active_order(session, order.user_id) == order


async def test_revision_blocked_while_another_order_is_active(session: AsyncSession) -> None:
    completed = await _to_completed(session, await _order(session))
    await svc.create_order(session, user_id=completed.user_id)

    with pytest.raises(ActiveOrderExistsError):
        await svc.change_status(session, completed.id, S.REVISION_REQUESTED)
    await session.refresh(completed)
    assert completed.status is S.COMPLETED


# --- E, F: переходы статусов ---------------------------------------------------------


async def test_cancel_at_consent_step_does_not_record_consent(session: AsyncSession) -> None:
    order = await _order(session)
    order = await _advance(session, order, S.WAITING_FOR_CONSENT, S.CANCELLED)

    assert order.consent_given_at is None
    assert order.cancelled_at is not None


async def test_full_flow_with_revision(session: AsyncSession) -> None:
    order = await _order(session)
    order = await _advance(session, order, S.WAITING_FOR_CONSENT, S.WAITING_FOR_FILES)
    assert order.consent_given_at is not None

    order = await _advance(session, order, S.ANALYZING_MATERIALS, S.WAITING_FOR_MISSING_DATA)
    order = await _advance(session, order, S.WAITING_FOR_DATA_CONFIRMATION)
    await _confirm_requirements(session, order)
    order = await _advance(session, order, S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION)
    await _confirm_plan(session, order)
    order = await _advance(session, order, S.PAYMENT_PENDING)
    await pay_for_order(session, order.id)
    order = await _advance(session, order, S.COMPLETED)
    assert order.completed_at is not None
    assert await svc.can_create_order(session, order.user_id)

    order = await _advance(session, order, S.REVISION_REQUESTED)
    assert not await svc.can_create_order(session, order.user_id)
    order = await _advance(session, order, S.REGENERATING, S.COMPLETED)
    assert order.status is S.COMPLETED


@pytest.mark.parametrize("target", [S.PAID, S.COMPLETED, S.GENERATING, S.FAILED])
async def test_invalid_transition_is_rejected(session: AsyncSession, target: S) -> None:
    order = await _order(session)

    with pytest.raises(InvalidStatusTransitionError):
        await svc.change_status(session, order.id, target)
    await session.refresh(order)
    assert order.status is S.NEW


# --- G, H, I, J: версии ТЗ и плана ---------------------------------------------------


async def test_requirements_version_create_edit_confirm(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))

    draft = await svc.create_requirements_version(session, order.id, {"topic": "Черновик"})
    assert (draft.version, draft.status, draft.confirmed_at) == (1, VersionStatus.DRAFT, None)
    draft = await svc.update_requirements_draft(session, order.id, draft.id, REQUIREMENTS)
    assert draft.content["topic"] == REQUIREMENTS["topic"]

    confirmed = await svc.confirm_requirements_version(session, order.id, draft.id)
    assert confirmed.status is VersionStatus.CONFIRMED
    assert confirmed.confirmed_at is not None

    with pytest.raises(VersionAlreadyConfirmedError):
        await svc.update_requirements_draft(session, order.id, draft.id, {"topic": "Другая"})

    v2 = await svc.create_requirements_version(session, order.id, {"topic": "Новая тема"})
    with pytest.raises(DraftVersionExistsError):
        await svc.create_requirements_version(session, order.id, {"topic": "Ещё одна"})
    history = await svc.get_requirements_versions(session, order.id)
    assert [(v.version, v.status) for v in history] == [
        (1, VersionStatus.CONFIRMED),
        (2, VersionStatus.DRAFT),
    ]
    assert history[0].content["topic"] == REQUIREMENTS["topic"]
    assert v2.content["topic"] == "Новая тема"


async def test_requirements_content_is_validated(session: AsyncSession) -> None:
    order = await _order(session)

    with pytest.raises(ValueError):
        await svc.create_requirements_version(session, order.id, {"unknown_field": "x"})
    with pytest.raises(ValueError):
        await svc.create_requirements_version(
            session, order.id, {"practice_start": "2026-07-01", "practice_end": "2026-06-01"}
        )


@pytest.mark.parametrize("table", ["order_requirements_versions", "order_plan_versions"])
async def test_confirmed_version_is_immutable_in_database(
    session: AsyncSession, table: str
) -> None:
    order = await _to_data_confirmation(session, await _order(session))
    requirements = await _confirm_requirements(session, order)
    await _advance(session, order, S.PREPARING_PLAN)
    plan = await _confirm_plan(session, order)
    version_id = requirements.id if table == "order_requirements_versions" else plan.id

    for sql in (
        f"UPDATE {table} SET content = '{{}}'::jsonb WHERE id = :id",
        f"UPDATE {table} SET status = 'draft', confirmed_at = NULL WHERE id = :id",
        f"DELETE FROM {table} WHERE id = :id",
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            async with session.begin_nested():
                await session.execute(text(sql), {"id": version_id})


async def test_draft_version_is_editable_in_database(session: AsyncSession) -> None:
    order = await _order(session)
    draft = await svc.create_requirements_version(session, order.id, {"topic": "Черновик"})

    await session.execute(
        text('UPDATE order_requirements_versions SET content = \'{"topic": "X"}\' WHERE id = :id'),
        {"id": draft.id},
    )
    await session.refresh(draft)
    assert draft.content == {"topic": "X"}


async def test_plan_requires_confirmed_requirements(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))

    with pytest.raises(RequirementsNotConfirmedError):
        await svc.create_plan_version(session, order.id, PLAN_V1)
    with pytest.raises(RequirementsNotConfirmedError):
        await svc.change_status(session, order.id, S.PREPARING_PLAN)

    await svc.create_requirements_version(session, order.id, REQUIREMENTS)
    with pytest.raises(RequirementsNotConfirmedError):
        await svc.create_plan_version(session, order.id, PLAN_V1)


async def test_plan_cannot_be_confirmed_when_requirements_changed(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))
    await _confirm_requirements(session, order)
    plan = await svc.create_plan_version(session, order.id, PLAN_V1)

    # Пользователь начал менять ТЗ: v2 ещё не подтверждена.
    requirements_v2 = await svc.create_requirements_version(session, order.id, REQUIREMENTS)
    with pytest.raises(RequirementsNotConfirmedError):
        await svc.confirm_plan_version(session, order.id, plan.id)

    # ТЗ v2 подтверждено, но черновик плана построен на v1.
    await svc.confirm_requirements_version(session, order.id, requirements_v2.id)
    with pytest.raises(OutdatedVersionError):
        await svc.confirm_plan_version(session, order.id, plan.id)

    plan = await svc.update_plan_draft(session, order.id, plan.id, PLAN_V1)
    assert plan.requirements_version_id == requirements_v2.id
    plan = await svc.confirm_plan_version(session, order.id, plan.id)
    assert plan.status is VersionStatus.CONFIRMED


async def test_new_plan_version_keeps_old_one(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))
    requirements = await _confirm_requirements(session, order)
    order = await _advance(session, order, S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION)
    v1 = await _confirm_plan(session, order)
    v1_content, v1_confirmed_at = v1.content, v1.confirmed_at

    # Пользователь попросил изменения — новая версия и новое подтверждение.
    order = await _advance(session, order, S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION)
    v2 = await _confirm_plan(session, order, PLAN_V2)

    history = await svc.get_plan_versions(session, order.id)
    assert [v.version for v in history] == [1, 2]
    old, new = history
    assert old.id == v1.id
    assert old.status is VersionStatus.CONFIRMED
    assert old.content == v1_content
    assert old.confirmed_at == v1_confirmed_at
    assert new.id == v2.id
    assert new.requirements_version_id == requirements.id
    # Порядок разделов сохраняется так, как его задал план.
    assert [s["title"] for s in new.content["sections"]] == [
        "Введение",
        "Анализ отдела продаж",
        "Характеристика предприятия",
        "Заключение",
    ]
    assert old.content["sections"][1]["requirements"] == ["структура", "виды деятельности"]


async def test_versions_are_frozen_after_payment(session: AsyncSession) -> None:
    order = await _to_payment_pending(session, await _order(session))
    await pay_for_order(session, order.id)

    with pytest.raises(OrderNotEditableError):
        await svc.create_requirements_version(session, order.id, REQUIREMENTS)
    with pytest.raises(OrderNotEditableError):
        await svc.create_plan_version(session, order.id, PLAN_V2)


# --- K: оплата -----------------------------------------------------------------------


async def test_payment_pending_requires_confirmed_plan(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))
    await _confirm_requirements(session, order)
    order = await _advance(session, order, S.PREPARING_PLAN, S.WAITING_FOR_PLAN_CONFIRMATION)
    await svc.create_plan_version(session, order.id, PLAN_V1)

    with pytest.raises(PlanNotConfirmedError):
        await svc.change_status(session, order.id, S.PAYMENT_PENDING)


async def test_paid_requires_confirmed_plan(session: AsyncSession) -> None:
    order = await _to_payment_pending(session, await _order(session))
    # Новый черновик плана делает подтверждённую v1 неактуальной.
    await svc.create_plan_version(session, order.id, PLAN_V2)

    with pytest.raises(PlanNotConfirmedError):
        await svc.change_status(session, order.id, S.PAID)
    await session.refresh(order)
    assert order.status is S.PAYMENT_PENDING


async def test_paid_guard_does_not_depend_on_path(session: AsyncSession) -> None:
    """Даже если статус выставлен в обход сервиса, PAID без плана невозможен."""
    order = await _order(session)
    await session.execute(
        text("UPDATE orders SET status = 'payment_pending' WHERE id = :id"), {"id": order.id}
    )

    with pytest.raises(PlanNotConfirmedError):
        await svc.change_status(session, order.id, S.PAID)


async def test_paid_with_confirmed_plan(session: AsyncSession) -> None:
    """OD-4: подтверждённого плана мало — PAID только после списания кредитов."""
    order = await _to_payment_pending(session, await _order(session))

    with pytest.raises(OrderNotPaidError):
        await svc.change_status(session, order.id, S.PAID)
    await session.refresh(order)
    assert order.status is S.PAYMENT_PENDING

    await pay_for_order(session, order.id)
    await session.refresh(order)
    assert order.status is S.GENERATING


# --- L: FAILED -----------------------------------------------------------------------


async def test_failed_before_payment_retries_same_step(session: AsyncSession) -> None:
    order = await _advance(
        session,
        await _order(session),
        S.WAITING_FOR_CONSENT,
        S.WAITING_FOR_FILES,
        S.ANALYZING_MATERIALS,
    )
    order = await svc.change_status(session, order.id, S.FAILED, error="timeout")
    assert order.failed_from_status is S.ANALYZING_MATERIALS
    assert order.error == "timeout"
    assert await svc.get_active_order(session, order.user_id) == order

    # Обход оплаты через FAILED невозможен.
    for target in (S.GENERATING, S.PAID, S.COMPLETED, S.PREPARING_PLAN):
        with pytest.raises(InvalidStatusTransitionError):
            await svc.change_status(session, order.id, target)

    order = await svc.change_status(session, order.id, S.ANALYZING_MATERIALS)
    assert order.failed_from_status is None
    assert order.error is None


async def test_failed_before_payment_can_be_cancelled(session: AsyncSession) -> None:
    order = await _to_data_confirmation(session, await _order(session))
    await _confirm_requirements(session, order)
    order = await _advance(session, order, S.PREPARING_PLAN, S.FAILED, S.CANCELLED)

    assert order.status is S.CANCELLED
    assert order.failed_from_status is None


async def test_failed_generation_retries_but_cannot_cancel(session: AsyncSession) -> None:
    order = await _to_payment_pending(session, await _order(session))
    await pay_for_order(session, order.id)
    order = await _advance(session, order, S.FAILED)
    assert order.failed_from_status is S.GENERATING

    with pytest.raises(InvalidStatusTransitionError):
        await svc.change_status(session, order.id, S.CANCELLED)
    order = await _advance(session, order, S.GENERATING, S.COMPLETED)
    assert order.status is S.COMPLETED


async def test_failed_status_consistency_enforced_by_database(session: AsyncSession) -> None:
    order = await _order(session)

    with pytest.raises(IntegrityError, match="failed_from_status_consistent"):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE orders SET status = 'failed' WHERE id = :id"), {"id": order.id}
            )


# --- M: CANCELLED --------------------------------------------------------------------


async def test_cancelled_is_terminal(session: AsyncSession) -> None:
    order = await _advance(session, await _order(session), S.WAITING_FOR_CONSENT, S.CANCELLED)
    assert order.cancelled_at is not None
    assert await svc.get_active_order(session, order.user_id) is None

    for target in S:
        with pytest.raises(InvalidStatusTransitionError):
            await svc.change_status(session, order.id, target)
    with pytest.raises(OrderNotEditableError):
        await svc.create_requirements_version(session, order.id, REQUIREMENTS)
