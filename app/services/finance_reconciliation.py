from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import LedgerType
from app.models import Balance, LedgerEntry, PlatformLedgerEntry, TraderLedgerEntry, User


TWOPLACES = Decimal('0.01')


def money(value) -> Decimal:
    return Decimal(value or '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    actual_available: Decimal
    actual_frozen: Decimal
    expected_available: Decimal
    expected_frozen: Decimal

    @property
    def available_delta(self) -> Decimal:
        return money(self.actual_available - self.expected_available)

    @property
    def frozen_delta(self) -> Decimal:
        return money(self.actual_frozen - self.expected_frozen)


async def reconcile_merchant_balance(db: AsyncSession, merchant_id: UUID, currency: str = 'RUB') -> ReconciliationResult:
    balance = (await db.execute(
        select(Balance).where(Balance.merchant_id == merchant_id, Balance.currency == currency)
    )).scalar_one_or_none()
    entries = (await db.execute(
        select(LedgerEntry)
        .where(LedgerEntry.merchant_id == merchant_id, LedgerEntry.currency == currency)
        .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
    )).scalars().all()
    expected_available = Decimal('0.00')
    expected_frozen = Decimal('0.00')
    for entry in entries:
        amount = money(entry.amount)
        if entry.entry_type == LedgerType.credit.value:
            expected_available += amount
        elif entry.entry_type == LedgerType.hold.value:
            expected_available -= amount
            expected_frozen += amount
        elif entry.entry_type == LedgerType.release.value:
            expected_available += amount
            expected_frozen -= amount
        elif entry.entry_type == LedgerType.debit.value:
            if expected_frozen >= amount:
                expected_frozen -= amount
            else:
                expected_available -= amount
        elif entry.entry_type == LedgerType.fee.value:
            continue
    actual_available = money(balance.available if balance else Decimal('0.00'))
    actual_frozen = money(balance.frozen if balance else Decimal('0.00'))
    expected_available = money(expected_available)
    expected_frozen = money(expected_frozen)
    return ReconciliationResult(
        ok=actual_available == expected_available and actual_frozen == expected_frozen,
        actual_available=actual_available,
        actual_frozen=actual_frozen,
        expected_available=expected_available,
        expected_frozen=expected_frozen,
    )


async def reconcile_trader_balance(db: AsyncSession, trader_id: UUID) -> ReconciliationResult:
    trader = (await db.execute(select(User).where(User.id == trader_id))).scalar_one_or_none()
    latest = (await db.execute(
        select(TraderLedgerEntry)
        .where(TraderLedgerEntry.trader_id == trader_id)
        .order_by(TraderLedgerEntry.created_at.desc(), TraderLedgerEntry.id.desc())
        .limit(1)
    )).scalar_one_or_none()
    actual_available = money((trader.trader_balance if trader else Decimal('0.00')) - (trader.trader_hold if trader else Decimal('0.00')))
    actual_frozen = money(trader.trader_hold if trader else Decimal('0.00'))
    if latest:
        expected_available = money(latest.balance_after - latest.hold_after)
        expected_frozen = money(latest.hold_after)
    else:
        expected_available = Decimal('0.00')
        expected_frozen = Decimal('0.00')
    return ReconciliationResult(
        ok=actual_available == expected_available and actual_frozen == expected_frozen,
        actual_available=actual_available,
        actual_frozen=actual_frozen,
        expected_available=expected_available,
        expected_frozen=expected_frozen,
    )


async def platform_income_total(db: AsyncSession, merchant_id: UUID | None = None) -> Decimal:
    query = select(PlatformLedgerEntry).where(PlatformLedgerEntry.entry_type == 'platform_income')
    if merchant_id is not None:
        query = query.where(PlatformLedgerEntry.merchant_id == merchant_id)
    rows = (await db.execute(query)).scalars().all()
    return money(sum((money(row.amount) for row in rows), Decimal('0.00')))
