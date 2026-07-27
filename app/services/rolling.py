from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    MerchantSettlementMode,
    RollingAccountStatus,
    RollingAllocationStatus,
    RollingLedgerType,
)
from app.models import (
    Deposit,
    MerchantFinanceProfile,
    MerchantRollingAccount,
    MerchantRollingAllocation,
    MerchantRollingLedgerEntry,
    OperationFeeSnapshot,
)
from app.services.ledger import LedgerError, credit, debit_available
from app.services.audit import audit
from app.services.rapira import (
    RollingRapiraQuote,
    RollingRateUnavailable,
    get_strict_rolling_ask_quote,
)


logger = logging.getLogger('app.finance')
MONEY_QUANT = Decimal('0.01')
USDT_QUANT = Decimal('0.000001')
RATE_QUANT = Decimal('0.00000001')
PERCENT_QUANT = Decimal('0.000001')


class RollingError(ValueError):
    code = 'rolling_error'


class RollingModeConflict(RollingError):
    code = 'rolling_mode_conflict'


class RollingAccountSuspended(RollingError):
    code = 'rolling_suspended'


def money(value) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else '0')).quantize(
            MONEY_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid RUB amount') from exc
    if not result.is_finite():
        raise RollingError('invalid RUB amount')
    return result


def usdt(value) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else '0')).quantize(
            USDT_QUANT,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid USDT amount') from exc
    if not result.is_finite():
        raise RollingError('invalid USDT amount')
    return result


def rolling_rate(value) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid Rapira rate') from exc
    if not result.is_finite() or result <= 0:
        raise RollingError('invalid Rapira rate')
    return result


def percentage(value) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollingError('invalid percentage') from exc
    if not result.is_finite() or result < 0 or result > 100:
        raise RollingError('invalid percentage')
    return result


async def get_finance_profile(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    lock: bool = False,
) -> MerchantFinanceProfile | None:
    query = select(MerchantFinanceProfile).where(
        MerchantFinanceProfile.merchant_id == merchant_id
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_finance_profile(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    actor_id: UUID | None = None,
    lock: bool = False,
) -> MerchantFinanceProfile:
    profile = await get_finance_profile(db, merchant_id, lock=lock)
    if profile:
        return profile
    profile = MerchantFinanceProfile(
        merchant_id=merchant_id,
        settlement_mode=MerchantSettlementMode.postpaid.value,
        created_by=actor_id,
        updated_by=actor_id,
    )
    db.add(profile)
    await db.flush()
    return profile


async def merchant_settlement_mode(db: AsyncSession, merchant_id: UUID) -> str:
    profile = await get_finance_profile(db, merchant_id)
    if not profile or not profile.is_active:
        return MerchantSettlementMode.postpaid.value
    return profile.settlement_mode


async def get_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    lock: bool = False,
) -> MerchantRollingAccount | None:
    query = select(MerchantRollingAccount).where(
        MerchantRollingAccount.merchant_id == merchant_id
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
    *,
    lock: bool = True,
) -> MerchantRollingAccount:
    account = await get_rolling_account(db, merchant_id, lock=lock)
    if account:
        return account
    account = MerchantRollingAccount(
        merchant_id=merchant_id,
        principal_usdt=Decimal('0.000000'),
        recovered_usdt=Decimal('0.000000'),
        outstanding_usdt=Decimal('0.000000'),
        status=RollingAccountStatus.exhausted.value,
    )
    db.add(account)
    await db.flush()
    return account


async def rolling_quote_for_deposit_create(
    db: AsyncSession,
    merchant_id: UUID,
) -> RollingRapiraQuote | None:
    if await merchant_settlement_mode(db, merchant_id) != MerchantSettlementMode.rolling.value:
        return None
    account = await get_rolling_account(db, merchant_id)
    if account and account.status == RollingAccountStatus.suspended.value:
        raise RollingAccountSuspended('rolling account is suspended')
    return await get_strict_rolling_ask_quote()


async def create_pending_allocation(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    quote: RollingRapiraQuote | None,
) -> MerchantRollingAllocation | None:
    if quote is None:
        return None
    existing = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    if existing:
        return existing
    profile = await get_finance_profile(db, deposit.merchant_id, lock=True)
    if not profile or profile.settlement_mode != MerchantSettlementMode.rolling.value:
        raise RollingModeConflict('merchant is not in rolling mode')
    account = await get_or_create_rolling_account(db, deposit.merchant_id, lock=True)
    if account.status == RollingAccountStatus.suspended.value:
        raise RollingAccountSuspended('rolling account is suspended')
    gross = money(snapshot.calculation_base_amount)
    merchant_fee = money(snapshot.merchant_fee_amount)
    merchant_net = money(gross - merchant_fee)
    if merchant_net < 0:
        raise RollingError('merchant net cannot be negative')
    rate_value = rolling_rate(quote.rate)
    merchant_net_usdt = usdt(merchant_net / rate_value)
    allocation = MerchantRollingAllocation(
        deposit_id=deposit.id,
        merchant_id=deposit.merchant_id,
        rolling_account_id=account.id,
        gross_rub=gross,
        merchant_fee_percent_snapshot=percentage(snapshot.merchant_rate_percent),
        merchant_fee_rub=merchant_fee,
        merchant_net_rub=merchant_net,
        rapira_rate_rub=rate_value,
        rapira_rate_symbol=quote.symbol,
        rapira_rate_side=quote.side,
        rapira_rate_source=quote.source,
        rapira_rate_field=quote.provider_field,
        rapira_rate_updated_at=quote.updated_at,
        rapira_provider_timestamp=quote.provider_timestamp,
        rapira_fetched_at=quote.fetched_at,
        rapira_freshness_basis=quote.freshness_basis,
        merchant_net_usdt=merchant_net_usdt,
        status=RollingAllocationStatus.pending.value,
    )
    db.add(allocation)
    await db.flush()
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=deposit.merchant_id,
        deposit_id=deposit.id,
        entry_type=RollingLedgerType.pending_added.value,
        amount_usdt=merchant_net_usdt,
        amount_rub=merchant_net,
        rate_rub=rate_value,
        reason='rolling pending exposure created',
        idempotency_key=f'rolling:{deposit.id}:pending-added',
        metadata_json={
            'rapira_side': quote.side,
            'rapira_source': quote.source,
            'rapira_field': quote.provider_field,
            'rapira_symbol': quote.symbol,
            'rapira_provider_timestamp': (
                quote.provider_timestamp.isoformat()
                if quote.provider_timestamp
                else None
            ),
            'rapira_fetched_at': quote.fetched_at.isoformat(),
            'rapira_freshness_basis': quote.freshness_basis,
        },
    ))
    return allocation


async def apply_merchant_financing(
    db: AsyncSession,
    *,
    deposit: Deposit,
    snapshot: OperationFeeSnapshot,
    merchant_net_rub: Decimal,
    description: str,
) -> dict:
    merchant_net_rub = money(merchant_net_rub)
    allocation_ref = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    # Financing mode is frozen by the presence of the allocation at create time.
    # A later merchant mode switch must never reinterpret an existing deposit.
    if not allocation_ref:
        if merchant_net_rub > 0:
            await credit(
                db,
                deposit.merchant_id,
                merchant_net_rub,
                deposit.id,
                f'deposit-confirm:{deposit.id}:net',
                description,
            )
        return {
            'settlement_mode': MerchantSettlementMode.postpaid.value,
            'rolling_applied_usdt': Decimal('0.000000'),
            'rolling_applied_rub': Decimal('0.00'),
            'settle_credited_rub': merchant_net_rub,
            'rapira_rate_rub': None,
            'rapira_rate_side': None,
            'rapira_rate_source': None,
        }

    account = await get_rolling_account(db, deposit.merchant_id, lock=True)
    if not account:
        raise RollingError('rolling account is missing')
    allocation = (await db.execute(
        select(MerchantRollingAllocation)
        .where(MerchantRollingAllocation.id == allocation_ref.id)
        .with_for_update()
    )).scalar_one_or_none()
    if not allocation:
        raise RollingError('rolling allocation is missing')
    if allocation.status == RollingAllocationStatus.paid.value:
        return _allocation_financing_result(allocation)
    if allocation.status != RollingAllocationStatus.pending.value:
        raise RollingError(f'rolling allocation status {allocation.status} cannot be paid')
    if money(allocation.merchant_net_rub) != merchant_net_rub:
        raise RollingError('rolling allocation merchant net does not match fee snapshot')

    outstanding = usdt(account.outstanding_usdt)
    net_usdt = usdt(allocation.merchant_net_usdt)
    applied_usdt = min(net_usdt, outstanding)
    if applied_usdt == net_usdt:
        applied_rub = merchant_net_rub
    else:
        applied_rub = min(
            merchant_net_rub,
            money(applied_usdt * rolling_rate(allocation.rapira_rate_rub)),
        )
    settle_credit = money(merchant_net_rub - applied_rub)
    if money(applied_rub + settle_credit) != merchant_net_rub:
        raise RollingError('rolling RUB split invariant failed')

    account.recovered_usdt = usdt(account.recovered_usdt + applied_usdt)
    account.outstanding_usdt = usdt(account.outstanding_usdt - applied_usdt)
    if account.outstanding_usdt < 0:
        raise RollingError('rolling outstanding cannot be negative')
    if account.outstanding_usdt == 0:
        account.status = RollingAccountStatus.exhausted.value
    elif account.status != RollingAccountStatus.suspended.value:
        account.status = RollingAccountStatus.active.value

    allocation.rolling_applied_usdt = applied_usdt
    allocation.rolling_applied_rub = applied_rub
    allocation.settle_credited_rub = settle_credit
    allocation.status = RollingAllocationStatus.paid.value
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=deposit.merchant_id,
        deposit_id=deposit.id,
        entry_type=RollingLedgerType.pending_released.value,
        amount_usdt=net_usdt,
        amount_rub=merchant_net_rub,
        rate_rub=allocation.rapira_rate_rub,
        reason='rolling pending exposure finalized as paid',
        idempotency_key=f'rolling:{deposit.id}:pending-released-paid',
    ))
    if applied_usdt > 0:
        db.add(MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.recovery.value,
            amount_usdt=applied_usdt,
            amount_rub=applied_rub,
            rate_rub=allocation.rapira_rate_rub,
            reason='successful deposit rolling recovery',
            idempotency_key=f'rolling:{deposit.id}:recovery',
        ))
    if settle_credit > 0:
        await credit(
            db,
            deposit.merchant_id,
            settle_credit,
            deposit.id,
            f'deposit-confirm:{deposit.id}:net',
            f'{description} (rolling overflow)',
        )
        db.add(MerchantRollingLedgerEntry(
            rolling_account_id=account.id,
            merchant_id=deposit.merchant_id,
            deposit_id=deposit.id,
            entry_type=RollingLedgerType.settle_overflow.value,
            amount_usdt=usdt(settle_credit / rolling_rate(allocation.rapira_rate_rub)),
            amount_rub=settle_credit,
            rate_rub=allocation.rapira_rate_rub,
            reason='successful deposit settle overflow',
            idempotency_key=f'rolling:{deposit.id}:settle-overflow',
        ))
    return _allocation_financing_result(allocation)


def _allocation_financing_result(allocation: MerchantRollingAllocation) -> dict:
    return {
        'settlement_mode': MerchantSettlementMode.rolling.value,
        'rolling_applied_usdt': usdt(allocation.rolling_applied_usdt),
        'rolling_applied_rub': money(allocation.rolling_applied_rub),
        'settle_credited_rub': money(allocation.settle_credited_rub),
        'rapira_rate_rub': rolling_rate(allocation.rapira_rate_rub),
        'rapira_rate_symbol': allocation.rapira_rate_symbol,
        'rapira_rate_side': allocation.rapira_rate_side,
        'rapira_rate_source': allocation.rapira_rate_source,
        'rapira_provider_timestamp': allocation.rapira_provider_timestamp,
        'rapira_fetched_at': allocation.rapira_fetched_at,
        'rapira_freshness_basis': allocation.rapira_freshness_basis,
    }


async def release_pending_allocation(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
) -> MerchantRollingAllocation | None:
    allocation_ref = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    if not allocation_ref:
        return None
    account = (await db.execute(
        select(MerchantRollingAccount)
        .where(MerchantRollingAccount.id == allocation_ref.rolling_account_id)
        .with_for_update()
    )).scalar_one()
    allocation = (await db.execute(
        select(MerchantRollingAllocation)
        .where(MerchantRollingAllocation.id == allocation_ref.id)
        .with_for_update()
    )).scalar_one()
    if allocation.status != RollingAllocationStatus.pending.value:
        return allocation
    allocation.status = RollingAllocationStatus.released.value
    allocation.release_reason = (reason or 'failed')[:64]
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=deposit.merchant_id,
        deposit_id=deposit.id,
        entry_type=RollingLedgerType.pending_released.value,
        amount_usdt=allocation.merchant_net_usdt,
        amount_rub=allocation.merchant_net_rub,
        rate_rub=allocation.rapira_rate_rub,
        reason=f'rolling pending exposure released: {allocation.release_reason}',
        idempotency_key=f'rolling:{deposit.id}:pending-released',
    ))
    return allocation


async def reopen_released_allocation(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
    idempotency_suffix: str,
) -> MerchantRollingAllocation | None:
    """Restore pending exposure for an approved appeal without changing its snapshot."""
    allocation_ref = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    if not allocation_ref:
        return None
    account = (await db.execute(
        select(MerchantRollingAccount)
        .where(MerchantRollingAccount.id == allocation_ref.rolling_account_id)
        .with_for_update()
    )).scalar_one()
    allocation = (await db.execute(
        select(MerchantRollingAllocation)
        .where(MerchantRollingAllocation.id == allocation_ref.id)
        .with_for_update()
    )).scalar_one()
    if allocation.status == RollingAllocationStatus.pending.value:
        return allocation
    if allocation.status != RollingAllocationStatus.released.value:
        raise RollingError(
            f'rolling allocation status {allocation.status} cannot be reopened'
        )
    allocation.status = RollingAllocationStatus.pending.value
    allocation.release_reason = None
    allocation.finalized_at = None
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=deposit.merchant_id,
        deposit_id=deposit.id,
        entry_type=RollingLedgerType.pending_added.value,
        amount_usdt=allocation.merchant_net_usdt,
        amount_rub=allocation.merchant_net_rub,
        rate_rub=allocation.rapira_rate_rub,
        reason=f'rolling pending exposure reopened: {reason}'[:500],
        idempotency_key=(
            f'rolling:{deposit.id}:pending-reopened:{idempotency_suffix}'
        )[:180],
    ))
    return allocation


async def set_merchant_settlement_mode(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    mode: str,
    actor_id: UUID,
) -> tuple[MerchantFinanceProfile, MerchantRollingAccount | None]:
    try:
        normalized = MerchantSettlementMode(mode).value
    except ValueError as exc:
        raise RollingModeConflict('unknown merchant settlement mode') from exc
    profile = await get_or_create_finance_profile(
        db,
        merchant_id,
        actor_id=actor_id,
        lock=True,
    )
    account = await get_rolling_account(db, merchant_id, lock=True)
    if normalized == MerchantSettlementMode.postpaid.value and profile.settlement_mode == MerchantSettlementMode.rolling.value:
        if account and usdt(account.outstanding_usdt) > 0:
            raise RollingModeConflict('cannot disable rolling while outstanding is positive')
        pending = (await db.execute(
            select(func.count(MerchantRollingAllocation.id)).where(
                MerchantRollingAllocation.merchant_id == merchant_id,
                MerchantRollingAllocation.status == RollingAllocationStatus.pending.value,
            )
        )).scalar_one()
        if pending:
            raise RollingModeConflict('cannot disable rolling while allocations are pending')
        if account:
            account.status = RollingAccountStatus.closed.value
    elif normalized == MerchantSettlementMode.rolling.value:
        account = account or await get_or_create_rolling_account(db, merchant_id, lock=True)
        if account.status == RollingAccountStatus.closed.value:
            account.status = (
                RollingAccountStatus.active.value
                if usdt(account.outstanding_usdt) > 0
                else RollingAccountStatus.exhausted.value
            )
    profile.settlement_mode = normalized
    profile.updated_by = actor_id
    return profile, account


async def register_funding(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    actor_id: UUID,
    amount_usdt: Decimal,
    network: str,
    destination_address: str,
    tx_hash: str,
    funded_at: datetime,
    reason: str,
) -> MerchantRollingLedgerEntry:
    amount = usdt(amount_usdt)
    if amount <= 0:
        raise RollingError('funding amount must be positive')
    network = (network or '').strip().upper()
    destination_address = (destination_address or '').strip()
    tx_hash = (tx_hash or '').strip()
    reason = (reason or '').strip()
    if not network or not destination_address or not tx_hash or not reason:
        raise RollingError('network, destination address, tx hash, and reason are required')
    if not funded_at.tzinfo:
        funded_at = funded_at.replace(tzinfo=timezone.utc)
    fingerprint = hashlib.sha256(
        f'{merchant_id}|{network}|{tx_hash}'.encode('utf-8')
    ).hexdigest()
    idempotency_key = f'rolling-funding:{fingerprint}'
    existing = (await db.execute(
        select(MerchantRollingLedgerEntry).where(
            MerchantRollingLedgerEntry.idempotency_key == idempotency_key
        )
    )).scalar_one_or_none()
    if existing:
        if (
            existing.merchant_id != merchant_id
            or usdt(existing.amount_usdt) != amount
            or existing.network != network
            or existing.tx_hash != tx_hash
        ):
            raise RollingError('funding idempotency conflict')
        return existing

    profile = await get_finance_profile(db, merchant_id, lock=True)
    if not profile or profile.settlement_mode != MerchantSettlementMode.rolling.value:
        raise RollingModeConflict('enable rolling before funding')
    account = await get_or_create_rolling_account(db, merchant_id, lock=True)
    entry_type = (
        RollingLedgerType.funding.value
        if usdt(account.principal_usdt) == 0
        else RollingLedgerType.topup.value
    )
    account.principal_usdt = usdt(account.principal_usdt + amount)
    account.outstanding_usdt = usdt(account.outstanding_usdt + amount)
    if account.status != RollingAccountStatus.suspended.value:
        account.status = RollingAccountStatus.active.value
    entry = MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=merchant_id,
        actor_id=actor_id,
        entry_type=entry_type,
        amount_usdt=amount,
        network=network,
        destination_address=destination_address,
        tx_hash=tx_hash,
        funded_at=funded_at.astimezone(timezone.utc),
        reason=reason[:500],
        idempotency_key=idempotency_key,
    )
    db.add(entry)
    await db.flush()
    return entry


async def set_rolling_suspension(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    suspended: bool,
    actor_id: UUID,
    reason: str,
) -> MerchantRollingAccount:
    reason = (reason or '').strip()
    if not reason:
        raise RollingError('reason is required')
    account = await get_rolling_account(db, merchant_id, lock=True)
    if not account:
        raise RollingError('rolling account is missing')
    account.status = (
        RollingAccountStatus.suspended.value
        if suspended
        else (
            RollingAccountStatus.active.value
            if usdt(account.outstanding_usdt) > 0
            else RollingAccountStatus.exhausted.value
        )
    )
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=merchant_id,
        actor_id=actor_id,
        entry_type=RollingLedgerType.manual_adjustment.value,
        amount_usdt=Decimal('0.000000'),
        reason=f'rolling {"suspended" if suspended else "resumed"}: {reason}'[:500],
        idempotency_key=(
            f'rolling:{account.id}:{"suspend" if suspended else "resume"}:'
            f'{datetime.now(timezone.utc).isoformat()}'
        ),
    ))
    return account


async def reverse_paid_allocation(
    db: AsyncSession,
    *,
    deposit: Deposit,
    actor_id: UUID | None,
    reason: str,
) -> MerchantRollingAllocation | None:
    reason = (reason or '').strip()
    if not reason:
        raise RollingError('reversal reason is required')
    allocation_ref = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.deposit_id == deposit.id
        )
    )).scalar_one_or_none()
    if not allocation_ref:
        return None
    account = (await db.execute(
        select(MerchantRollingAccount)
        .where(MerchantRollingAccount.id == allocation_ref.rolling_account_id)
        .with_for_update()
    )).scalar_one()
    allocation = (await db.execute(
        select(MerchantRollingAllocation)
        .where(MerchantRollingAllocation.id == allocation_ref.id)
        .with_for_update()
    )).scalar_one()
    if allocation.status == RollingAllocationStatus.reversed.value:
        return allocation
    if allocation.status != RollingAllocationStatus.paid.value:
        raise RollingError('only paid rolling allocation can be reversed')
    applied_usdt = usdt(allocation.rolling_applied_usdt)
    if usdt(account.recovered_usdt) < applied_usdt:
        raise RollingError('rolling recovered balance is lower than reversal amount')
    settle_credit = money(allocation.settle_credited_rub)
    if settle_credit > 0:
        try:
            await debit_available(
                db,
                deposit.merchant_id,
                settle_credit,
                deposit.id,
                f'rolling:{deposit.id}:settle-reversal',
                f'rolling reversal: {reason}',
            )
        except LedgerError as exc:
            raise RollingError(
                'settle credit is no longer available; receivable workflow is required'
            ) from exc
    account.recovered_usdt = usdt(account.recovered_usdt - applied_usdt)
    account.outstanding_usdt = usdt(account.outstanding_usdt + applied_usdt)
    if account.status != RollingAccountStatus.suspended.value:
        account.status = (
            RollingAccountStatus.active.value
            if account.outstanding_usdt > 0
            else RollingAccountStatus.exhausted.value
        )
    allocation.status = RollingAllocationStatus.reversed.value
    allocation.finalized_at = datetime.now(timezone.utc)
    db.add(MerchantRollingLedgerEntry(
        rolling_account_id=account.id,
        merchant_id=deposit.merchant_id,
        deposit_id=deposit.id,
        actor_id=actor_id,
        entry_type=RollingLedgerType.reversal.value,
        amount_usdt=applied_usdt,
        amount_rub=money(allocation.rolling_applied_rub + settle_credit),
        rate_rub=allocation.rapira_rate_rub,
        reason=reason[:500],
        idempotency_key=f'rolling:{deposit.id}:reversal',
        metadata_json={
            'rolling_applied_rub': str(money(allocation.rolling_applied_rub)),
            'settle_reversed_rub': str(settle_credit),
        },
    ))
    await audit(
        db,
        'rolling_allocation_reversed',
        'deposit',
        actor_id,
        deposit.id,
        details={
            'rolling_applied_usdt': str(applied_usdt),
            'rolling_applied_rub': str(money(allocation.rolling_applied_rub)),
            'settle_reversed_rub': str(settle_credit),
            'reason': reason,
        },
    )
    return allocation


async def rolling_overview(db: AsyncSession, merchant_id: UUID) -> dict:
    profile = await get_finance_profile(db, merchant_id)
    mode = (
        profile.settlement_mode
        if profile and profile.is_active
        else MerchantSettlementMode.postpaid.value
    )
    if mode != MerchantSettlementMode.rolling.value:
        return {'settlement_mode': mode, 'rolling': None}
    account = await get_rolling_account(db, merchant_id)
    if not account:
        return {'settlement_mode': mode, 'rolling': None}
    pending = usdt((await db.execute(
        select(
            func.coalesce(func.sum(MerchantRollingAllocation.merchant_net_usdt), 0)
        ).where(
            MerchantRollingAllocation.merchant_id == merchant_id,
            MerchantRollingAllocation.status == RollingAllocationStatus.pending.value,
        )
    )).scalar_one())
    outstanding = usdt(account.outstanding_usdt)
    return {
        'settlement_mode': mode,
        'rolling': {
            'principal_usdt': usdt(account.principal_usdt),
            'recovered_usdt': usdt(account.recovered_usdt),
            'outstanding_usdt': outstanding,
            'pending_exposure_usdt': pending,
            'covered_pending_usdt': min(outstanding, pending),
            'uncovered_pending_usdt': max(usdt(pending - outstanding), Decimal('0.000000')),
            'status': account.status,
        },
    }


@dataclass(frozen=True)
class RollingReconciliationReport:
    merchant_id: UUID
    ok: bool
    expected_principal_usdt: Decimal
    actual_principal_usdt: Decimal
    expected_recovered_usdt: Decimal
    actual_recovered_usdt: Decimal
    expected_outstanding_usdt: Decimal
    actual_outstanding_usdt: Decimal
    pending_exposure_usdt: Decimal
    ledger_pending_exposure_usdt: Decimal
    settle_overflow_rub: Decimal
    ledger_settle_overflow_rub: Decimal
    released_allocations: int
    paid_split_mismatches: tuple[str, ...]


async def reconcile_rolling_account(
    db: AsyncSession,
    merchant_id: UUID,
) -> RollingReconciliationReport | None:
    account = await get_rolling_account(db, merchant_id)
    if not account:
        return None
    ledger = (await db.execute(
        select(MerchantRollingLedgerEntry).where(
            MerchantRollingLedgerEntry.rolling_account_id == account.id
        )
    )).scalars().all()
    expected_principal = usdt(sum(
        (
            usdt(row.amount_usdt)
            for row in ledger
            if row.entry_type in {
                RollingLedgerType.funding.value,
                RollingLedgerType.topup.value,
            }
        ),
        Decimal('0'),
    ))
    recoveries = usdt(sum(
        (
            usdt(row.amount_usdt)
            for row in ledger
            if row.entry_type == RollingLedgerType.recovery.value
        ),
        Decimal('0'),
    ))
    reversals = usdt(sum(
        (
            usdt(row.amount_usdt)
            for row in ledger
            if row.entry_type == RollingLedgerType.reversal.value
        ),
        Decimal('0'),
    ))
    expected_recovered = usdt(recoveries - reversals)
    expected_outstanding = usdt(expected_principal - expected_recovered)
    pending_exposure = usdt((await db.execute(
        select(
            func.coalesce(func.sum(MerchantRollingAllocation.merchant_net_usdt), 0)
        ).where(
            MerchantRollingAllocation.rolling_account_id == account.id,
            MerchantRollingAllocation.status == RollingAllocationStatus.pending.value,
        )
    )).scalar_one())
    pending_added = usdt(sum(
        (
            usdt(row.amount_usdt)
            for row in ledger
            if row.entry_type == RollingLedgerType.pending_added.value
        ),
        Decimal('0'),
    ))
    pending_released = usdt(sum(
        (
            usdt(row.amount_usdt)
            for row in ledger
            if row.entry_type == RollingLedgerType.pending_released.value
        ),
        Decimal('0'),
    ))
    ledger_pending_exposure = usdt(pending_added - pending_released)
    paid_allocations = (await db.execute(
        select(MerchantRollingAllocation).where(
            MerchantRollingAllocation.rolling_account_id == account.id,
            MerchantRollingAllocation.status == RollingAllocationStatus.paid.value,
        )
    )).scalars().all()
    released_allocations = (await db.execute(
        select(func.count(MerchantRollingAllocation.id)).where(
            MerchantRollingAllocation.rolling_account_id == account.id,
            MerchantRollingAllocation.status == RollingAllocationStatus.released.value,
        )
    )).scalar_one()
    settle_overflow = money(sum(
        (money(row.settle_credited_rub) for row in paid_allocations),
        Decimal('0.00'),
    ))
    ledger_settle_overflow = money(sum(
        (
            money(row.amount_rub)
            for row in ledger
            if row.entry_type == RollingLedgerType.settle_overflow.value
        ),
        Decimal('0.00'),
    ) - sum(
        (
            money((row.metadata_json or {}).get('settle_reversed_rub', 0))
            for row in ledger
            if row.entry_type == RollingLedgerType.reversal.value
        ),
        Decimal('0.00'),
    ))
    split_mismatches = tuple(
        str(row.deposit_id)
        for row in paid_allocations
        if money(row.rolling_applied_rub + row.settle_credited_rub)
        != money(row.merchant_net_rub)
    )
    actual_principal = usdt(account.principal_usdt)
    actual_recovered = usdt(account.recovered_usdt)
    actual_outstanding = usdt(account.outstanding_usdt)
    ok = (
        expected_principal == actual_principal
        and expected_recovered == actual_recovered
        and expected_outstanding == actual_outstanding
        and ledger_pending_exposure == pending_exposure
        and ledger_settle_overflow == settle_overflow
        and not split_mismatches
    )
    if not ok:
        logger.error(
            'rolling_reconciliation_mismatch',
            extra={
                'merchant_id': str(merchant_id),
                'principal_delta': str(actual_principal - expected_principal),
                'recovered_delta': str(actual_recovered - expected_recovered),
                'outstanding_delta': str(actual_outstanding - expected_outstanding),
                'split_mismatch_count': len(split_mismatches),
            },
        )
    return RollingReconciliationReport(
        merchant_id=merchant_id,
        ok=ok,
        expected_principal_usdt=expected_principal,
        actual_principal_usdt=actual_principal,
        expected_recovered_usdt=expected_recovered,
        actual_recovered_usdt=actual_recovered,
        expected_outstanding_usdt=expected_outstanding,
        actual_outstanding_usdt=actual_outstanding,
        pending_exposure_usdt=pending_exposure,
        ledger_pending_exposure_usdt=ledger_pending_exposure,
        settle_overflow_rub=settle_overflow,
        ledger_settle_overflow_rub=ledger_settle_overflow,
        released_allocations=released_allocations,
        paid_split_mismatches=split_mismatches,
    )
