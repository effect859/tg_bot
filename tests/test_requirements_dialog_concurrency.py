"""Гонки уточнения и подтверждения ТЗ между независимыми сессиями PostgreSQL.

Каждая сессия — своё соединение и настоящие транзакции (committed_sessionmaker),
как у двух апдейтов, которые aiogram обрабатывает параллельно. Третья сессия
держит блокировку заказа, пока обе операции не встанут в очередь: так они
гарантированно конкурируют, а не выполняются одна после другой случайно.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enums import OrderStatus, VersionStatus
from app.domain.errors import InvalidStatusTransitionError
from app.domain.requirements_dialog import content_digest
from app.services import orders
from app.services.requirements_dialog import (
    AnswerOutcomeKind,
    confirm_requirements,
    start_edit,
    submit_answer,
)
from conftest import COMMITTED_TG_ID_BASE
from dialog_samples import AFTER_ANALYSIS, FULL, fresh_order, order_with_draft, versions

S = OrderStatus
K = AnswerOutcomeKind
Sessionmaker = async_sessionmaker[AsyncSession]
Operation = Callable[[AsyncSession], Awaitable[Any]]


VERSIONS = "order_requirements_versions"
IMMUTABLE_TRIGGER = f"trg_{VERSIONS}_immutable_confirmed"


@pytest.fixture
async def committed(committed_sessionmaker: Sessionmaker) -> AsyncIterator[Sessionmaker]:
    """committed_sessionmaker + удаление подтверждённых версий ТЗ тестовых заказов.

    Подтверждённую версию БД удалить не даёт (триггер неизменности), и каскадное
    удаление заказов в общей очистке упало бы. Триггер отключается только внутри
    транзакции очистки: DDL в PostgreSQL транзакционный.
    """
    yield committed_sessionmaker
    async with committed_sessionmaker() as session:
        await session.execute(text(f"ALTER TABLE {VERSIONS} DISABLE TRIGGER {IMMUTABLE_TRIGGER}"))
        await session.execute(
            text(
                f"DELETE FROM {VERSIONS} WHERE order_id IN (SELECT o.id FROM orders o "
                "JOIN users u ON u.id = o.user_id WHERE u.tg_id >= :base)"
            ),
            {"base": COMMITTED_TG_ID_BASE},
        )
        await session.execute(text(f"ALTER TABLE {VERSIONS} ENABLE TRIGGER {IMMUTABLE_TRIGGER}"))
        await session.commit()


async def _run(sessionmaker: Sessionmaker, operation: Operation) -> Any:
    """Операция в своей транзакции: commit при успехе; исключение — результат."""
    async with sessionmaker() as session:
        try:
            result = await operation(session)
        except Exception as exc:
            await session.rollback()
            return exc
        await session.commit()
        return result


async def _race(sessionmaker: Sessionmaker, order_id: int, *operations: Operation) -> list[Any]:
    """Запускает операции, пока заказ заблокирован другой транзакцией, затем отпускает."""
    async with sessionmaker() as holder:
        await orders.lock_order(holder, order_id)
        tasks = [asyncio.create_task(_run(sessionmaker, op)) for op in operations]
        await asyncio.sleep(0.3)  # все операции дошли до блокировки и ждут
        assert not any(task.done() for task in tasks)
        await holder.commit()
    return await asyncio.gather(*tasks)


async def _state(sessionmaker: Sessionmaker, order_id: int) -> tuple[S, list[Any]]:
    async with sessionmaker() as session:
        order = await fresh_order(session, order_id)
        return order.status, [(v.status, v.content) for v in await versions(session, order_id)]


async def _setup(sessionmaker: Sessionmaker, content: dict[str, Any], status: S, n: int) -> int:
    async with sessionmaker() as session:
        return await order_with_draft(
            session, content, status=status, tg_id=COMMITTED_TG_ID_BASE + n
        )


async def test_two_simultaneous_answers_fill_two_questions(
    committed: Sessionmaker,
) -> None:
    order_id = await _setup(committed, AFTER_ANALYSIS, S.WAITING_FOR_MISSING_DATA, 1)

    first, second = await _race(
        committed,
        order_id,
        lambda s: submit_answer(s, order_id, "НИУ ВШЭ"),
        lambda s: submit_answer(s, order_id, "Иванов Иван"),
    )

    # По очереди: первый — на вопрос о вузе, второй — на следующий (ФИО).
    assert {first.kind, second.kind} == {K.ACCEPTED, K.COMPLETED}
    assert {first.field, second.field} == {"university", "student_name"}
    status, [(version_status, content)] = await _state(committed, order_id)
    assert status is S.WAITING_FOR_DATA_CONFIRMATION
    assert version_status is VersionStatus.DRAFT
    assert {content["university"], content["student_name"]} == {"НИУ ВШЭ", "Иванов Иван"}
    assert {k: v for k, v in content.items() if k not in ("university", "student_name")} == {
        k: v for k, v in AFTER_ANALYSIS.items() if k not in ("university", "student_name")
    }


async def test_two_simultaneous_confirms_confirm_once(
    committed: Sessionmaker,
) -> None:
    order_id = await _setup(committed, FULL, S.WAITING_FOR_DATA_CONFIRMATION, 2)
    digest = content_digest(FULL)

    results = await _race(
        committed,
        order_id,
        lambda s: confirm_requirements(s, order_id, digest),
        lambda s: confirm_requirements(s, order_id, digest),
    )

    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], InvalidStatusTransitionError)
    status, found = await _state(committed, order_id)
    assert status is S.PREPARING_PLAN
    assert found == [(VersionStatus.CONFIRMED, FULL)]


async def test_answer_and_cancel(committed: Sessionmaker) -> None:
    order_id = await _setup(committed, AFTER_ANALYSIS, S.WAITING_FOR_MISSING_DATA, 3)

    answer, cancel = await _race(
        committed,
        order_id,
        lambda s: submit_answer(s, order_id, "НИУ ВШЭ"),
        lambda s: orders.change_status(s, order_id, S.CANCELLED),
    )

    assert not isinstance(answer, Exception) and not isinstance(cancel, Exception)
    status, [(_, content)] = await _state(committed, order_id)
    assert status is S.CANCELLED
    if answer.kind is K.ACCEPTED:  # ответ успел до отмены
        assert content == {**AFTER_ANALYSIS, "university": "НИУ ВШЭ"}
    else:  # отмена первой: ответ ничего не записал
        assert answer.kind is K.NOT_EXPECTED
        assert content == AFTER_ANALYSIS


async def test_edit_and_confirm(committed: Sessionmaker) -> None:
    order_id = await _setup(committed, FULL, S.WAITING_FOR_DATA_CONFIRMATION, 4)

    edit, confirm = await _race(
        committed,
        order_id,
        lambda s: start_edit(s, order_id, "topic"),
        lambda s: confirm_requirements(s, order_id, content_digest(FULL)),
    )

    status, [(version_status, content)] = await _state(committed, order_id)
    if isinstance(edit, Exception):  # подтверждение первым
        assert isinstance(edit, InvalidStatusTransitionError)
        assert not isinstance(confirm, Exception)
        assert (status, version_status, content) == (
            S.PREPARING_PLAN,
            VersionStatus.CONFIRMED,
            FULL,
        )
    else:  # изменение первым: подтверждать нечего
        assert isinstance(confirm, InvalidStatusTransitionError)
        assert (status, version_status) == (S.WAITING_FOR_MISSING_DATA, VersionStatus.DRAFT)
        assert content == {**FULL, "topic": None}


async def test_answer_and_double_submit_of_last_answer(
    committed: Sessionmaker,
) -> None:
    """Два одинаковых последних ответа: второй не меняет уже собранное ТЗ."""
    content = {**FULL, "student_name": None}
    order_id = await _setup(committed, content, S.WAITING_FOR_MISSING_DATA, 5)

    results = await _race(
        committed,
        order_id,
        lambda s: submit_answer(s, order_id, "Иванов Иван"),
        lambda s: submit_answer(s, order_id, "Иванов Иван"),
    )

    assert sorted(r.kind for r in results) == sorted([K.COMPLETED, K.NOT_EXPECTED])
    status, [(_, stored)] = await _state(committed, order_id)
    assert status is S.WAITING_FOR_DATA_CONFIRMATION
    assert stored == {**FULL, "student_name": "Иванов Иван"}
