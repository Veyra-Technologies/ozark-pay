"""Currency-safe trader wallets and immutable foreign operation totals."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models import (
    Deposit,
    Requisite,
    TraderOperationSnapshot,
    TraderWallet,
    TraderWalletLedgerEntry,
    User,
)


USDT_QUANTUM = Decimal('0.00000001')
LOCAL_QUANTUM = Decimal('0.01')


class TraderFinanceError(ValueError):
    pass


def _decimal(value: object, quantum: Decimal = USDT_QUANTUM) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TraderFinanceError('invalid monetary amount') from exc
    if not result.is_finite():
        raise TraderFinanceError('invalid monetary amount')
    return result


async def get_usdt_wallet(db, trader_id: UUID, *, for_update: bool = False) -> TraderWallet | None:
    statement = select(TraderWallet).where(
        TraderWallet.trader_id == trader_id,
        TraderWallet.currency == 'USDT',
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one_or_none()


async def ensure_usdt_wallet(db, trader_id: UUID) -> TraderWallet:
    wallet = await get_usdt_wallet(db, trader_id, for_update=True)
    if wallet:
        return wallet
    wallet = TraderWallet(
        trader_id=trader_id,
        currency='USDT',
        balance=Decimal('0'),
        hold=Decimal('0'),
    )
    db.add(wallet)
    await db.flush()
    return wallet


async def manual_usdt_wallet_adjustment(
    db,
    trader: User,
    *,
    target_balance: Decimal,
    target_hold: Decimal,
    operation_id: UUID | None,
    idempotency_prefix: str,
    reason: str,
) -> list[TraderWalletLedgerEntry]:
    if not reason.strip():
        raise TraderFinanceError('manual wallet adjustment reason is required')
    target_balance = _decimal(target_balance)
    target_hold = _decimal(target_hold)
    if target_balance < 0 or target_hold < 0 or target_hold > target_balance:
        raise TraderFinanceError('USDT reserve must be between zero and balance')
    wallet = await ensure_usdt_wallet(db, trader.id)
    previous_balance = _decimal(wallet.balance)
    previous_hold = _decimal(wallet.hold)
    keys = {
        f'{idempotency_prefix}:balance',
        f'{idempotency_prefix}:hold',
    }
    existing_entries = (await db.execute(
        select(TraderWalletLedgerEntry).where(
            TraderWalletLedgerEntry.idempotency_key.in_(keys)
        )
    )).scalars().all()
    if existing_entries:
        matches_target = (
            previous_balance == target_balance
            and previous_hold == target_hold
            and all(
                _decimal(entry.balance_after) == target_balance
                and _decimal(entry.hold_after) == target_hold
                for entry in existing_entries
            )
        )
        if not matches_target:
            raise TraderFinanceError('idempotency key already belongs to another wallet adjustment')
        return existing_entries
    wallet.balance = target_balance
    wallet.hold = target_hold
    entries: list[TraderWalletLedgerEntry] = []
    for suffix, entry_type, amount in (
        ('balance', 'balance_adjustment', target_balance - previous_balance),
        ('hold', 'reserve_set', target_hold - previous_hold),
    ):
        if amount == 0:
            continue
        key = f'{idempotency_prefix}:{suffix}'
        entry = TraderWalletLedgerEntry(
            wallet_id=wallet.id,
            trader_id=trader.id,
            operation_id=operation_id,
            entry_type=entry_type,
            amount=amount,
            balance_after=target_balance,
            hold_after=target_hold,
            currency='USDT',
            idempotency_key=key,
            description=reason.strip(),
        )
        db.add(entry)
        entries.append(entry)
    return entries


def wallet_view(account: User | None, wallet: TraderWallet | None, quote: dict | None = None) -> dict:
    if not account:
        return {'country': None, 'local_currency': None, 'accounting_currency': None}
    country = account.working_country
    local_currency = account.working_currency
    usdt_balance = _decimal(wallet.balance if wallet else 0)
    usdt_hold = _decimal(wallet.hold if wallet else 0)
    result = {
        'country': country,
        'local_currency': local_currency,
        'accounting_currency': 'USDT' if country in {'AM', 'GE'} else 'RUB',
        'usdt_balance': usdt_balance,
        'usdt_hold': usdt_hold,
        'usdt_available': usdt_balance - usdt_hold,
        'rub_balance': _decimal(account.trader_balance or 0, LOCAL_QUANTUM),
        'rub_hold': _decimal(account.trader_hold or 0, LOCAL_QUANTUM),
        'rub_available': _decimal(account.trader_balance or 0, LOCAL_QUANTUM) - _decimal(account.trader_hold or 0, LOCAL_QUANTUM),
        'rate': None,
        'local_balance': None,
        'local_hold': None,
        'local_available': None,
    }
    if country in {'AM', 'GE'} and quote and quote.get('available') and quote.get('rate'):
        rate = _decimal(quote['rate'], Decimal('0.000000000001'))
        result.update(
            rate=rate,
            local_balance=(usdt_balance * rate).quantize(LOCAL_QUANTUM, rounding=ROUND_HALF_UP),
            local_hold=(usdt_hold * rate).quantize(LOCAL_QUANTUM, rounding=ROUND_HALF_UP),
            local_available=((usdt_balance - usdt_hold) * rate).quantize(LOCAL_QUANTUM, rounding=ROUND_HALF_UP),
        )
    return result


async def capture_foreign_operation_snapshot(
    db,
    *,
    deposit: Deposit,
    requisite: Requisite,
    trader: User,
    settlement_usdt: Decimal,
    profit_local: Decimal,
    profit_usdt: Decimal,
    rate_local_per_usdt: Decimal,
    provider_timestamp: datetime,
    fetched_at: datetime,
) -> TraderOperationSnapshot:
    existing = (await db.execute(select(TraderOperationSnapshot).where(
        TraderOperationSnapshot.deposit_id == deposit.id
    ))).scalar_one_or_none()
    if existing:
        return existing
    if trader.working_country not in {'AM', 'GE'} or trader.working_currency not in {'AMD', 'GEL'}:
        raise TraderFinanceError('foreign operation snapshot requires AM or GE profile')
    if requisite.trader_id != trader.id or requisite.country != trader.working_country or requisite.currency != trader.working_currency:
        raise TraderFinanceError('requisite does not belong to the assigned jurisdiction')
    if deposit.requisites_id != requisite.id or deposit.currency != trader.working_currency:
        raise TraderFinanceError('deposit currency or owner mismatch')
    rate = _decimal(rate_local_per_usdt, Decimal('0.000000000001'))
    if rate <= 0:
        raise TraderFinanceError('positive fixed XE rate is required')
    snapshot = TraderOperationSnapshot(
        deposit_id=deposit.id,
        trader_id=trader.id,
        local_currency=deposit.currency,
        local_amount=_decimal(deposit.amount),
        settlement_usdt=_decimal(settlement_usdt),
        profit_local=_decimal(profit_local),
        profit_usdt=_decimal(profit_usdt),
        rate_local_per_usdt=rate,
        rate_source='xe',
        provider_timestamp=provider_timestamp,
        fetched_at=fetched_at,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


def foreign_statistics(
    successful_deposits: list[Deposit],
    snapshots_by_deposit: dict[UUID, TraderOperationSnapshot],
    *,
    local_currency: str,
) -> dict:
    local_turnover = sum((Decimal(row.amount) for row in successful_deposits), Decimal('0'))
    count = len(successful_deposits)
    snapshots = [snapshots_by_deposit.get(row.id) for row in successful_deposits]
    complete = all(
        snapshot is not None
        and snapshot.local_currency == local_currency
        and _decimal(snapshot.local_amount) == _decimal(row.amount)
        for row, snapshot in zip(successful_deposits, snapshots)
    )
    return {
        'local_currency': local_currency,
        'turnover_local': local_turnover,
        'average_local': local_turnover / count if count else Decimal('0'),
        'history_complete': complete,
        'turnover_usdt': sum((row.settlement_usdt for row in snapshots if row), Decimal('0')) if complete else None,
        'profit_local': sum((row.profit_local for row in snapshots if row), Decimal('0')) if complete else None,
        'profit_usdt': sum((row.profit_usdt for row in snapshots if row), Decimal('0')) if complete else None,
        'average_usdt': (sum((row.settlement_usdt for row in snapshots if row), Decimal('0')) / count) if complete and count else (Decimal('0') if complete else None),
    }


async def require_working_account(db, account_id: UUID) -> User:
    account = await db.get(User, account_id) if account_id else None
    if not account or account.role not in ('trader', 'operator'):
        raise HTTPException(409, 'Трейдер не назначен')
    if account.role == 'operator' and account.trader_account_id and account.trader_account_id != account.id:
        account = await db.get(User, account.trader_account_id)
    if not account or not account.is_active or account.is_locked or account.is_archived:
        raise HTTPException(409, 'Кабинет трейдера недоступен')
    if not account.working_country or not account.working_currency:
        raise HTTPException(409, 'Страну работы должен назначить Admin или Superadmin')
    return account
