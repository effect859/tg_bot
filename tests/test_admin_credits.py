"""Ручная корректировка баланса (Stage 3.12): сервис admin_adjust и консоль app.admin_credits.

БД — настоящая PostgreSQL: обычные тесты — в SAVEPOINT-фикстуре (conftest), гонка с
оплатой заказа — между независимыми сессиями с настоящими commit.
"""

import argparse
import asyncio
import io
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import test_report_completion_concurrency as completion_races
from app import admin_credits as cli
from app.db.models import BalanceTransaction, Order, User
from app.domain.billing import order_price
from app.domain.enums import DocType, OrderStatus, TransactionReason
from app.services.billing import (
    MAX_ADMIN_ADJUST_CREDITS,
    AdjustmentPreview,
    AdjustmentRefusal,
    ChargeOutcomeKind,
    admin_adjust,
    ledger_balance,
    pay_order,
    preview_admin_adjustment,
)
from app.services.users import register_or_update_user
from billing_samples import grant_credits
from conftest import COMMITTED_TG_ID_BASE
from report_samples import payment_pending_order

Sessionmaker = async_sessionmaker[AsyncSession]
TG_ID = 777_000_001
ENABLED = {cli.ENABLE_VARIABLE: "1"}
SRC = Path(__file__).resolve().parents[1] / "src" / "app"

# Гонки с настоящими commit: удаляет заказы и их версии (журнал — committed_sessionmaker).
sessions = completion_races.sessions


async def _user(session: AsyncSession, balance: int = 0, tg_id: int = TG_ID) -> int:
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name="Оператор", language_code="ru"
    )
    user_id = user.id
    if balance:
        await grant_credits(session, user_id, balance)
    await session.commit()
    return user_id


async def _ledger(session: AsyncSession, user_id: int) -> list[tuple[TransactionReason, int]]:
    rows = await session.execute(
        select(BalanceTransaction.reason, BalanceTransaction.amount)
        .where(BalanceTransaction.user_id == user_id)
        .order_by(BalanceTransaction.id)
    )
    return [(reason, amount) for reason, amount in rows]


async def _balance(session: AsyncSession, user_id: int) -> int:
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user.balance


async def _preview(session: AsyncSession, credits: int, tg_id: int = TG_ID) -> AdjustmentPreview:
    preview = await preview_admin_adjustment(session, tg_id, credits)
    await session.rollback()
    assert isinstance(preview, AdjustmentPreview)
    return preview


async def _run(
    sessionmaker: Sessionmaker, argv: list[str], environ: dict[str, str] | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    args = cli.build_parser().parse_args(argv)
    code = await cli.execute(args, sessionmaker, environ or {}, out)
    return code, out.getvalue()


def _code(output: str) -> str:
    match = re.search(r"--confirm ([0-9a-f]{12})", output)
    assert match is not None, output
    return match.group(1)


# --- Сервис -----------------------------------------------------------------------------


async def test_adjust_writes_ledger_and_balance_together(session: AsyncSession) -> None:
    user_id = await _user(session, balance=100)
    preview = await _preview(session, 500)

    outcome = await admin_adjust(session, TG_ID, 500, preview.confirm_code)

    assert outcome.refusal is None
    assert (outcome.user_id, outcome.balance) == (user_id, 600)
    assert await _balance(session, user_id) == 600
    assert await _ledger(session, user_id) == [
        (TransactionReason.BONUS, 100),
        (TransactionReason.ADMIN_ADJUST, 500),
    ]
    assert await ledger_balance(session, user_id) == 600
    row = await session.get(BalanceTransaction, outcome.transaction_id)
    assert row is not None and (row.order_id, row.payment_id) == (None, None)


async def test_negative_adjustment_is_debit(session: AsyncSession) -> None:
    user_id = await _user(session, balance=500)
    preview = await _preview(session, -200)

    outcome = await admin_adjust(session, TG_ID, -200, preview.confirm_code)

    assert (outcome.refusal, outcome.balance) == (None, 300)
    assert await ledger_balance(session, user_id) == 300


async def test_negative_final_balance_refused(session: AsyncSession) -> None:
    user_id = await _user(session, balance=50)
    preview = await _preview(session, -100)
    assert preview.refusal is AdjustmentRefusal.NEGATIVE_BALANCE

    outcome = await admin_adjust(session, TG_ID, -100, preview.confirm_code)

    assert outcome.refusal is AdjustmentRefusal.NEGATIVE_BALANCE
    assert await _balance(session, user_id) == 50
    assert await _ledger(session, user_id) == [(TransactionReason.BONUS, 50)]


@pytest.mark.parametrize(
    "credits", [0, MAX_ADMIN_ADJUST_CREDITS + 1, -MAX_ADMIN_ADJUST_CREDITS - 1, True]
)
async def test_invalid_amount_refused(session: AsyncSession, credits: int) -> None:
    user_id = await _user(session, balance=50_000)
    preview = await preview_admin_adjustment(session, TG_ID, credits)
    await session.rollback()
    assert isinstance(preview, AdjustmentPreview)
    assert preview.refusal is AdjustmentRefusal.INVALID_AMOUNT

    outcome = await admin_adjust(session, TG_ID, credits, preview.confirm_code)

    assert outcome.refusal is AdjustmentRefusal.INVALID_AMOUNT
    assert await _ledger(session, user_id) == [(TransactionReason.BONUS, 50_000)]


async def test_limit_is_inclusive(session: AsyncSession) -> None:
    await _user(session)
    preview = await _preview(session, MAX_ADMIN_ADJUST_CREDITS)

    outcome = await admin_adjust(session, TG_ID, MAX_ADMIN_ADJUST_CREDITS, preview.confirm_code)

    assert outcome.refusal is None


async def test_unknown_user_refused_and_not_created(session: AsyncSession) -> None:
    assert await preview_admin_adjustment(session, TG_ID, 100) is AdjustmentRefusal.USER_NOT_FOUND

    outcome = await admin_adjust(session, TG_ID, 100, "0" * 12)

    assert outcome.refusal is AdjustmentRefusal.USER_NOT_FOUND
    assert await session.scalar(select(User).where(User.tg_id == TG_ID)) is None


async def test_repeat_with_same_code_refused(session: AsyncSession) -> None:
    """Повтор той же команды после успеха: код устарел (новая проводка) — отказ."""
    user_id = await _user(session)
    preview = await _preview(session, 500)
    assert (await admin_adjust(session, TG_ID, 500, preview.confirm_code)).refusal is None

    again = await admin_adjust(session, TG_ID, 500, preview.confirm_code)

    assert again.refusal is AdjustmentRefusal.STALE_CODE
    assert await _ledger(session, user_id) == [(TransactionReason.ADMIN_ADJUST, 500)]
    assert await _balance(session, user_id) == 500


async def test_code_stale_after_any_balance_change(session: AsyncSession) -> None:
    user_id = await _user(session)
    preview = await _preview(session, 500)
    await grant_credits(session, user_id, 10)  # например, пополнение после предпросмотра
    await session.commit()

    outcome = await admin_adjust(session, TG_ID, 500, preview.confirm_code)

    assert outcome.refusal is AdjustmentRefusal.STALE_CODE
    assert await _balance(session, user_id) == 10


async def test_code_is_bound_to_amount_and_user(session: AsyncSession) -> None:
    await _user(session)
    await _user(session, tg_id=TG_ID + 1)
    preview = await _preview(session, 500)

    other_amount = await admin_adjust(session, TG_ID, 501, preview.confirm_code)
    other_user = await admin_adjust(session, TG_ID + 1, 500, preview.confirm_code)

    assert other_amount.refusal is AdjustmentRefusal.STALE_CODE
    assert other_user.refusal is AdjustmentRefusal.STALE_CODE


async def test_new_preview_allows_deliberate_repeat(session: AsyncSession) -> None:
    """Защита — от случайного повтора, не идемпотентность: новый предпросмотр — новый код."""
    user_id = await _user(session)
    first = await _preview(session, 500)
    await admin_adjust(session, TG_ID, 500, first.confirm_code)
    second = await _preview(session, 500)

    outcome = await admin_adjust(session, TG_ID, 500, second.confirm_code)

    assert second.confirm_code != first.confirm_code
    assert second.recent_adjustments == 1
    assert outcome.refusal is None
    assert await ledger_balance(session, user_id) == 1000


async def test_preview_changes_nothing(session: AsyncSession) -> None:
    user_id = await _user(session, balance=100)

    preview = await _preview(session, 500)

    assert (preview.user_id, preview.balance, preview.new_balance) == (user_id, 100, 600)
    assert (preview.refusal, preview.recent_adjustments) == (None, 0)
    assert await _ledger(session, user_id) == [(TransactionReason.BONUS, 100)]


async def test_failure_before_commit_leaves_nothing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проводка и баланс — одна транзакция: сбой до commit не оставляет ни того, ни другого."""
    user_id = await _user(session, balance=100)
    preview = await _preview(session, 500)

    async def broken_commit() -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(session, "commit", broken_commit)
    with pytest.raises(RuntimeError):
        await admin_adjust(session, TG_ID, 500, preview.confirm_code)
    await session.rollback()
    monkeypatch.undo()

    assert await _balance(session, user_id) == 100
    assert await _ledger(session, user_id) == [(TransactionReason.BONUS, 100)]


async def test_log_has_ids_and_amount_but_no_tg_id(
    session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = await _user(session)
    preview = await _preview(session, 500)

    with caplog.at_level("INFO", logger="app.services.billing"):
        outcome = await admin_adjust(session, TG_ID, 500, preview.confirm_code)

    [record] = [r for r in caplog.records if "Admin balance adjustment" in r.getMessage()]
    message = record.getMessage()
    assert f"user_id={user_id} amount=500 transaction_id={outcome.transaction_id}" in message
    assert str(TG_ID) not in caplog.text


# --- Консоль ----------------------------------------------------------------------------


async def test_cli_preview_then_confirm(session: AsyncSession, sessionmaker: Sessionmaker) -> None:
    user_id = await _user(session, balance=100)

    code, preview = await _run(sessionmaker, ["adjust", "--tg-id", str(TG_ID), "--credits", "500"])

    assert code == cli.EXIT_OK
    assert "Предпросмотр (ничего не изменено)" in preview
    assert "баланс 100 → 600" in preview
    assert await _ledger(session, user_id) == [(TransactionReason.BONUS, 100)]

    confirm = ["adjust", "--tg-id", str(TG_ID), "--credits", "500", "--confirm", _code(preview)]
    code, done = await _run(sessionmaker, confirm, ENABLED)

    assert code == cli.EXIT_OK
    assert "баланс теперь 600" in done
    assert await _balance(session, user_id) == 600

    code, again = await _run(sessionmaker, confirm, ENABLED)

    assert code == cli.EXIT_STALE_CODE
    assert "Код подтверждения не совпал" in again
    assert await _balance(session, user_id) == 600


@pytest.mark.parametrize("environ", [{}, {cli.ENABLE_VARIABLE: "0"}, {cli.ENABLE_VARIABLE: "yes"}])
async def test_cli_confirm_requires_safety_variable(
    session: AsyncSession, sessionmaker: Sessionmaker, environ: dict[str, str]
) -> None:
    user_id = await _user(session)
    preview = await _preview(session, 500)
    argv = ["adjust", "--tg-id", str(TG_ID), "--credits", "500", "--confirm", preview.confirm_code]

    code, output = await _run(sessionmaker, argv, environ)

    assert code == cli.EXIT_NOT_ENABLED
    assert "не авторизация" in output
    assert await _ledger(session, user_id) == []


async def test_cli_preview_works_without_safety_variable(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    await _user(session)

    code, _ = await _run(sessionmaker, ["adjust", "--tg-id", str(TG_ID), "--credits", "5"])

    assert code == cli.EXIT_OK


@pytest.mark.parametrize(
    ("credits", "balance", "expected"),
    [
        ("0", 0, cli.EXIT_INVALID),
        ("-100", 50, cli.EXIT_INVALID),
        ("100", None, cli.EXIT_USER_NOT_FOUND),
    ],
    ids=["zero", "negative-balance", "unknown-user"],
)
async def test_cli_refusals(
    session: AsyncSession,
    sessionmaker: Sessionmaker,
    credits: str,
    balance: int | None,
    expected: int,
) -> None:
    if balance is not None:
        await _user(session, balance=balance)
    argv = ["adjust", "--tg-id", str(TG_ID), "--credits", credits]

    preview_code, _ = await _run(sessionmaker, argv)
    confirm_code, _ = await _run(sessionmaker, [*argv, "--confirm", "0" * 12], ENABLED)

    assert (preview_code, confirm_code) == (expected, expected)


async def test_cli_show_is_read_only(session: AsyncSession, sessionmaker: Sessionmaker) -> None:
    user_id = await _user(session, balance=100)
    before = await session.scalar(select(func.count()).select_from(BalanceTransaction))

    code, output = await _run(sessionmaker, ["show", "--tg-id", str(TG_ID)])

    assert code == cli.EXIT_OK
    assert f"Пользователь #{user_id}: баланс 100 кредитов" in output
    assert "Активного заказа нет." in output
    assert "bonus +100" in output
    assert await session.scalar(select(func.count()).select_from(BalanceTransaction)) == before


async def test_cli_show_unknown_user(sessionmaker: Sessionmaker) -> None:
    code, output = await _run(sessionmaker, ["show", "--tg-id", str(TG_ID)])

    assert code == cli.EXIT_USER_NOT_FOUND
    assert "/start" in output


@pytest.mark.parametrize(
    "argv",
    [
        ["adjust", "--tg-id", "1", "--credits", "5", "--force"],
        ["adjust", "--tg-id", "1", "--credits", "5", "--reason", "тест"],
        ["adjust", "--tg-id", "1", "--credits", "1.5"],
        ["grant", "--tg-id", "1"],
    ],
    ids=["no-force", "no-reason", "integer-only", "unknown-command"],
)
def test_cli_rejects_unsupported_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(argv)

    assert caught.value.code == 2


def test_no_telegram_access_to_adjustment() -> None:
    """Корректировка — только из консоли: Telegram-слой её не импортирует."""
    for path in (SRC / "bot").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "admin_adjust" not in source, path
        assert "admin_credits" not in source, path


def test_safety_variable_not_in_production_compose() -> None:
    compose = (SRC.parents[1] / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert cli.ENABLE_VARIABLE not in compose


def test_parser_namespace() -> None:
    args = cli.build_parser().parse_args(["adjust", "--tg-id", "5", "--credits", "-7"])

    assert args == argparse.Namespace(command="adjust", tg_id=5, credits=-7, confirm=None)


# --- Гонка с оплатой заказа (настоящие commit) -------------------------------------------


async def test_concurrent_adjustment_and_order_payment(sessions: Sessionmaker) -> None:
    """Одна блокировка пользователя: без взаимной блокировки, баланс = журнал, не < 0."""
    price = order_price(DocType.PRACTICE_REPORT)
    tg_id = COMMITTED_TG_ID_BASE + 900
    async with sessions() as session:
        order_id = await payment_pending_order(session, tg_id=tg_id)
        user_id = await session.scalar(select(Order.user_id).where(Order.id == order_id))
        assert user_id is not None
        await grant_credits(session, user_id, price)
        await session.commit()
    async with sessions() as session:
        preview = await _preview(session, -300, tg_id=tg_id)
    barrier = asyncio.Barrier(2)

    async def pay() -> ChargeOutcomeKind:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await pay_order(session, user_id, order_id)).kind

    async def adjust() -> AdjustmentRefusal | None:
        async with sessions() as session:
            await asyncio.wait_for(barrier.wait(), 10)
            return (await admin_adjust(session, tg_id, -300, preview.confirm_code)).refusal

    paid, refusal = await asyncio.wait_for(asyncio.gather(pay(), adjust()), 30)

    # Кто первым взял блокировку пользователя, тот и прошёл; второй получил отказ:
    # после оплаты баланс 0, и списание 300 отклоняется (проверка суммы — раньше кода).
    assert (paid, refusal) in {
        (ChargeOutcomeKind.PAID, AdjustmentRefusal.NEGATIVE_BALANCE),
        (ChargeOutcomeKind.INSUFFICIENT, None),
    }
    async with sessions() as session:
        balance = await _balance(session, user_id)
        assert balance == await ledger_balance(session, user_id)
        assert balance >= 0
        order = await session.get(Order, order_id)
        assert order is not None
        paid_ok = paid is ChargeOutcomeKind.PAID
        assert order.status is (OrderStatus.GENERATING if paid_ok else OrderStatus.PAYMENT_PENDING)
