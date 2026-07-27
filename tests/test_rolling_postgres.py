import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import AppealStatus, DepositStatus, Role
from app.core.security import auth_state_marker, create_token, hash_password
from app.db.session import engine as application_engine
from app.main import app
from app.models import (
    ApiKey,
    Appeal,
    Balance,
    Deposit,
    FeeRule,
    LedgerEntry,
    Merchant,
    MerchantRollingAccount,
    MerchantRollingAllocation,
    MerchantRollingLedgerEntry,
    OperationFeeSnapshot,
    Requisite,
    TraderLedgerEntry,
    User,
    WebhookDeliveryAttempt,
    WebhookEvent,
)
from app.services.appeals import approve_deposit_appeal, reject_deposit_appeal
from app.services.deposit_confirmation import (
    DepositConfirmationConflict,
    confirm_deposit_payment,
)
from app.services.deposit_lifecycle import (
    expire_due_deposits,
    finalize_unsuccessful_deposit,
)
from app.services.ledger import debit_available
from app.services.rapira import RollingRapiraQuote
from app.services.rolling import (
    RollingError,
    RollingModeConflict,
    apply_merchant_financing,
    create_pending_allocation,
    merchant_settlement_mode,
    reconcile_rolling_account,
    register_funding,
    reverse_paid_allocation,
    rolling_quote_for_deposit_create,
    set_merchant_settlement_mode,
)
from app.services.webhooks import deliver_event


CONCURRENCY_TIMEOUT_SECONDS = 10


def _database_url() -> str:
    url = os.getenv('TEST_DATABASE_URL', '')
    if not url:
        pytest.skip('TEST_DATABASE_URL is required for PostgreSQL locking tests')
    if 'postgresql' not in url:
        pytest.fail('Rolling integration tests require PostgreSQL')
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+asyncpg://', 1)
    return url


async def _concurrent(*awaitables):
    return await asyncio.wait_for(
        asyncio.gather(*awaitables),
        timeout=CONCURRENCY_TIMEOUT_SECONDS,
    )


async def _merchant_fixture(db: AsyncSession, *, rolling=True):
    suffix = uuid.uuid4().hex
    owner = User(
        email=f'merchant-{suffix}@example.test',
        password_hash='not-used-in-test',
        role=Role.merchant.value,
    )
    actor = User(
        email=f'admin-{suffix}@example.test',
        password_hash='not-used-in-test',
        role=Role.superadmin.value,
        twofa_enabled=True,
    )
    db.add_all([owner, actor])
    await db.flush()
    merchant = Merchant(
        owner_id=owner.id,
        name=f'Test merchant {suffix}',
    )
    db.add(merchant)
    await db.flush()
    if rolling:
        await set_merchant_settlement_mode(
            db,
            merchant_id=merchant.id,
            mode='rolling',
            actor_id=actor.id,
        )
    return merchant, actor


async def _deposit_fixture(
    db: AsyncSession,
    merchant: Merchant,
    *,
    gross='100000.00',
    fee='10000.00',
    rate='100.00000000',
    merchant_percent='10',
    create_allocation=True,
    expires_at=None,
):
    now = datetime.now(timezone.utc)
    merchant_rule = FeeRule(
        method='sbp',
        percent=Decimal(merchant_percent),
        entity_type='merchant',
        entity_id=merchant.id,
        fee_side='merchant_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        max_amount=None,
        rate_percent=Decimal(merchant_percent),
        effective_from=now,
    )
    executor_rule = FeeRule(
        method='sbp',
        percent=Decimal('5'),
        entity_type='trader',
        entity_id=uuid.uuid4(),
        fee_side='executor_fee',
        payment_method='sbp',
        currency='RUB',
        min_amount=Decimal('0'),
        max_amount=None,
        rate_percent=Decimal('5'),
        effective_from=now,
    )
    db.add_all([merchant_rule, executor_rule])
    await db.flush()
    deposit = Deposit(
        merchant_id=merchant.id,
        external_id=f'test-{uuid.uuid4()}',
        idempotency_key=f'test-{uuid.uuid4()}',
        amount=Decimal(gross),
        currency='RUB',
        method='sbp',
        status=DepositStatus.pending.value,
        expires_at=expires_at or (now + timedelta(minutes=5)),
        metadata_json={},
    )
    db.add(deposit)
    await db.flush()
    fee_amount = Decimal(fee)
    merchant_rate = Decimal(merchant_percent)
    executor_rate = min(Decimal('5'), max(merchant_rate, Decimal('0')))
    executor_fee = min(
        fee_amount,
        (
            Decimal(gross) * executor_rate / Decimal('100')
        ).quantize(Decimal('0.01')),
    )
    platform_income = fee_amount - executor_fee
    snapshot = OperationFeeSnapshot(
        deposit_id=deposit.id,
        merchant_id=merchant.id,
        merchant_rate_rule_id=merchant_rule.id,
        merchant_rate_version=1,
        merchant_rate_percent=merchant_rate,
        merchant_fee_amount=fee_amount,
        executor_type='trader',
        executor_id=executor_rule.entity_id,
        executor_rate_rule_id=executor_rule.id,
        executor_rate_version=1,
        executor_rate_percent=executor_rate,
        executor_fee_amount=executor_fee,
        platform_margin_percent=merchant_rate - executor_rate,
        platform_income_amount=platform_income,
        calculation_base_amount=Decimal(gross),
        currency='RUB',
        payment_method='sbp',
        rate_snapshot_at=now,
    )
    db.add(snapshot)
    await db.flush()
    quote = RollingRapiraQuote(
        symbol='USDT/RUB',
        rate=Decimal(rate),
        side='ask',
        source='Rapira',
        received_at=now,
        market_updated_at=now,
        stale=False,
        provider_field='askPrice',
    )
    allocation = None
    if create_allocation:
        allocation = await create_pending_allocation(
            db,
            deposit=deposit,
            snapshot=snapshot,
            quote=quote,
        )
    return deposit, snapshot, allocation


def test_postgres_funding_partial_split_reconciliation_and_reversal_baseline():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                entry = await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('500'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='integration test',
                )
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='integration test',
                )
                await db.flush()
                assert entry.entry_type == 'funding'
                assert result['rolling_applied_usdt'] == Decimal('500.000000')
                assert result['rolling_applied_rub'] == Decimal('50000.00')
                assert result['settle_credited_rub'] == Decimal('40000.00')
                assert allocation.status == 'paid'
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.outstanding_usdt == Decimal('0.000000')
                balance = (await db.execute(
                    select(Balance).where(
                        Balance.merchant_id == merchant.id,
                        Balance.currency == 'RUB',
                    )
                )).scalar_one()
                assert balance.available == Decimal('40000.00')
                report = await reconcile_rolling_account(db, merchant.id)
                assert report is not None and report.ok
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_pending_may_exceed_outstanding_and_mode_switch_is_blocked():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('100'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='integration test',
                )
                _, _, allocation = await _deposit_fixture(db, merchant)
                assert allocation.merchant_net_usdt == Decimal('900.000000')
                with pytest.raises(RollingModeConflict):
                    await set_merchant_settlement_mode(
                        db,
                        merchant_id=merchant.id,
                        mode='postpaid',
                        actor_id=actor.id,
                    )
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_concurrent_paid_allocations_do_not_overrecover():
    async def scenario():
        engine = create_async_engine(_database_url())
        merchant_id = None
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                merchant_id = merchant.id
                await register_funding(
                    setup,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('1000'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='concurrency test',
                )
                dep1, snap1, _ = await _deposit_fixture(setup, merchant)
                dep2, snap2, _ = await _deposit_fixture(setup, merchant)
                ids = (dep1.id, snap1.id, dep2.id, snap2.id)
                await setup.commit()

            async def pay(deposit_id, snapshot_id):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    deposit = (await db.execute(
                        select(Deposit).where(Deposit.id == deposit_id).with_for_update()
                    )).scalar_one()
                    snapshot = (await db.execute(
                        select(OperationFeeSnapshot).where(
                            OperationFeeSnapshot.id == snapshot_id
                        )
                    )).scalar_one()
                    result = await apply_merchant_financing(
                        db,
                        deposit=deposit,
                        snapshot=snapshot,
                        merchant_net_rub=Decimal('90000'),
                        description='concurrency test',
                    )
                    deposit.status = DepositStatus.paid.value
                    await db.commit()
                    return result

            results = await _concurrent(
                pay(ids[0], ids[1]),
                pay(ids[2], ids[3]),
            )
            assert sum(
                (row['rolling_applied_usdt'] for row in results),
                Decimal('0'),
            ) == Decimal('1000.000000')
            assert sum(
                (row['settle_credited_rub'] for row in results),
                Decimal('0'),
            ) == Decimal('80000.00')
            async with AsyncSession(engine) as verify:
                account = (await verify.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant_id
                    )
                )).scalar_one()
                assert account.outstanding_usdt == Decimal('0.000000')
                assert account.recovered_usdt == Decimal('1000.000000')
                recoveries = (await verify.execute(
                    select(MerchantRollingLedgerEntry).where(
                        MerchantRollingLedgerEntry.rolling_account_id == account.id,
                        MerchantRollingLedgerEntry.entry_type == 'recovery',
                    )
                )).scalars().all()
                assert sum(
                    (row.amount_usdt for row in recoveries),
                    Decimal('0'),
                ) == Decimal('1000.000000')
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_postpaid_default_credits_legacy_balance_without_rapira(
    monkeypatch,
):
    async def forbidden_quote():
        raise AssertionError('postpaid must not call strict Rapira')

    monkeypatch.setattr(
        'app.services.rolling.get_strict_rolling_ask_quote',
        forbidden_quote,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _ = await _merchant_fixture(db, rolling=False)
                assert await rolling_quote_for_deposit_create(
                    db,
                    merchant.id,
                ) is None
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    create_allocation=False,
                )
                assert allocation is None
                assert await merchant_settlement_mode(db, merchant.id) == 'postpaid'
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='postpaid regression',
                )
                await db.flush()
                assert result['settlement_mode'] == 'postpaid'
                assert result['settle_credited_rub'] == Decimal('90000.00')
                balance = (await db.execute(
                    select(Balance).where(
                        Balance.merchant_id == merchant.id,
                        Balance.currency == 'RUB',
                    )
                )).scalar_one()
                assert balance.available == Decimal('90000.00')
                assert await db.scalar(
                    select(func.count(MerchantRollingAllocation.id)).where(
                        MerchantRollingAllocation.merchant_id == merchant.id
                    )
                ) == 0
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_full_recovery_then_outstanding_zero_routes_all_to_settle():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('900'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='full recovery test',
                )
                first, first_snapshot, _ = await _deposit_fixture(db, merchant)
                first_result = await apply_merchant_financing(
                    db,
                    deposit=first,
                    snapshot=first_snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='full recovery',
                )
                assert first_result['rolling_applied_usdt'] == Decimal('900.000000')
                assert first_result['rolling_applied_rub'] == Decimal('90000.00')
                assert first_result['settle_credited_rub'] == Decimal('0.00')

                second, second_snapshot, _ = await _deposit_fixture(db, merchant)
                second_result = await apply_merchant_financing(
                    db,
                    deposit=second,
                    snapshot=second_snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='post-exhaustion overflow',
                )
                await db.flush()
                assert second_result['rolling_applied_usdt'] == Decimal('0.000000')
                assert second_result['rolling_applied_rub'] == Decimal('0.00')
                assert second_result['settle_credited_rub'] == Decimal('90000.00')
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.status == 'exhausted'
                assert account.outstanding_usdt == Decimal('0.000000')
                report = await reconcile_rolling_account(db, merchant.id)
                assert report is not None and report.ok
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    ('target_status', 'reason'),
    [
        (DepositStatus.failed.value, 'operator_failed'),
        (DepositStatus.cancelled.value, 'merchant_cancelled'),
    ],
)
def test_postgres_unsuccessful_finalization_releases_pending_once_without_finance(
    target_status,
    reason,
):
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('100'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='release test',
                )
                deposit, _, allocation = await _deposit_fixture(db, merchant)
                first = await finalize_unsuccessful_deposit(
                    db,
                    deposit,
                    reason=reason,
                    target_status=target_status,
                    actor_id=actor.id,
                    actor_role='admin',
                )
                second = await finalize_unsuccessful_deposit(
                    db,
                    deposit,
                    reason=reason,
                    target_status=target_status,
                    actor_id=actor.id,
                    actor_role='admin',
                )
                await db.flush()
                assert first.changed is True
                assert second.changed is False
                assert allocation.status == 'released'
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.outstanding_usdt == Decimal('100.000000')
                assert account.recovered_usdt == Decimal('0.000000')
                releases = await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit.id,
                        MerchantRollingLedgerEntry.entry_type == 'pending_released',
                    )
                )
                credits = await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == deposit.id,
                        LedgerEntry.entry_type == 'credit',
                    )
                )
                assert releases == 1
                assert credits == 0
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_expiration_worker_is_idempotent_and_releases_pending():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _ = await _merchant_fixture(db)
                expired = datetime.now(timezone.utc) - timedelta(seconds=1)
                deposit, _, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    expires_at=expired,
                )
                first = await expire_due_deposits(db, now=datetime.now(timezone.utc))
                second = await expire_due_deposits(db, now=datetime.now(timezone.utc))
                await db.flush()
                assert [row.deposit_id for row in first].count(deposit.id) == 1
                assert all(row.deposit_id != deposit.id for row in second)
                assert deposit.status == DepositStatus.failed.value
                assert deposit.metadata_json['failure_reason'] == 'trader_timeout'
                assert allocation.status == 'released'
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit.id,
                        MerchantRollingLedgerEntry.entry_type == 'pending_released',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_expiration_worker_isolates_invalid_hold_and_continues_batch():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _ = await _merchant_fixture(db)
                trader = User(
                    email=f'trader-{uuid.uuid4().hex}@example.test',
                    password_hash='not-used-in-test',
                    role=Role.trader.value,
                    trader_balance=Decimal('0.00'),
                    trader_hold=Decimal('0.00'),
                )
                db.add(trader)
                await db.flush()
                requisite = Requisite(
                    trader_id=trader.id,
                    owner_name='Invalid Hold Trader',
                    method='sbp',
                    value_encrypted='test-requisite',
                )
                db.add(requisite)
                await db.flush()
                bad, _, bad_allocation = await _deposit_fixture(
                    db,
                    merchant,
                    expires_at=(
                        datetime.now(timezone.utc) - timedelta(seconds=2)
                    ),
                )
                bad.requisites_id = requisite.id
                bad.metadata_json = {
                    'trader_hold_amount': '100000.00',
                    'trader_hold_status': 'held',
                }
                healthy, _, healthy_allocation = await _deposit_fixture(
                    db,
                    merchant,
                    expires_at=(
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ),
                )

                results = await expire_due_deposits(
                    db,
                    now=datetime.now(timezone.utc),
                )
                await db.flush()
                await db.refresh(bad)
                await db.refresh(bad_allocation)

                assert all(row.deposit_id != bad.id for row in results)
                assert [row.deposit_id for row in results].count(healthy.id) == 1
                assert bad.status == DepositStatus.pending.value
                assert bad_allocation.status == 'pending'
                assert healthy.status == DepositStatus.failed.value
                assert healthy_allocation.status == 'released'
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_repeated_timeout_releases_real_trader_hold_once():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _ = await _merchant_fixture(db)
                trader = User(
                    email=f'trader-{uuid.uuid4().hex}@example.test',
                    password_hash='not-used-in-test',
                    role=Role.trader.value,
                    trader_balance=Decimal('200000.00'),
                    trader_hold=Decimal('100000.00'),
                    trader_commission_percent=Decimal('0.00'),
                )
                db.add(trader)
                await db.flush()
                requisite = Requisite(
                    trader_id=trader.id,
                    owner_name='Test Trader',
                    method='sbp',
                    value_encrypted='test-requisite',
                )
                db.add(requisite)
                await db.flush()
                deposit, _, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
                deposit.requisites_id = requisite.id
                deposit.metadata_json = {
                    **(deposit.metadata_json or {}),
                    'trader_hold_amount': '100000.00',
                    'trader_hold_status': 'held',
                }

                first = await expire_due_deposits(
                    db,
                    now=datetime.now(timezone.utc),
                )
                second = await expire_due_deposits(
                    db,
                    now=datetime.now(timezone.utc),
                )
                await db.flush()
                assert [row.deposit_id for row in first].count(deposit.id) == 1
                assert all(row.deposit_id != deposit.id for row in second)
                assert trader.trader_hold == Decimal('0.00')
                assert allocation.status == 'released'
                assert await db.scalar(
                    select(func.count(TraderLedgerEntry.id)).where(
                        TraderLedgerEntry.operation_id == deposit.id,
                        TraderLedgerEntry.entry_type == 'release_hold',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_funding_is_idempotent_and_topup_reactivates_exhausted_account():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                tx_hash = f'test-{uuid.uuid4()}'
                kwargs = {
                    'merchant_id': merchant.id,
                    'actor_id': actor.id,
                    'amount_usdt': Decimal('100'),
                    'network': 'trc20',
                    'destination_address': 'T-test-address',
                    'tx_hash': tx_hash,
                    'funded_at': datetime.now(timezone.utc),
                    'reason': 'idempotent funding',
                }
                funding = await register_funding(db, **kwargs)
                repeated = await register_funding(db, **kwargs)
                assert funding.id == repeated.id

                deposit, snapshot, _ = await _deposit_fixture(db, merchant)
                await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='exhaust account',
                )
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.status == 'exhausted'

                topup = await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('50'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='reactivate account',
                )
                assert topup.entry_type == 'topup'
                assert account.principal_usdt == Decimal('150.000000')
                assert account.outstanding_usdt == Decimal('50.000000')
                assert account.status == 'active'
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_idempotent_allocation_keeps_the_original_rate_snapshot():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, _ = await _merchant_fixture(db)
                deposit, snapshot, allocation = await _deposit_fixture(db, merchant)
                replacement = RollingRapiraQuote(
                    symbol='USDT/RUB',
                    rate=Decimal('200'),
                    side='ask',
                    source='Rapira',
                    received_at=datetime.now(timezone.utc),
                    market_updated_at=datetime.now(timezone.utc),
                    stale=False,
                    provider_field='askPrice',
                )
                repeated = await create_pending_allocation(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    quote=replacement,
                )
                assert repeated.id == allocation.id
                assert repeated.rapira_rate_rub == Decimal('100.00000000')
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit.id,
                        MerchantRollingLedgerEntry.entry_type == 'pending_added',
                    )
                ) == 1
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_pending_blocks_mode_switch_even_when_outstanding_is_zero():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await _deposit_fixture(db, merchant)
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.outstanding_usdt == Decimal('0.000000')
                with pytest.raises(RollingModeConflict):
                    await set_merchant_settlement_mode(
                        db,
                        merchant_id=merchant.id,
                        mode='postpaid',
                        actor_id=actor.id,
                    )
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_confirm_before_deadline_is_paid_and_repeat_is_rejected():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db, rolling=False)
                deposit, _, _ = await _deposit_fixture(
                    db,
                    merchant,
                    create_allocation=False,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
                result = await confirm_deposit_payment(
                    db,
                    deposit.id,
                    actor_id=actor.id,
                    actor_ip='127.0.0.1',
                    audit_action='test_confirm',
                    description='confirm before deadline',
                )
                assert result.confirmed is True
                assert result.status == DepositStatus.paid.value
                assert result.settlement['settlement_mode'] == 'postpaid'
                with pytest.raises(DepositConfirmationConflict):
                    await confirm_deposit_payment(
                        db,
                        deposit.id,
                        actor_id=actor.id,
                        actor_ip='127.0.0.1',
                        audit_action='test_confirm_repeat',
                        description='repeat confirm',
                    )
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_confirm_after_deadline_finalizes_timeout_once():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                deposit, _, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
                result = await confirm_deposit_payment(
                    db,
                    deposit.id,
                    actor_id=actor.id,
                    actor_ip='127.0.0.1',
                    audit_action='test_expired_confirm',
                    description='must not settle',
                )
                assert result.confirmed is False
                assert result.status == DepositStatus.failed.value
                assert result.reason == 'trader_timeout'
                assert allocation.status == 'released'
                with pytest.raises(DepositConfirmationConflict):
                    await confirm_deposit_payment(
                        db,
                        deposit.id,
                        actor_id=actor.id,
                        actor_ip='127.0.0.1',
                        audit_action='test_expired_confirm_repeat',
                        description='must remain failed',
                    )
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_parallel_confirm_and_expire_have_one_financial_finalization():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                deposit, _, _ = await _deposit_fixture(
                    setup,
                    merchant,
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                )
                ids = deposit.id, actor.id
                await setup.commit()

            async def confirm():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        result = await confirm_deposit_payment(
                            db,
                            ids[0],
                            actor_id=ids[1],
                            actor_ip='127.0.0.1',
                            audit_action='test_parallel_confirm',
                            description='parallel timeout',
                        )
                        return result.confirmed
                    except DepositConfirmationConflict:
                        return False

            async def expire():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    rows = await expire_due_deposits(
                        db,
                        now=datetime.now(timezone.utc),
                    )
                    await db.commit()
                    return len(rows)

            await _concurrent(confirm(), expire())
            async with AsyncSession(engine) as verify:
                stored = await verify.get(Deposit, ids[0])
                assert stored.status == DepositStatus.failed.value
                assert await verify.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == ids[0],
                        MerchantRollingLedgerEntry.entry_type == 'pending_released',
                    )
                ) == 1
                assert await verify.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == ids[0],
                        LedgerEntry.entry_type == 'credit',
                    )
                ) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_reversal_restores_exact_parts_and_reconciliation_stays_clean():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('500'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='reversal test',
                )
                deposit, snapshot, allocation = await _deposit_fixture(db, merchant)
                await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='reversal test',
                )
                reversed_allocation = await reverse_paid_allocation(
                    db,
                    deposit=deposit,
                    actor_id=actor.id,
                    reason='test reversal',
                )
                repeated = await reverse_paid_allocation(
                    db,
                    deposit=deposit,
                    actor_id=actor.id,
                    reason='test reversal repeat',
                )
                await db.flush()
                assert reversed_allocation.id == allocation.id == repeated.id
                assert allocation.status == 'reversed'
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.recovered_usdt == Decimal('0.000000')
                assert account.outstanding_usdt == Decimal('500.000000')
                balance = (await db.execute(
                    select(Balance).where(
                        Balance.merchant_id == merchant.id,
                        Balance.currency == 'RUB',
                    )
                )).scalar_one()
                assert balance.available == Decimal('0.00')
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit.id,
                        MerchantRollingLedgerEntry.entry_type == 'recovery',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit.id,
                        MerchantRollingLedgerEntry.entry_type == 'reversal',
                    )
                ) == 1
                report = await reconcile_rolling_account(db, merchant.id)
                assert report is not None and report.ok
                await db.rollback()
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_superadmin_decline_with_reason_finalizes_the_deposit(
    monkeypatch,
):
    import app.web.routes as web_routes
    from starlette.requests import Request

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                deposit, _, allocation = await _deposit_fixture(db, merchant)

                async def current_user(request, session):
                    assert session is db
                    return actor

                monkeypatch.setattr(
                    web_routes,
                    'get_current_web_user',
                    current_user,
                )
                monkeypatch.setattr(
                    web_routes,
                    'enqueue_webhook_delivery',
                    lambda event_id: True,
                )
                monkeypatch.setattr(
                    web_routes,
                    'enqueue_aggregator_callback_delivery',
                    lambda callback_id: True,
                )
                request = Request({
                    'type': 'http',
                    'method': 'POST',
                    'path': f'/cabinet/deposits/{deposit.id}/decline',
                    'headers': [],
                    'query_string': b'',
                    'scheme': 'http',
                    'server': ('testserver', 80),
                    'client': ('127.0.0.1', 12345),
                })
                response = await web_routes.cabinet_decline_deposit(
                    str(deposit.id),
                    request,
                    reason='confirmed operational rejection',
                    db=db,
                )
                assert response.status_code == 303
                assert deposit.status == DepositStatus.failed.value
                assert allocation.status == 'released'
                assert (
                    deposit.metadata_json['failure_reason']
                    == 'manual_decline:confirmed operational rejection'
                )
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_postgres_pending_created_before_topup_recovers_the_new_rolling():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                )
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.outstanding_usdt == Decimal('0.000000')

                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('100'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='topup after pending create',
                )
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='pending then topup',
                )
                deposit.status = DepositStatus.paid.value
                await db.flush()

                assert result['rolling_applied_usdt'] == Decimal('100.000000')
                assert result['rolling_applied_rub'] == Decimal('10000.00')
                assert result['settle_credited_rub'] == Decimal('80000.00')
                assert allocation.status == 'paid'
                assert account.recovered_usdt == Decimal('100.000000')
                assert account.outstanding_usdt == Decimal('0.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    (
        'gross',
        'fee',
        'merchant_percent',
        'rate',
        'outstanding',
        'expected_applied_rub',
        'expected_settle',
    ),
    [
        (
            '0.01',
            '0.00',
            '0.000001',
            '100.12345678',
            '0.000001',
            '0.00',
            '0.01',
        ),
        (
            '0.03',
            '0.01',
            '33.333333',
            '100.12345678',
            '0.000100',
            '0.01',
            '0.01',
        ),
        (
            '1.01',
            '0.00',
            '0',
            '100.00000000',
            '0.010000',
            '1.00',
            '0.01',
        ),
        (
            '1.00',
            '0.00',
            '0',
            '1.00000000',
            '0.999999',
            '1.00',
            '0.00',
        ),
        (
            '1.00',
            '0.00',
            '0',
            '3.33333333',
            '0.300000',
            '1.00',
            '0.00',
        ),
    ],
)
def test_postgres_rolling_decimal_boundary_matrix(
    gross,
    fee,
    merchant_percent,
    rate,
    outstanding,
    expected_applied_rub,
    expected_settle,
):
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                await register_funding(
                    db,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal(outstanding),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='rounding matrix',
                )
                deposit, snapshot, allocation = await _deposit_fixture(
                    db,
                    merchant,
                    gross=gross,
                    fee=fee,
                    merchant_percent=merchant_percent,
                    rate=rate,
                )
                merchant_net = Decimal(gross) - Decimal(fee)
                result = await apply_merchant_financing(
                    db,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=merchant_net,
                    description='rounding matrix',
                )
                await db.flush()

                assert result['rolling_applied_rub'] == Decimal(
                    expected_applied_rub
                )
                assert result['settle_credited_rub'] == Decimal(expected_settle)
                assert (
                    result['rolling_applied_rub']
                    + result['settle_credited_rub']
                ) == merchant_net
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.outstanding_usdt >= 0
                assert account.recovered_usdt >= 0
                assert allocation.rolling_applied_rub >= 0
                assert allocation.settle_credited_rub >= 0
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_funding_below_one_usdt_micro_is_rejected():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db)
                with pytest.raises(RollingError, match='must be positive'):
                    await register_funding(
                        db,
                        merchant_id=merchant.id,
                        actor_id=actor.id,
                        amount_usdt=Decimal('0.0000004'),
                        network='TRC20',
                        destination_address='T-test-address',
                        tx_hash=f'test-{uuid.uuid4()}',
                        funded_at=datetime.now(timezone.utc),
                        reason='below storage precision',
                    )
                account = (await db.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant.id
                    )
                )).scalar_one()
                assert account.principal_usdt == Decimal('0.000000')
                assert account.outstanding_usdt == Decimal('0.000000')
                await db.rollback()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_reversal_with_withdrawn_settle_rolls_back_completely():
    async def scenario():
        engine = create_async_engine(_database_url())
        deposit_id = None
        merchant_id = None
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                merchant_id = merchant.id
                await register_funding(
                    setup,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('500'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='reversal insufficient settle',
                )
                deposit, snapshot, _ = await _deposit_fixture(setup, merchant)
                deposit_id = deposit.id
                await apply_merchant_financing(
                    setup,
                    deposit=deposit,
                    snapshot=snapshot,
                    merchant_net_rub=Decimal('90000'),
                    description='reversal insufficient settle',
                )
                deposit.status = DepositStatus.paid.value
                await debit_available(
                    setup,
                    merchant.id,
                    Decimal('40000'),
                    deposit.id,
                    f'test:{deposit.id}:withdraw-settle',
                    'simulate merchant withdrawal',
                )
                await setup.commit()

            async with AsyncSession(engine, expire_on_commit=False) as reverse:
                deposit = (await reverse.execute(
                    select(Deposit)
                    .where(Deposit.id == deposit_id)
                    .with_for_update()
                )).scalar_one()
                with pytest.raises(
                    RollingError,
                    match='receivable workflow is required',
                ):
                    await reverse_paid_allocation(
                        reverse,
                        deposit=deposit,
                        actor_id=None,
                        reason='must fail atomically',
                    )
                await reverse.rollback()

            async with AsyncSession(engine) as verify:
                allocation = (await verify.execute(
                    select(MerchantRollingAllocation).where(
                        MerchantRollingAllocation.deposit_id == deposit_id
                    )
                )).scalar_one()
                account = (await verify.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == merchant_id
                    )
                )).scalar_one()
                balance = (await verify.execute(
                    select(Balance).where(
                        Balance.merchant_id == merchant_id,
                        Balance.currency == 'RUB',
                    )
                )).scalar_one()
                assert allocation.status == 'paid'
                assert account.recovered_usdt == Decimal('500.000000')
                assert account.outstanding_usdt == Decimal('0.000000')
                assert balance.available == Decimal('0.00')
                assert await verify.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == deposit_id,
                        MerchantRollingLedgerEntry.entry_type == 'reversal',
                    )
                ) == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_parallel_repeat_confirm_and_reversal_finish_without_deadlock():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                await register_funding(
                    setup,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('500'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='confirm reversal concurrency',
                )
                deposit, _, _ = await _deposit_fixture(setup, merchant)
                result = await confirm_deposit_payment(
                    setup,
                    deposit.id,
                    actor_id=actor.id,
                    actor_ip='127.0.0.1',
                    audit_action='test_initial_confirm',
                    description='initial paid before reversal race',
                )
                ids = result.deposit_id, actor.id

            async def repeat_confirm():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        await confirm_deposit_payment(
                            db,
                            ids[0],
                            actor_id=ids[1],
                            actor_ip='127.0.0.1',
                            audit_action='test_repeat_confirm_race',
                            description='repeat confirm race',
                        )
                    except DepositConfirmationConflict:
                        return 'conflict'
                    return 'unexpected'

            async def reverse():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    deposit = (await db.execute(
                        select(Deposit)
                        .where(Deposit.id == ids[0])
                        .with_for_update()
                    )).scalar_one()
                    allocation = await reverse_paid_allocation(
                        db,
                        deposit=deposit,
                        actor_id=ids[1],
                        reason='concurrent reversal',
                    )
                    await db.commit()
                    return allocation.status

            outcomes = await _concurrent(repeat_confirm(), reverse())
            assert set(outcomes) == {'conflict', 'reversed'}
            async with AsyncSession(engine) as verify:
                assert await verify.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == ids[0],
                        MerchantRollingLedgerEntry.entry_type == 'reversal',
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_parallel_paid_and_topup_preserve_account_invariant():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                await register_funding(
                    setup,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('500'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='paid topup race baseline',
                )
                deposit, snapshot, _ = await _deposit_fixture(setup, merchant)
                ids = deposit.id, snapshot.id, merchant.id, actor.id
                await setup.commit()

            async def pay():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    deposit = (await db.execute(
                        select(Deposit)
                        .where(Deposit.id == ids[0])
                        .with_for_update()
                    )).scalar_one()
                    snapshot = await db.get(OperationFeeSnapshot, ids[1])
                    result = await apply_merchant_financing(
                        db,
                        deposit=deposit,
                        snapshot=snapshot,
                        merchant_net_rub=Decimal('90000'),
                        description='paid topup race',
                    )
                    deposit.status = DepositStatus.paid.value
                    await db.commit()
                    return result

            async def topup():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    entry = await register_funding(
                        db,
                        merchant_id=ids[2],
                        actor_id=ids[3],
                        amount_usdt=Decimal('50'),
                        network='TRC20',
                        destination_address='T-test-address',
                        tx_hash=f'test-{uuid.uuid4()}',
                        funded_at=datetime.now(timezone.utc),
                        reason='concurrent topup',
                    )
                    await db.commit()
                    return entry.entry_type

            paid_result, entry_type = await _concurrent(pay(), topup())
            assert entry_type == 'topup'
            assert paid_result['rolling_applied_usdt'] in {
                Decimal('500.000000'),
                Decimal('550.000000'),
            }
            async with AsyncSession(engine) as verify:
                account = (await verify.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == ids[2]
                    )
                )).scalar_one()
                assert account.principal_usdt == Decimal('550.000000')
                assert (
                    account.recovered_usdt + account.outstanding_usdt
                    == account.principal_usdt
                )
                assert account.recovered_usdt >= 0
                assert account.outstanding_usdt >= 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_parallel_cancel_and_confirm_have_one_terminal_movement():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                await register_funding(
                    setup,
                    merchant_id=merchant.id,
                    actor_id=actor.id,
                    amount_usdt=Decimal('100'),
                    network='TRC20',
                    destination_address='T-test-address',
                    tx_hash=f'test-{uuid.uuid4()}',
                    funded_at=datetime.now(timezone.utc),
                    reason='cancel confirm race',
                )
                deposit, _, _ = await _deposit_fixture(setup, merchant)
                ids = deposit.id, actor.id
                await setup.commit()

            async def confirm():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    try:
                        result = await confirm_deposit_payment(
                            db,
                            ids[0],
                            actor_id=ids[1],
                            actor_ip='127.0.0.1',
                            audit_action='test_cancel_confirm_race',
                            description='cancel confirm race',
                        )
                        return result.status
                    except DepositConfirmationConflict:
                        return 'conflict'

            async def cancel():
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    deposit = (await db.execute(
                        select(Deposit)
                        .where(Deposit.id == ids[0])
                        .with_for_update()
                    )).scalar_one()
                    result = await finalize_unsuccessful_deposit(
                        db,
                        deposit,
                        reason='merchant_cancelled',
                        target_status=DepositStatus.cancelled.value,
                        actor_role='merchant',
                    )
                    await db.commit()
                    return result.status

            await _concurrent(confirm(), cancel())
            async with AsyncSession(engine) as verify:
                deposit = await verify.get(Deposit, ids[0])
                allocation = (await verify.execute(
                    select(MerchantRollingAllocation).where(
                        MerchantRollingAllocation.deposit_id == ids[0]
                    )
                )).scalar_one()
                assert deposit.status in {
                    DepositStatus.paid.value,
                    DepositStatus.cancelled.value,
                }
                assert allocation.status == (
                    'paid'
                    if deposit.status == DepositStatus.paid.value
                    else 'released'
                )
                assert await verify.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.deposit_id == ids[0],
                        MerchantRollingLedgerEntry.entry_type
                        == 'pending_released',
                    )
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_parallel_appeal_resolutions_use_deposit_then_appeal_order():
    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                deposit, _, _ = await _deposit_fixture(setup, merchant)
                await finalize_unsuccessful_deposit(
                    setup,
                    deposit,
                    reason='initial failure before appeal',
                    actor_role='system',
                )
                deposit.status = DepositStatus.appeal_opened.value
                appeal = Appeal(
                    operation_type='deposit',
                    operation_id=deposit.id,
                    created_by=actor.id,
                    status=AppealStatus.opened.value,
                    metadata_json={
                        'previous_deposit_status': DepositStatus.failed.value,
                        'deadline_at': (
                            datetime.now(timezone.utc) + timedelta(minutes=5)
                        ).isoformat(),
                    },
                )
                setup.add(appeal)
                await setup.commit()
                ids = deposit.id, appeal.id, actor.id

            async def resolve(approve):
                async with AsyncSession(engine, expire_on_commit=False) as db:
                    appeal = await db.get(Appeal, ids[1])
                    try:
                        if approve:
                            await approve_deposit_appeal(
                                db,
                                appeal,
                                actor_id=ids[2],
                            )
                        else:
                            await reject_deposit_appeal(
                                db,
                                appeal,
                                actor_id=ids[2],
                            )
                        await db.commit()
                        return 'approved' if approve else 'rejected'
                    except ValueError:
                        await db.rollback()
                        return 'lost-race'

            outcomes = await _concurrent(resolve(True), resolve(False))
            assert 'lost-race' in outcomes
            async with AsyncSession(engine) as verify:
                appeal = await verify.get(Appeal, ids[1])
                deposit = await verify.get(Deposit, ids[0])
                allocation = (await verify.execute(
                    select(MerchantRollingAllocation).where(
                        MerchantRollingAllocation.deposit_id == ids[0]
                    )
                )).scalar_one()
                if appeal.status == AppealStatus.approved.value:
                    assert deposit.status == DepositStatus.paid.value
                    assert allocation.status == 'paid'
                else:
                    assert appeal.status == AppealStatus.rejected.value
                    assert deposit.status == DepositStatus.failed.value
                    assert allocation.status == 'released'
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_http_confirm_after_deadline_commits_before_409(
    monkeypatch,
):
    monkeypatch.setattr(
        'app.api.v1.admin.enqueue_deposit_confirmation_deliveries',
        lambda result: {
            'webhook_enqueued': False,
            'aggregator_callbacks_enqueued': 0,
        },
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, actor = await _merchant_fixture(setup)
                trader = User(
                    email=f'trader-{uuid.uuid4().hex}@example.test',
                    password_hash='not-used-in-test',
                    role=Role.trader.value,
                    trader_balance=Decimal('200000.00'),
                    trader_hold=Decimal('100000.00'),
                    trader_commission_percent=Decimal('0.00'),
                )
                setup.add(trader)
                await setup.flush()
                requisite = Requisite(
                    trader_id=trader.id,
                    owner_name='Test Trader',
                    method='sbp',
                    value_encrypted='test-requisite',
                )
                setup.add(requisite)
                await setup.flush()
                deposit, _, _ = await _deposit_fixture(
                    setup,
                    merchant,
                    expires_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1),
                )
                deposit.requisites_id = requisite.id
                deposit.metadata_json = {
                    **(deposit.metadata_json or {}),
                    'trader_hold_amount': '100000.00',
                    'trader_hold_status': 'held',
                }
                await setup.commit()
                ids = deposit.id, actor.id, trader.id
                token = create_token(
                    str(actor.id),
                    'access',
                    timedelta(minutes=5),
                    {'auth': auth_state_marker(actor.password_hash)},
                )

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='http://localhost',
            ) as client:
                first = await client.post(
                    f'/api/v1/admin/deposits/{ids[0]}/confirm',
                    headers={'Authorization': f'Bearer {token}'},
                )
                assert first.status_code == 409
                assert first.json()['detail']['code'] == 'deposit_expired'

                async with AsyncSession(engine) as verify:
                    stored = await verify.get(Deposit, ids[0])
                    trader = await verify.get(User, ids[2])
                    allocation = (await verify.execute(
                        select(MerchantRollingAllocation).where(
                            MerchantRollingAllocation.deposit_id == ids[0]
                        )
                    )).scalar_one()
                    assert stored.status == DepositStatus.failed.value
                    assert (
                        stored.metadata_json['failure_reason']
                        == 'trader_timeout'
                    )
                    assert trader.trader_hold == Decimal('0.00')
                    assert allocation.status == 'released'
                    assert await verify.scalar(
                        select(func.count(WebhookEvent.id)).where(
                            WebhookEvent.merchant_id == merchant.id,
                            WebhookEvent.event_type == 'deposit.failed',
                        )
                    ) == 1
                    assert await verify.scalar(
                        select(func.count(TraderLedgerEntry.id)).where(
                            TraderLedgerEntry.operation_id == ids[0],
                            TraderLedgerEntry.entry_type == 'release_hold',
                        )
                    ) == 1
                    assert await verify.scalar(
                        select(func.count(LedgerEntry.id)).where(
                            LedgerEntry.operation_id == ids[0],
                            LedgerEntry.entry_type == 'credit',
                        )
                    ) == 0

                repeated = await client.post(
                    f'/api/v1/admin/deposits/{ids[0]}/confirm',
                    headers={'Authorization': f'Bearer {token}'},
                )
                assert repeated.status_code == 409

            async with AsyncSession(engine) as verify:
                assert await verify.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.merchant_id == merchant.id,
                        WebhookEvent.event_type == 'deposit.failed',
                    )
                ) == 1
                assert await verify.scalar(
                    select(func.count(TraderLedgerEntry.id)).where(
                        TraderLedgerEntry.operation_id == ids[0],
                        TraderLedgerEntry.entry_type == 'release_hold',
                    )
                ) == 1
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_outbox_scanner_recovers_commit_enqueue_crash(
    monkeypatch,
):
    import app.services.deposit_confirmation as confirmation
    import app.services.webhook_enqueue as webhook_enqueue
    from app.workers.celery_app import deliver_due_webhooks_task

    async def setup_scenario():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                merchant, actor = await _merchant_fixture(db, rolling=False)
                deposit, _, _ = await _deposit_fixture(
                    db,
                    merchant,
                    create_allocation=False,
                    expires_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1),
                )
                result = await confirm_deposit_payment(
                    db,
                    deposit.id,
                    actor_id=actor.id,
                    actor_ip='127.0.0.1',
                    audit_action='test_outbox_commit_enqueue_crash',
                    description='outbox crash recovery',
                )
                return result, deposit.id, merchant.id
        finally:
            await engine.dispose()

    result, deposit_id, merchant_id = asyncio.run(setup_scenario())

    def failed_enqueue(event_id):
        raise RuntimeError('simulated broker outage after commit')

    monkeypatch.setattr(
        confirmation,
        'enqueue_webhook_delivery',
        failed_enqueue,
    )
    delivery = confirmation.enqueue_deposit_confirmation_deliveries(result)
    assert delivery['webhook_enqueued'] is False

    async def verify_pending():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine) as db:
                event = await db.get(WebhookEvent, result.webhook_event_id)
                assert event.status == 'queued'
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == deposit_id,
                        LedgerEntry.entry_type == 'credit',
                    )
                ) == 0
        finally:
            await engine.dispose()

    asyncio.run(verify_pending())

    enqueued = []
    monkeypatch.setattr(
        webhook_enqueue,
        'enqueue_webhook_delivery',
        lambda event_id: enqueued.append(event_id) or True,
    )
    assert deliver_due_webhooks_task() >= 1
    assert result.webhook_event_id in enqueued

    async def verify_not_repeated():
        engine = create_async_engine(_database_url())
        try:
            async with AsyncSession(engine) as db:
                stored = await db.get(Deposit, deposit_id)
                event = await db.get(WebhookEvent, result.webhook_event_id)
                assert stored.status == DepositStatus.failed.value
                assert event.status == 'queued'
                assert await db.scalar(
                    select(func.count(WebhookEvent.id)).where(
                        WebhookEvent.merchant_id == merchant_id,
                        WebhookEvent.event_type == 'deposit.failed',
                    )
                ) == 1
                assert await db.scalar(
                    select(func.count(LedgerEntry.id)).where(
                        LedgerEntry.operation_id == deposit_id,
                        LedgerEntry.entry_type == 'credit',
                    )
                ) == 0
        finally:
            await engine.dispose()

    asyncio.run(verify_not_repeated())


def _csrf_from_html(response: httpx.Response) -> str:
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        response.text,
    )
    assert match, response.text[:500]
    return match.group(1)


def test_postgres_superadmin_rolling_funding_requires_real_2fa_session_and_csrf():
    async def scenario():
        engine = create_async_engine(_database_url())
        password = f'Test-password-{uuid.uuid4().hex}'
        otp_secret = pyotp.random_base32()
        suffix = uuid.uuid4().hex
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                owner = User(
                    email=f'web-merchant-{suffix}@example.test',
                    password_hash=hash_password(password),
                    role=Role.merchant.value,
                )
                actor = User(
                    email=f'web-superadmin-{suffix}@example.test',
                    password_hash=hash_password(password),
                    role=Role.superadmin.value,
                    twofa_enabled=True,
                    twofa_secret=otp_secret,
                )
                setup.add_all([owner, actor])
                await setup.flush()
                merchant = Merchant(
                    owner_id=owner.id,
                    name=f'Web Rolling {suffix}',
                )
                setup.add(merchant)
                await setup.flush()
                await set_merchant_settlement_mode(
                    setup,
                    merchant_id=merchant.id,
                    mode='rolling',
                    actor_id=actor.id,
                )
                await setup.commit()
                ids = merchant.id, actor.id

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                login_page = await client.get('/staff/login')
                assert login_page.status_code == 200
                login_csrf = _csrf_from_html(login_page)
                valid_otp = pyotp.TOTP(otp_secret).now()
                invalid_otp = (
                    '111111' if valid_otp == '000000' else '000000'
                )

                invalid_login = await client.post(
                    '/staff/login',
                    data={
                        'csrf_token': login_csrf,
                        'email': actor.email,
                        'password': password,
                        'otp': invalid_otp,
                    },
                    headers={'Accept': 'text/html'},
                )
                assert invalid_login.status_code == 401
                assert 'processing_staff_session' not in client.cookies

                login = await client.post(
                    '/staff/login',
                    data={
                        'csrf_token': login_csrf,
                        'email': actor.email,
                        'password': password,
                        'otp': valid_otp,
                    },
                    headers={'Accept': 'text/html'},
                )
                assert login.status_code == 303
                assert login.headers['location'] == '/staff/cabinet'
                assert 'processing_staff_session' in client.cookies

                cabinet = await client.get('/staff/cabinet?section=rolling')
                assert cabinet.status_code == 200
                staff_csrf = client.cookies.get('processing_staff_csrf')
                assert staff_csrf

                funding_url = (
                    f'/staff/cabinet/rolling/merchants/{ids[0]}/funding'
                )
                funding_data = {
                    'amount_usdt': '12.345678',
                    'network': 'TRC20',
                    'destination_address': 'T-web-test-address',
                    'tx_hash': f'web-test-{suffix}',
                    'funded_at': datetime.now(timezone.utc).isoformat(),
                    'reason': 'real session csrf route test',
                }
                missing_csrf = await client.post(
                    funding_url,
                    data=funding_data,
                )
                assert missing_csrf.status_code == 403

                funded = await client.post(
                    funding_url,
                    data={**funding_data, 'csrf_token': staff_csrf},
                )
                assert funded.status_code == 303
                repeated = await client.post(
                    funding_url,
                    data={**funding_data, 'csrf_token': staff_csrf},
                )
                assert repeated.status_code == 303

            async with AsyncSession(engine) as verify:
                account = (await verify.execute(
                    select(MerchantRollingAccount).where(
                        MerchantRollingAccount.merchant_id == ids[0]
                    )
                )).scalar_one()
                assert account.principal_usdt == Decimal('12.345678')
                assert account.outstanding_usdt == Decimal('12.345678')
                assert await verify.scalar(
                    select(func.count(MerchantRollingLedgerEntry.id)).where(
                        MerchantRollingLedgerEntry.merchant_id == ids[0],
                        MerchantRollingLedgerEntry.entry_type == 'funding',
                    )
                ) == 1
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_trader_session_cannot_decline_deposit():
    async def scenario():
        engine = create_async_engine(_database_url())
        password = f'Test-password-{uuid.uuid4().hex}'
        suffix = uuid.uuid4().hex
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, _ = await _merchant_fixture(setup, rolling=False)
                trader = User(
                    email=f'web-trader-{suffix}@example.test',
                    password_hash=hash_password(password),
                    role=Role.trader.value,
                )
                setup.add(trader)
                await setup.flush()
                requisite = Requisite(
                    trader_id=trader.id,
                    owner_name='Web Trader',
                    method='sbp',
                    value_encrypted='web-test-requisite',
                )
                setup.add(requisite)
                await setup.flush()
                deposit, _, _ = await _deposit_fixture(
                    setup,
                    merchant,
                    create_allocation=False,
                )
                deposit.requisites_id = requisite.id
                await setup.commit()
                ids = trader.id, deposit.id

            transport = httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url='https://localhost',
                follow_redirects=False,
            ) as client:
                login_page = await client.get('/trader/login')
                assert login_page.status_code == 200
                login = await client.post(
                    '/trader/login',
                    data={
                        'csrf_token': _csrf_from_html(login_page),
                        'email': trader.email,
                        'password': password,
                    },
                    headers={'Accept': 'text/html'},
                )
                assert login.status_code == 303
                cabinet = await client.get('/trader/cabinet')
                assert cabinet.status_code == 200
                trader_csrf = client.cookies.get('processing_trader_csrf')
                assert trader_csrf
                decline = await client.post(
                    f'/trader/cabinet/deposits/{ids[1]}/decline',
                    data={
                        'csrf_token': trader_csrf,
                        'reason': 'must be forbidden',
                    },
                )
                assert decline.status_code == 403

            async with AsyncSession(engine) as verify:
                stored = await verify.get(Deposit, ids[1])
                assert stored.status == DepositStatus.pending.value
        finally:
            await application_engine.dispose()
            await engine.dispose()

    asyncio.run(scenario())


def test_postgres_outbox_releases_db_lock_during_http_and_preserves_newer_claim(
    monkeypatch,
):
    monkeypatch.setattr(
        'app.services.webhooks.validate_public_webhook_url',
        lambda url: url,
    )

    async def scenario():
        engine = create_async_engine(_database_url())
        http_started = asyncio.Event()
        http_release = asyncio.Event()

        async def delayed_http(url, body, headers, timeout_seconds):
            del url, body, headers, timeout_seconds
            http_started.set()
            await http_release.wait()
            return 204

        monkeypatch.setattr(
            'app.services.webhooks._post_validated_webhook',
            delayed_http,
        )
        try:
            async with AsyncSession(engine, expire_on_commit=False) as setup:
                merchant, _ = await _merchant_fixture(setup, rolling=False)
                merchant.webhook_url = 'https://webhook.example.test/events'
                setup.add(ApiKey(
                    merchant_id=merchant.id,
                    api_key=f'test-key-{uuid.uuid4().hex}',
                    secret_hash='test-only-webhook-secret',
                    mode='sandbox',
                ))
                event = WebhookEvent(
                    merchant_id=merchant.id,
                    event_type='deposit.failed',
                    payload={'id': str(uuid.uuid4())},
                )
                setup.add(event)
                await setup.commit()
                event_id = event.id

            async with AsyncSession(engine, expire_on_commit=False) as delivery:
                task = asyncio.create_task(deliver_event(delivery, event_id))
                await asyncio.wait_for(
                    http_started.wait(),
                    timeout=CONCURRENCY_TIMEOUT_SECONDS,
                )

                async with AsyncSession(engine, expire_on_commit=False) as reclaim:
                    locked = await asyncio.wait_for(
                        reclaim.execute(
                            select(WebhookEvent)
                            .where(WebhookEvent.id == event_id)
                            .with_for_update()
                        ),
                        timeout=1,
                    )
                    reclaimed = locked.scalar_one()
                    assert reclaimed.status == 'delivering'
                    assert reclaimed.attempts == 1
                    reclaimed.attempts = 2
                    reclaimed.next_retry_at = (
                        datetime.now(timezone.utc) + timedelta(minutes=2)
                    )
                    await reclaim.commit()

                http_release.set()
                result = await asyncio.wait_for(
                    task,
                    timeout=CONCURRENCY_TIMEOUT_SECONDS,
                )
                assert result.status == 'delivering'
                assert result.attempts == 2
                await delivery.commit()

            async with AsyncSession(engine) as verify:
                stored = await verify.get(WebhookEvent, event_id)
                assert stored.status == 'delivering'
                assert stored.attempts == 2
                attempt = (await verify.execute(
                    select(WebhookDeliveryAttempt).where(
                        WebhookDeliveryAttempt.webhook_event_id == event_id,
                        WebhookDeliveryAttempt.attempt_no == 1,
                    )
                )).scalar_one()
                assert attempt.status == 'pending'
        finally:
            await engine.dispose()

    asyncio.run(scenario())
