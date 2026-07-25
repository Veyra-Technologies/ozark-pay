from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit
from app.services.fee_tiers import FeeConfigurationError, get_operation_fee_snapshot, money, rate
from app.services.ledger import (
    credit,
    record_fee,
    record_platform_executor_fee,
    record_platform_income,
)


async def settle_deposit_credit(
    db: AsyncSession,
    *,
    merchant_id: UUID,
    method: str,
    amount: Decimal,
    operation_id: UUID,
    description: str,
) -> dict:
    """Settle exclusively from the immutable operation fee snapshot."""
    deposit = (await db.execute(
        select(Deposit).where(Deposit.id == operation_id).with_for_update()
    )).scalar_one_or_none()
    if not deposit or deposit.merchant_id != merchant_id:
        raise FeeConfigurationError('deposit does not match settlement target')
    snapshot = await get_operation_fee_snapshot(db, operation_id, lock=True)
    gross = money(amount)
    if money(deposit.amount) != gross or money(snapshot.calculation_base_amount) != gross:
        raise FeeConfigurationError('fee snapshot amount does not match deposit')
    if snapshot.currency != deposit.currency or snapshot.payment_method != deposit.method:
        raise FeeConfigurationError('fee snapshot operation dimensions do not match deposit')

    merchant_fee = money(snapshot.merchant_fee_amount)
    executor_fee = money(snapshot.executor_fee_amount)
    platform_income = money(snapshot.platform_income_amount)
    if merchant_fee != money(executor_fee + platform_income):
        raise FeeConfigurationError('fee snapshot invariant is broken')
    net = money(gross - merchant_fee)
    if net < Decimal('0.00') or platform_income < Decimal('0.00'):
        raise FeeConfigurationError('fee snapshot contains a negative settlement amount')

    metadata = dict(deposit.metadata_json or {})
    metadata.update({
        'merchant_commission_percent': str(rate(snapshot.merchant_rate_percent)),
        'merchant_fee_amount': str(merchant_fee),
        'merchant_net_amount': str(net),
        'executor_type': snapshot.executor_type,
        'executor_id': str(snapshot.executor_id),
        'executor_fee_amount': str(executor_fee),
        'platform_income_amount': str(platform_income),
        'fee_snapshot_id': str(snapshot.id),
    })
    deposit.metadata_json = metadata

    if net > 0:
        await credit(
            db, merchant_id, net, operation_id,
            f'deposit-confirm:{operation_id}:net', description,
        )
    await record_fee(
        db, merchant_id, merchant_fee, operation_id,
        f'deposit-confirm:{operation_id}:merchant-fee',
        f'merchant fee for {description}',
    )
    executor_entry = await record_platform_executor_fee(
        db, merchant_id, executor_fee, operation_id,
        f'deposit-confirm:{operation_id}:executor-fee',
        f'{snapshot.executor_type} executor fee for {description}',
    )
    income_entry = await record_platform_income(
        db, merchant_id, platform_income, operation_id,
        f'deposit-confirm:{operation_id}:platform-income',
        f'platform income for {description}',
    )
    await db.flush()
    snapshot.settlement_status = 'settled'
    snapshot.settled_at = datetime.now(timezone.utc)
    snapshot.ledger_reference = income_entry.id if income_entry else None

    return {
        'gross_amount': str(gross),
        'fee_amount': str(merchant_fee),
        'net_amount': str(net),
        'merchant_commission_percent': str(rate(snapshot.merchant_rate_percent)),
        'merchant_fee_amount': str(merchant_fee),
        'merchant_net_amount': str(net),
        'executor_type': snapshot.executor_type,
        'executor_id': str(snapshot.executor_id),
        'executor_fee_amount': str(executor_fee),
        'platform_income_amount': str(platform_income),
        'merchant_rate_rule_id': str(snapshot.merchant_rate_rule_id),
        'executor_rate_rule_id': str(snapshot.executor_rate_rule_id),
        'fee_snapshot_id': str(snapshot.id),
        'executor_ledger_entry_id': str(executor_entry.id) if executor_entry else None,
    }
