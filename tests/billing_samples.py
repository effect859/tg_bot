"""Кредиты в тестах: начисление через журнал и оплата заказа настоящим pay_order.

PAID достижим только через списание (OD-4), поэтому тесты, которым нужен оплаченный
заказ, проходят тот же путь, что и бот: кредиты на балансе → pay_order.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BalanceTransaction, Order, User
from app.domain.enums import TransactionReason
from app.services.billing import ChargeOutcomeKind, pay_order


async def grant_credits(session: AsyncSession, user_id: int, credits: int) -> None:
    """Бонус ``credits`` кредитов: запись в журнал и баланс — как в billing, вместе."""
    user = await session.scalar(
        select(User).where(User.id == user_id).execution_options(populate_existing=True)
    )
    assert user is not None
    session.add(BalanceTransaction(user_id=user_id, amount=credits, reason=TransactionReason.BONUS))
    user.balance += credits
    await session.flush()


async def pay_for_order(session: AsyncSession, order_id: int) -> None:
    """Заказ в PAYMENT_PENDING → кредиты на цену → pay_order → GENERATING (зафиксировано)."""
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None and order.price is not None
    user_id = order.user_id
    await grant_credits(session, user_id, order.price)
    outcome = await pay_order(session, user_id, order_id)
    assert outcome.kind is ChargeOutcomeKind.PAID, outcome
