"""Telegram Stars в бот-слое: отправка счёта и чтение транзакций бота.

Адаптеры к aiogram для services.payments.telegram_stars: сервисы о Telegram не знают.
"""

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import LabeledPrice, TransactionPartnerUser

from app.services.payments.telegram_stars import STARS_CURRENCY, StarTransactionRecord


async def send_star_invoice(
    bot: Bot, chat_id: int, payload: str, title: str, description: str, amount: int
) -> None:
    """sendInvoice в Stars: provider_token для XTR не нужен, цена — одна позиция."""
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        currency=STARS_CURRENCY,
        prices=[LabeledPrice(label=title, amount=amount)],
    )


async def fetch_star_transactions(bot: Bot, offset: int, limit: int) -> list[StarTransactionRecord]:
    """getStarTransactions → записи в общем виде (порядок ответа сохраняется)."""
    result = await bot.get_star_transactions(offset=offset, limit=limit)
    records = []
    for transaction in result.transactions:
        source = transaction.source
        date = _aware(transaction.date)
        if isinstance(source, TransactionPartnerUser):
            records.append(
                StarTransactionRecord(
                    id=transaction.id,
                    amount=transaction.amount,
                    date=date,
                    transaction_type=source.transaction_type,
                    invoice_payload=source.invoice_payload,
                    payer_tg_id=source.user.id,
                    nanostar_amount=transaction.nanostar_amount,
                )
            )
        else:
            # Исходящие (возвраты, выводы) и прочие входящие — только для позиции.
            records.append(
                StarTransactionRecord(
                    id=transaction.id,
                    amount=transaction.amount,
                    date=date,
                    nanostar_amount=transaction.nanostar_amount,
                )
            )
    return records


def _aware(value: datetime | int) -> datetime:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
