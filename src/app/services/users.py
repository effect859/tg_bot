from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, User


async def register_or_update_user(
    session: AsyncSession,
    *,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
) -> User:
    """Атомарный upsert по tg_id: безопасен при параллельных апдейтах одного пользователя."""
    stmt = (
        insert(User)
        .values(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            language_code=language_code,
        )
        .on_conflict_do_update(
            index_elements=[User.tg_id],
            set_={
                "username": username,
                "first_name": first_name,
                "language_code": language_code,
                "last_seen_at": func.now(),
                "updated_at": func.now(),
            },
        )
        .returning(User)
    )
    result = await session.scalars(stmt, execution_options={"populate_existing": True})
    return result.one()


async def get_user_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))
