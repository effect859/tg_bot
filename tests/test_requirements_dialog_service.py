"""Уточнение и подтверждение ТЗ на настоящей БД: один черновик v1, переходы, отказы."""

from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderRequirementsVersion
from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import (
    InvalidStatusTransitionError,
    MaterialsNotAcceptedError,
    RequirementFieldNotEditableError,
    RequirementsChangedError,
    RequirementsVolumeConsentError,
    VersionAlreadyConfirmedError,
    VersionNotFoundError,
)
from app.domain.requirements_dialog import AnswerError, content_digest
from app.services import orders
from app.services import requirements_dialog as dialog_service
from app.services.order_materials import ensure_can_add_material
from app.services.requirements_dialog import (
    AnswerOutcomeKind,
    confirm_requirements,
    get_dialog,
    start_edit,
    submit_answer,
)
from dialog_samples import AFTER_ANALYSIS, EMPTY, FULL, fresh_order, order_with_draft, versions

S = OrderStatus
K = AnswerOutcomeKind


# --- Ответы ------------------------------------------------------------------------


async def test_first_question_after_analysis(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    dialog = await get_dialog(session, order_id)

    assert dialog.status is S.WAITING_FOR_MISSING_DATA
    assert dialog.field == "university"


async def test_answer_is_written_into_the_same_draft(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)
    [before] = await versions(session, order_id)

    outcome = await submit_answer(session, order_id, "  НИУ   ВШЭ ")

    assert outcome.kind is K.ACCEPTED
    assert (outcome.field, outcome.value) == ("university", "НИУ ВШЭ")
    assert outcome.dialog is not None and outcome.dialog.field == "student_name"
    [after] = await versions(session, order_id)
    assert (after.id, after.version, after.status) == (before.id, 1, VersionStatus.DRAFT)
    assert after.content == {**AFTER_ANALYSIS, "university": "НИУ ВШЭ"}
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_MISSING_DATA


async def test_rejected_answer_changes_nothing(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    outcome = await submit_answer(session, order_id, "12345")

    assert outcome.kind is K.REJECTED
    assert (outcome.field, outcome.error) == ("university", AnswerError.NO_LETTERS)
    [draft] = await versions(session, order_id)
    assert draft.content == AFTER_ANALYSIS
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_MISSING_DATA


async def test_last_answer_goes_to_confirmation_keeping_optional_fields(
    session: AsyncSession,
) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    first = await submit_answer(session, order_id, "НИУ ВШЭ")
    last = await submit_answer(session, order_id, "Иванов Иван Иванович")

    assert (first.kind, last.kind) == (K.ACCEPTED, K.COMPLETED)
    assert last.dialog is not None and last.dialog.field is None
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [draft] = await versions(session, order_id)
    assert draft.content == FULL
    # Найденное анализом сохранилось.
    assert draft.content["specialty"] == "Бизнес-информатика"
    assert draft.content["target_pages"] == 25
    assert draft.content["extra_requirements"] == ["Оформление по ГОСТ 7.32"]
    assert draft.content["supervisor"] is None


async def test_full_dialog_from_empty_draft(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, EMPTY)
    answers = [
        ("topic", "Анализ продаж", "Анализ продаж"),
        ("practice_type", "учебная", "учебная"),
        ("practice_place", "ООО «Ёлка»", "ООО «Ёлка»"),
        ("practice_start", "1.7.2026", "2026-07-01"),
        ("practice_end", "28.07.2026", "2026-07-28"),
        ("university", "НИУ ВШЭ", "НИУ ВШЭ"),
        ("student_name", "Иванов Иван", "Иванов Иван"),
    ]

    for field, text, stored in answers:
        assert (await get_dialog(session, order_id)).field == field
        outcome = await submit_answer(session, order_id, text)
        assert (outcome.field, outcome.value) == (field, stored)

    assert outcome.kind is K.COMPLETED
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    assert len(await versions(session, order_id)) == 1


async def test_conflicting_dates_are_asked_again(session: AsyncSession) -> None:
    content = {**AFTER_ANALYSIS, "practice_start": "2026-07-10", "practice_end": None}
    order_id = await order_with_draft(session, content)

    outcome = await submit_answer(session, order_id, "01.07.2026")

    assert outcome.kind is K.DATES_RESET
    assert outcome.dialog is not None and outcome.dialog.field == "practice_start"
    [draft] = await versions(session, order_id)
    assert (draft.content["practice_start"], draft.content["practice_end"]) == (None, None)
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_MISSING_DATA


@pytest.mark.parametrize("status", [S.WAITING_FOR_DATA_CONFIRMATION, S.PREPARING_PLAN, S.CANCELLED])
async def test_answer_in_other_status_is_not_expected(session: AsyncSession, status: S) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    if status is S.PREPARING_PLAN:
        await confirm_requirements(session, order_id, content_digest(FULL))
    elif status is S.CANCELLED:
        await orders.change_status(session, order_id, S.CANCELLED)
    before = [v.content for v in await versions(session, order_id)]

    outcome = await submit_answer(session, order_id, "Новая тема")

    assert outcome.kind is K.NOT_EXPECTED
    assert [v.content for v in await versions(session, order_id)] == before
    assert (await fresh_order(session, order_id)).status is status


async def test_missing_draft_is_reported(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, EMPTY, with_draft=False)

    with pytest.raises(VersionNotFoundError):
        await submit_answer(session, order_id, "Тема")
    with pytest.raises(VersionNotFoundError):
        await get_dialog(session, order_id)


async def test_complete_draft_waiting_for_data_moves_to_confirmation(
    session: AsyncSession,
) -> None:
    """Несогласованное состояние (всё заполнено, но ждём данных): текст не пишется."""
    order_id = await order_with_draft(session, FULL)

    outcome = await submit_answer(session, order_id, "лишний текст")

    assert outcome.kind is K.COMPLETED and outcome.field is None
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [draft] = await versions(session, order_id)
    assert draft.content == FULL


# --- Подтверждение -----------------------------------------------------------------


async def test_confirm_makes_v1_confirmed_and_moves_to_preparing_plan(
    session: AsyncSession,
) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    version = await confirm_requirements(session, order_id, content_digest(FULL))

    assert (version.version, version.status) == (1, VersionStatus.CONFIRMED)
    assert version.confirmed_at is not None
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN
    [only] = await versions(session, order_id)
    assert only.content == FULL


async def test_second_confirm_is_refused(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    await confirm_requirements(session, order_id, content_digest(FULL))

    with pytest.raises(InvalidStatusTransitionError):
        await confirm_requirements(session, order_id, content_digest(FULL))

    assert [v.status for v in await versions(session, order_id)] == [VersionStatus.CONFIRMED]


async def test_confirm_of_changed_requirements_is_refused(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    old_digest = content_digest({**FULL, "topic": "Старая тема"})

    with pytest.raises(RequirementsChangedError):
        await confirm_requirements(session, order_id, old_digest)

    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    assert [v.status for v in await versions(session, order_id)] == [VersionStatus.DRAFT]


async def test_confirm_while_waiting_for_data_is_refused(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    with pytest.raises(InvalidStatusTransitionError):
        await confirm_requirements(session, order_id, content_digest(AFTER_ANALYSIS))


async def test_confirmed_version_is_immutable(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    version = await confirm_requirements(session, order_id, content_digest(FULL))

    with pytest.raises(VersionAlreadyConfirmedError):
        await orders.update_requirements_draft(session, order_id, version.id, EMPTY)
    with pytest.raises(VersionNotFoundError):
        await get_dialog(session, order_id)  # черновика больше нет

    assert await orders.has_confirmed_requirements(session, order_id)
    with pytest.raises(MaterialsNotAcceptedError):
        await ensure_can_add_material(
            session, order_id, filename="late.pdf", mime_type="application/pdf", size_bytes=10
        )


# --- Изменение ---------------------------------------------------------------------


async def test_edit_clears_field_and_asks_it_again(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    dialog = await start_edit(session, order_id, "practice_place")

    assert dialog.field == "practice_place"
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_MISSING_DATA
    [draft] = await versions(session, order_id)
    assert draft.content == {**FULL, "practice_place": None}
    assert draft.status is VersionStatus.DRAFT

    outcome = await submit_answer(session, order_id, "АО «Сосна»")

    assert outcome.kind is K.COMPLETED
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [draft] = await versions(session, order_id)
    assert draft.content == {**FULL, "practice_place": "АО «Сосна»"}


async def test_edit_of_dates_clears_both(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    dialog = await start_edit(session, order_id, "practice_start")

    assert dialog.field == "practice_start"
    assert (dialog.content["practice_start"], dialog.content["practice_end"]) == (None, None)


@pytest.mark.parametrize("field", ["specialty", "supervisor", "target_pages", "bogus", ""])
async def test_edit_of_optional_field_is_refused(session: AsyncSession, field: str) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    with pytest.raises(RequirementFieldNotEditableError):
        await start_edit(session, order_id, field)


async def test_edit_outside_confirmation_is_refused(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, AFTER_ANALYSIS)

    with pytest.raises(InvalidStatusTransitionError):
        await start_edit(session, order_id, "topic")

    [draft] = await versions(session, order_id)
    assert draft.content == AFTER_ANALYSIS


# --- Неподдерживаемый объём: согласие продолжить без него (Stage 3.10) ---------------

UNSUPPORTED = {**FULL, "target_pages": None, "unsupported_target_pages": 150}
# Черновик, сохранённый до поля unsupported_target_pages: объём в target_pages.
LEGACY = {**FULL, "target_pages": 150}
LEGACY_WITHOUT_KEY = {k: v for k, v in LEGACY.items() if k != "unsupported_target_pages"}


async def _legacy_order(session: AsyncSession, content: dict[str, Any]) -> int:
    """Заказ на подтверждении ТЗ с черновиком ровно в виде ``content`` (как до Stage 3.10)."""
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)
    [draft] = await versions(session, order_id)
    await session.execute(
        update(OrderRequirementsVersion)
        .where(OrderRequirementsVersion.id == draft.id)
        .values(content=content)
    )
    await session.commit()
    return order_id


async def _assert_untouched(session: AsyncSession, order_id: int, content: dict[str, Any]) -> None:
    assert (await fresh_order(session, order_id)).status is S.WAITING_FOR_DATA_CONFIRMATION
    [draft] = await versions(session, order_id)
    assert draft.status is VersionStatus.DRAFT
    assert draft.content == content
    assert content_digest(draft.content) == content_digest(content)


async def test_consent_confirms_requirements_keeping_unsupported_volume(
    session: AsyncSession,
) -> None:
    order_id = await order_with_draft(session, UNSUPPORTED, status=S.WAITING_FOR_DATA_CONFIRMATION)

    version = await confirm_requirements(
        session, order_id, content_digest(UNSUPPORTED), without_volume=True
    )

    assert version.status is VersionStatus.CONFIRMED
    assert version.content == UNSUPPORTED  # подтверждено ровно то, что видел пользователь
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN


@pytest.mark.parametrize("content", [UNSUPPORTED, LEGACY, LEGACY_WITHOUT_KEY])
async def test_plain_confirm_of_unsupported_volume_is_refused(
    session: AsyncSession, content: dict[str, Any]
) -> None:
    order_id = await _legacy_order(session, content)

    with pytest.raises(RequirementsVolumeConsentError):
        await confirm_requirements(session, order_id, content_digest(content))
    await session.rollback()

    await _assert_untouched(session, order_id, content)


async def test_consent_without_unsupported_volume_is_refused(session: AsyncSession) -> None:
    order_id = await order_with_draft(session, FULL, status=S.WAITING_FOR_DATA_CONFIRMATION)

    with pytest.raises(RequirementsVolumeConsentError):
        await confirm_requirements(session, order_id, content_digest(FULL), without_volume=True)
    await session.rollback()

    await _assert_untouched(session, order_id, FULL)


@pytest.mark.parametrize("content", [LEGACY, LEGACY_WITHOUT_KEY])
async def test_consent_moves_legacy_volume_and_confirms(
    session: AsyncSession, content: dict[str, Any]
) -> None:
    order_id = await _legacy_order(session, content)

    version = await confirm_requirements(
        session, order_id, content_digest(content), without_volume=True
    )
    await session.commit()

    assert version.content == UNSUPPORTED
    [confirmed] = await versions(session, order_id)
    assert (confirmed.status, confirmed.content) == (VersionStatus.CONFIRMED, UNSUPPORTED)
    assert (await fresh_order(session, order_id)).status is S.PREPARING_PLAN


async def test_consent_under_stale_digest_moves_nothing(session: AsyncSession) -> None:
    """Отпечаток сверяется с тем, что видел пользователь, — до переноса объёма."""
    order_id = await _legacy_order(session, LEGACY)

    for stale in (content_digest(UNSUPPORTED), content_digest({**LEGACY, "topic": "Другая"})):
        with pytest.raises(RequirementsChangedError):
            await confirm_requirements(session, order_id, stale, without_volume=True)
        await session.rollback()

    await _assert_untouched(session, order_id, LEGACY)


@pytest.mark.parametrize("failing", ["confirm_requirements_version", "change_status"])
async def test_consent_failure_after_transfer_leaves_legacy_draft_intact(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """Перенос и подтверждение — одна транзакция: сбой после переноса откатывает и его."""
    order_id = await _legacy_order(session, LEGACY)
    original = getattr(dialog_service, failing)
    moved: list[Any] = []

    async def fail(session_: AsyncSession, order_id_: int, *args: Any, **kwargs: Any) -> Any:
        [draft] = await versions(session_, order_id_)
        moved.append(dict(draft.content))  # перенос уже записан в этой транзакции
        if failing == "change_status":
            await original(session_, order_id_, *args, **kwargs)
        raise OperationalError("UPDATE ...", {}, Exception("db down"))

    if failing == "change_status":
        # Подтверждение версии проходит, падает смена статуса.
        monkeypatch.setattr(dialog_service, "change_status", fail)
    else:
        monkeypatch.setattr(dialog_service, "confirm_requirements_version", fail)

    with pytest.raises(OperationalError):
        await confirm_requirements(session, order_id, content_digest(LEGACY), without_volume=True)
    await session.rollback()

    assert moved == [UNSUPPORTED]
    await _assert_untouched(session, order_id, LEGACY)
