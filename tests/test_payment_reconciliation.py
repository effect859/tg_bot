"""Сверка платежей Stars: кредиты зачисляются, даже если successful_payment потерян.

getStarTransactions подменён списком транзакций (FakeTransactions): записываются
запросы (offset, limit), чтобы проверить, когда API вызывается и с какого места.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import BalanceTransaction, Payment
from app.domain.billing import CREDIT_PACKAGES, INVOICE_TTL
from app.domain.enums import PaymentStatus
from app.services import payment_reconciliation as service
from app.services.billing import (
    TopupOutcomeKind,
    confirm_topup,
    create_topup,
    ledger_balance,
    topup_payload,
)
from app.services.payment_reconciliation import (
    PAGE_SIZE,
    RECONCILIATION_MIN_AGE,
    RECONCILIATION_WINDOW,
    Cursor,
    PaymentReconciliation,
    find_payments_for_reconciliation,
    reconcile_star_payments,
)
from app.services.payments.telegram_stars import (
    StarTransactionRecord,
    notification_from_transaction,
)
from app.services.users import register_or_update_user

Sessionmaker = async_sessionmaker[AsyncSession]
TG_ID = 777_555_222
PACKAGE = CREDIT_PACKAGES[0]


class FakeTransactions:
    """getStarTransactions: срез списка по offset/limit; запросы записываются."""

    def __init__(self, records: list[StarTransactionRecord] | None = None) -> None:
        self.records = records or []
        self.calls: list[tuple[int, int]] = []
        self.error: Exception | None = None

    async def __call__(self, offset: int, limit: int) -> list[StarTransactionRecord]:
        self.calls.append((offset, limit))
        if self.error is not None:
            raise self.error
        return self.records[offset : offset + limit]


async def _invoice(session: AsyncSession, tg_id: int = TG_ID) -> tuple[int, int, datetime]:
    """(user_id, payment_id, created_at) нового PENDING-счёта."""
    user = await register_or_update_user(
        session, tg_id=tg_id, username=None, first_name=None, language_code=None
    )
    user_id = user.id
    payment = await create_topup(session, user_id, PACKAGE)
    return user_id, payment.id, payment.created_at


def _paid(payment_id: int, date: datetime, **kwargs: object) -> StarTransactionRecord:
    values: dict[str, object] = {
        "id": f"charge-{payment_id}",
        "amount": PACKAGE.stars,
        "date": date,
        "transaction_type": "invoice_payment",
        "invoice_payload": topup_payload(payment_id),
        "payer_tg_id": TG_ID,
    }
    values.update(kwargs)
    return StarTransactionRecord(**values)  # type: ignore[arg-type]


def _noise(index: int, date: datetime) -> StarTransactionRecord:
    """Чужая транзакция (другой бот-платёж или вывод) — только для позиции."""
    return StarTransactionRecord(id=f"other-{index}", amount=1, date=date)


async def _payment(session: AsyncSession, payment_id: int) -> Payment:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert payment is not None
    return payment


async def _topups(session: AsyncSession, user_id: int) -> int:
    rows = await session.scalars(
        select(BalanceTransaction).where(BalanceTransaction.user_id == user_id)
    )
    return len(list(rows))


async def test_no_candidates_no_api_calls(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    fetch = FakeTransactions()

    result = await reconcile_star_payments(sessionmaker, fetch)

    assert (result.outcomes, fetch.calls) == ([], [])


async def test_lost_successful_payment_is_credited(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    user_id, payment_id, created = await _invoice(session)
    now = created + RECONCILIATION_MIN_AGE + timedelta(seconds=1)
    fetch = FakeTransactions([_noise(0, created), _paid(payment_id, created)])

    result = await reconcile_star_payments(sessionmaker, fetch, now=now)

    assert [o.kind for o in result.outcomes] == [TopupOutcomeKind.CREDITED]
    payment = await _payment(session, payment_id)
    assert (payment.status, payment.provider_payment_id) == (
        PaymentStatus.SUCCEEDED,
        f"charge-{payment_id}",
    )
    assert payment.raw is not None and payment.raw["source"] == "reconciliation"
    assert await ledger_balance(session, user_id) == PACKAGE.credits
    # Повторный проход: счёт уже не кандидат, API не вызывается.
    fetch.calls.clear()
    assert (await reconcile_star_payments(sessionmaker, fetch, now=now)).outcomes == []
    assert fetch.calls == []


async def test_reconciliation_after_handler_does_not_double_credit(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    """Апдейт обработан, а сверка нашла ту же транзакцию: зачисление одно."""
    user_id, payment_id, created = await _invoice(session)
    record = _paid(payment_id, created)
    notification = notification_from_transaction(record)
    assert notification is not None
    await confirm_topup(session, notification)
    # Даже если счёт выглядел бы кандидатом — confirm_topup идемпотентен.
    async with sessionmaker() as other:
        again = await confirm_topup(other, notification)

    assert again.kind is TopupOutcomeKind.ALREADY_CREDITED
    assert await _topups(session, user_id) == 1
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_fresh_and_old_invoices_are_not_candidates(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    _, _, created = await _invoice(session)
    fetch = FakeTransactions()

    too_fresh = created + RECONCILIATION_MIN_AGE - timedelta(seconds=1)
    too_old = created + RECONCILIATION_WINDOW + timedelta(seconds=1)
    for now in (too_fresh, too_old):
        assert await find_payments_for_reconciliation(session, now=now) == set()
        await reconcile_star_payments(sessionmaker, fetch, now=now)

    assert fetch.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"amount": PACKAGE.stars + 1},
        {"payer_tg_id": TG_ID + 1},
    ],
)
async def test_mismatching_transaction_is_rejected(
    session: AsyncSession, sessionmaker: Sessionmaker, kwargs: dict[str, object]
) -> None:
    user_id, payment_id, created = await _invoice(session)
    fetch = FakeTransactions([_paid(payment_id, created, **kwargs)])

    result = await reconcile_star_payments(
        sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE
    )

    assert [o.kind for o in result.outcomes] == [TopupOutcomeKind.REJECTED]
    assert (await _payment(session, payment_id)).status is PaymentStatus.PENDING
    assert await ledger_balance(session, user_id) == 0


async def test_non_invoice_and_foreign_transactions_ignored(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    user_id, payment_id, created = await _invoice(session)
    fetch = FakeTransactions(
        [
            _paid(payment_id, created, transaction_type="paid_media_payment"),
            _paid(payment_id, created, invoice_payload="topup:999999999"),
            _paid(payment_id, created, nanostar_amount=10),
        ]
    )

    result = await reconcile_star_payments(
        sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE
    )

    assert result.outcomes == []
    assert await ledger_balance(session, user_id) == 0


async def test_scans_pages_until_found(session: AsyncSession, sessionmaker: Sessionmaker) -> None:
    user_id, payment_id, created = await _invoice(session)
    records = [_noise(i, created) for i in range(PAGE_SIZE * 2 + 30)]
    records.append(_paid(payment_id, created))
    records.extend(_noise(1000 + i, created) for i in range(PAGE_SIZE))
    fetch = FakeTransactions(records)

    result = await reconcile_star_payments(
        sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE
    )

    assert [o.kind for o in result.outcomes] == [TopupOutcomeKind.CREDITED]
    # Остановка, как только кандидатов не осталось.
    assert fetch.calls == [(0, PAGE_SIZE), (PAGE_SIZE, PAGE_SIZE), (PAGE_SIZE * 2, PAGE_SIZE)]
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_page_limit_is_logged(
    session: AsyncSession,
    sessionmaker: Sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, created = await _invoice(session)
    monkeypatch.setattr(service, "MAX_PAGES", 2)
    fetch = FakeTransactions([_noise(i, created) for i in range(PAGE_SIZE * 5)])

    result = await reconcile_star_payments(
        sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE
    )

    assert (result.outcomes, result.pages) == ([], 2)
    assert "page limit reached: pages=2 unmatched=1" in caplog.text


async def test_cursor_skips_transactions_older_than_window(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    user_id, payment_id, created = await _invoice(session)
    now = created + RECONCILIATION_MIN_AGE
    old = now - RECONCILIATION_WINDOW - timedelta(days=1)
    history = [_noise(i, old + timedelta(seconds=i)) for i in range(150)]
    fetch = FakeTransactions(history + [_noise(500, created)])
    reconciliation = PaymentReconciliation(sessionmaker, fetch)

    first = await reconcile_star_payments(sessionmaker, fetch, now=now)
    assert first.cursor == Cursor(150, "other-149")

    # Оплата пришла позже: следующий проход начинает с курсора (проверив границу).
    fetch.records.append(_paid(payment_id, created))
    fetch.calls.clear()
    reconciliation.cursor = first.cursor
    second = await reconcile_star_payments(sessionmaker, fetch, cursor=first.cursor, now=now)

    assert [o.kind for o in second.outcomes] == [TopupOutcomeKind.CREDITED]
    assert fetch.calls == [(149, 1), (150, PAGE_SIZE)]
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_cursor_reset_when_boundary_moved(
    session: AsyncSession, sessionmaker: Sessionmaker, caplog: pytest.LogCaptureFixture
) -> None:
    """Порядок не хронологический (вставка в начало): курсор сбрасывается в 0."""
    user_id, payment_id, created = await _invoice(session)
    now = created + RECONCILIATION_MIN_AGE
    fetch = FakeTransactions([_paid(payment_id, created), _noise(1, created)])

    result = await reconcile_star_payments(
        sessionmaker, fetch, cursor=Cursor(1, "something-else"), now=now
    )

    assert [o.kind for o in result.outcomes] == [TopupOutcomeKind.CREDITED]
    assert fetch.calls[0] == (0, 1)
    assert fetch.calls[1] == (0, PAGE_SIZE)
    assert "cursor reset" in caplog.text
    assert await ledger_balance(session, user_id) == PACKAGE.credits


async def test_cursor_stops_at_non_monotonic_dates(
    session: AsyncSession, sessionmaker: Sessionmaker
) -> None:
    _, _, created = await _invoice(session)
    now = created + RECONCILIATION_MIN_AGE
    old = now - RECONCILIATION_WINDOW - timedelta(days=1)
    # Даты убывают: на хронологию полагаться нельзя — курсор не сдвигается дальше первой.
    fetch = FakeTransactions([_noise(0, old), _noise(1, old - timedelta(hours=1))])

    result = await reconcile_star_payments(sessionmaker, fetch, now=now)

    assert result.cursor == Cursor(1, "other-0")


async def test_recovery_pass_survives_api_error(
    session: AsyncSession,
    sessionmaker: Sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _invoice(session)
    # Проход берёт «сейчас»: счёт из этого теста становится кандидатом сразу.
    monkeypatch.setattr(service, "RECONCILIATION_MIN_AGE", timedelta(seconds=-60))
    fetch = FakeTransactions()
    fetch.error = RuntimeError("Telegram said: secret")
    recovery = PaymentReconciliation(sessionmaker, fetch)

    assert await recovery.run_pass() == []

    assert fetch.calls == [(0, PAGE_SIZE)]
    assert recovery.cursor == Cursor()
    assert "pass failed: error=RuntimeError" in caplog.text
    assert "secret" not in caplog.text


async def test_confirm_failure_is_logged_and_others_continue(
    session: AsyncSession,
    sessionmaker: Sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_a, payment_a, created = await _invoice(session)
    user_b, payment_b, _ = await _invoice(session, tg_id=TG_ID + 1)
    real_confirm = service.confirm_topup

    async def confirm(session: AsyncSession, notification: object) -> object:
        if notification.invoice_payload == topup_payload(payment_a):  # type: ignore[attr-defined]
            raise RuntimeError("db secret")
        return await real_confirm(session, notification)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "confirm_topup", confirm)
    fetch = FakeTransactions(
        [_paid(payment_a, created), _paid(payment_b, created, payer_tg_id=TG_ID + 1)]
    )

    result = await reconcile_star_payments(
        sessionmaker, fetch, now=created + RECONCILIATION_MIN_AGE
    )

    assert [o.kind for o in result.outcomes] == [TopupOutcomeKind.CREDITED]
    assert f"payload=topup:{payment_a} error=RuntimeError" in caplog.text
    assert "db secret" not in caplog.text
    assert await ledger_balance(session, user_a) == 0
    assert await ledger_balance(session, user_b) == PACKAGE.credits


async def test_recovery_runs_first_pass_at_start(sessionmaker: Sessionmaker) -> None:
    passes: list[list[object]] = []

    class Recording(PaymentReconciliation):
        async def run_pass(self) -> list[object]:  # type: ignore[override]
            outcomes = await super().run_pass()
            passes.append(list(outcomes))
            return outcomes

    recovery = Recording(sessionmaker, FakeTransactions(), interval_seconds=3600)
    recovery.start()
    try:
        for _ in range(250):
            if passes:
                break
            await asyncio.sleep(0.02)
    finally:
        await recovery.stop()

    assert passes == [[]]
    assert not recovery.running


def test_reconciliation_constants() -> None:
    assert service.RECONCILIATION_INTERVAL_SECONDS == 300.0
    assert timedelta(minutes=2) == RECONCILIATION_MIN_AGE
    # Окно шире TTL счёта: оплату у самой границы TTL сверка тоже найдёт.
    assert RECONCILIATION_WINDOW > INVOICE_TTL
    assert PAGE_SIZE == 100  # максимум getStarTransactions
    assert service.MAX_PAGES >= 1
    assert logging.getLogger(service.__name__).name == "app.services.payment_reconciliation"
