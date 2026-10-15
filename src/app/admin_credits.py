"""Ручная корректировка баланса кредитов оператором (консоль, не Telegram).

    python -m app.admin_credits show --tg-id N
    python -m app.admin_credits adjust --tg-id N --credits M
    ADMIN_CREDITS_ENABLED=1 python -m app.admin_credits adjust --tg-id N --credits M \\
        --confirm CODE

show и adjust без --confirm только читают. adjust без --confirm — предпросмотр:
баланс до и после и код подтверждения. Код привязан к пользователю, сумме, балансу
и последней проводке пользователя и пересчитывается под блокировкой: после любой
проводки (в том числе этой же корректировки) он устаревает, поэтому повтор той же
команды отклоняется. Это защита от случайного повтора, а не идемпотентность: новый
предпросмотр даёт новый код и позволяет сознательно повторить операцию.

ADMIN_CREDITS_ENABLED=1 — предохранитель от случайного выполнения, а не
авторизация: любой, у кого есть доступ к серверу, может его выставить. В
docker-compose.prod.yml переменной нет — её передают только на один запуск
(docker compose run -e ADMIN_CREDITS_ENABLED=1 ...). Настоящая граница доступа —
доступ к серверу и БД. Telegram-хендлера у корректировки нет.

Коды выхода: 0 — успех или предпросмотр, 2 — пользователь не найден, 3 — нет
предохранителя, 4 — код подтверждения не совпал, 5 — сумма недопустима или баланс
стал бы отрицательным, 1 — схема БД не совпадает с миграциями.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.schema import SchemaMismatchError, ensure_schema_is_current
from app.db.session import create_engine, create_sessionmaker
from app.logging_setup import setup_logging
from app.services.billing import (
    MAX_ADMIN_ADJUST_CREDITS,
    RECENT_ADJUSTMENTS_WINDOW,
    AdjustmentRefusal,
    admin_account,
    admin_adjust,
    preview_admin_adjustment,
)

ENABLE_VARIABLE = "ADMIN_CREDITS_ENABLED"

EXIT_OK = 0
EXIT_SCHEMA = 1
EXIT_USER_NOT_FOUND = 2
EXIT_NOT_ENABLED = 3
EXIT_STALE_CODE = 4
EXIT_INVALID = 5

_REFUSAL_EXIT = {
    AdjustmentRefusal.USER_NOT_FOUND: EXIT_USER_NOT_FOUND,
    AdjustmentRefusal.STALE_CODE: EXIT_STALE_CODE,
    AdjustmentRefusal.INVALID_AMOUNT: EXIT_INVALID,
    AdjustmentRefusal.NEGATIVE_BALANCE: EXIT_INVALID,
}
_REFUSAL_TEXT = {
    AdjustmentRefusal.USER_NOT_FOUND: (
        "Пользователь не найден: он должен сначала открыть бота (/start)."
    ),
    AdjustmentRefusal.STALE_CODE: (
        "Код подтверждения не совпал: баланс или журнал изменились после предпросмотра "
        "(или операция уже выполнена). Проверьте счёт командой show и при необходимости "
        "повторите предпросмотр."
    ),
    AdjustmentRefusal.INVALID_AMOUNT: (
        f"Недопустимая сумма: целое, не 0, по модулю не больше {MAX_ADMIN_ADJUST_CREDITS}."
    ),
    AdjustmentRefusal.NEGATIVE_BALANCE: "Отказ: баланс стал бы отрицательным.",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin_credits",
        description="Ручная корректировка баланса кредитов (ADMIN_ADJUST).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="баланс, последние проводки и активный заказ")
    show.add_argument("--tg-id", type=int, required=True)
    adjust = commands.add_parser("adjust", help="предпросмотр или выполнение корректировки")
    adjust.add_argument("--tg-id", type=int, required=True)
    adjust.add_argument(
        "--credits", type=int, required=True, help="сумма; отрицательная — списание"
    )
    adjust.add_argument("--confirm", help="код подтверждения из предпросмотра")
    return parser


async def execute(
    args: argparse.Namespace,
    sessionmaker: async_sessionmaker[AsyncSession],
    environ: Mapping[str, str],
    out: TextIO,
) -> int:
    """Команда по разобранным аргументам; код выхода."""
    if args.command == "show":
        return await _show(sessionmaker, args.tg_id, out)
    if args.confirm is None:
        return await _preview(sessionmaker, args.tg_id, args.credits, out)
    if environ.get(ENABLE_VARIABLE) != "1":
        print(
            f"Отказ: для выполнения нужна переменная окружения {ENABLE_VARIABLE}=1 "
            "(предохранитель от случайного запуска, не авторизация).",
            file=out,
        )
        return EXIT_NOT_ENABLED
    async with sessionmaker() as session:
        outcome = await admin_adjust(session, args.tg_id, args.credits, args.confirm)
    if outcome.refusal is not None:
        print(_REFUSAL_TEXT[outcome.refusal], file=out)
        return _REFUSAL_EXIT[outcome.refusal]
    print(
        f"Выполнено: проводка #{outcome.transaction_id}, пользователь #{outcome.user_id}, "
        f"{args.credits:+d} кредитов, баланс теперь {outcome.balance}.",
        file=out,
    )
    return EXIT_OK


async def _show(sessionmaker: async_sessionmaker[AsyncSession], tg_id: int, out: TextIO) -> int:
    async with sessionmaker() as session:
        account = await admin_account(session, tg_id)
        await session.rollback()  # только чтение
    if account is None:
        print(_REFUSAL_TEXT[AdjustmentRefusal.USER_NOT_FOUND], file=out)
        return EXIT_USER_NOT_FOUND
    print(f"Пользователь #{account.user_id}: баланс {account.balance} кредитов", file=out)
    if account.ledger_balance != account.balance:
        print(f"ВНИМАНИЕ: сумма журнала {account.ledger_balance} ≠ баланс", file=out)
    if account.active_order_id is None:
        print("Активного заказа нет.", file=out)
    else:
        print(
            f"Активный заказ #{account.active_order_id}: {account.active_order_status}, "
            f"цена {account.active_order_price}",
            file=out,
        )
    print("Последние проводки:" if account.last_entries else "Проводок нет.", file=out)
    for entry in account.last_entries:
        order = f" заказ #{entry.order_id}" if entry.order_id is not None else ""
        print(
            f"  #{entry.id} {entry.created_at:%Y-%m-%d %H:%M:%S %Z} {entry.reason} "
            f"{entry.amount:+d}{order}",
            file=out,
        )
    return EXIT_OK


async def _preview(
    sessionmaker: async_sessionmaker[AsyncSession], tg_id: int, credits: int, out: TextIO
) -> int:
    async with sessionmaker() as session:
        preview = await preview_admin_adjustment(session, tg_id, credits)
        await session.rollback()  # только чтение
    if isinstance(preview, AdjustmentRefusal):
        print(_REFUSAL_TEXT[preview], file=out)
        return _REFUSAL_EXIT[preview]
    if preview.refusal is not None:
        print(_REFUSAL_TEXT[preview.refusal], file=out)
        return _REFUSAL_EXIT[preview.refusal]
    print(
        f"Предпросмотр (ничего не изменено): пользователь #{preview.user_id}, "
        f"{credits:+d} кредитов, баланс {preview.balance} → {preview.new_balance}.",
        file=out,
    )
    if preview.recent_adjustments:
        hours = int(RECENT_ADJUSTMENTS_WINDOW.total_seconds() // 3600)
        print(
            f"Для сведения: корректировок этого пользователя за {hours} ч — "
            f"{preview.recent_adjustments}.",
            file=out,
        )
    print(
        f"Выполнить: {ENABLE_VARIABLE}=1 python -m app.admin_credits adjust "
        f"--tg-id {tg_id} --credits {credits} --confirm {preview.confirm_code}",
        file=out,
    )
    return EXIT_OK


async def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine(settings)
    try:
        # Как у бота: на несовпадающей схеме ничего не делаем.
        try:
            await ensure_schema_is_current(engine)
        except SchemaMismatchError as exc:
            print(f"Отказ: {exc}", file=out or sys.stdout)
            return EXIT_SCHEMA
        return await execute(
            args,
            create_sessionmaker(engine),
            os.environ if environ is None else environ,
            out or sys.stdout,
        )
    finally:
        await engine.dispose()


def run() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run()
