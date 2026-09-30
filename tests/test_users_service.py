from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, User
from app.domain.enums import DocType, OrderStatus
from app.services.users import get_user_orders, register_or_update_user


async def _register(session: AsyncSession, tg_id: int = 1001, **overrides: str | None) -> User:
    fields: dict[str, str | None] = {
        "username": "ivan",
        "first_name": "Иван",
        "language_code": "ru",
    }
    fields.update(overrides)
    return await register_or_update_user(session, tg_id=tg_id, **fields)


async def test_registers_new_user(session: AsyncSession) -> None:
    user = await _register(session)

    assert user.id is not None
    assert user.tg_id == 1001
    assert user.first_name == "Иван"
    assert user.balance == 0
    assert user.is_blocked is False


async def test_updates_existing_user_without_duplicate(session: AsyncSession) -> None:
    first = await _register(session, username="ivan")
    second = await _register(session, username="ivan_new", first_name="Ваня")

    assert second.id == first.id
    assert second.username == "ivan_new"
    assert second.first_name == "Ваня"
    count = await session.scalar(select(func.count()).select_from(User))
    assert count == 1


async def test_get_user_orders_returns_own_orders_newest_first(session: AsyncSession) -> None:
    user = await _register(session, tg_id=1)
    other = await _register(session, tg_id=2)
    # Активным может быть только один заказ пользователя, остальные — завершённые.
    statuses = [OrderStatus.CANCELLED, OrderStatus.COMPLETED, OrderStatus.NEW]
    orders = [
        Order(user_id=user.id, doc_type=DocType.PRACTICE_REPORT, status=status)
        for status in statuses
    ]
    session.add_all([*orders, Order(user_id=other.id, doc_type=DocType.PRACTICE_REPORT)])
    await session.flush()

    result = await get_user_orders(session, user.id, limit=2)

    # created_at одинаков в пределах транзакции, порядок определяет id.
    assert [o.id for o in result] == [orders[2].id, orders[1].id]
