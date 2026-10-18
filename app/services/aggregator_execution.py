"""Independent external executors using Deposit, snapshots and local settlement.

No production adapter is registered. All executable paths require the existing
multi-layer isolated QA admission plus an externally injected adapter factory.
"""
import asyncio
import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pyotp
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlsplit

from app.core.security import decrypt_secret, encrypt_secret
from app.models import (AggregatorAccount, AggregatorExecution, AggregatorIntegration,
    AggregatorIntegrationVersion, AggregatorProviderEvent, AggregatorWallet,
    AggregatorWalletEntry, Deposit, Merchant, OperationFeeSnapshot, TariffPlan)
from app.services import local_deposit_policy as local
from app.services.aggregator_adapters import (AggregatorAdapter, PaymentRequest,
    ProviderError, ProviderOrderAbsent, ProviderRequestNotSent, ProviderResult, VerifiedWebhook,
    public_addresses, validate_base_url)
from app.services.audit import audit
from app.services.country_tariffs import public_plan, require_admin, scope_lock
from app.services.fee_tiers import create_operation_fee_snapshot
from app.services.wallet_movements import collateral_delta


CREDENTIAL_FIELDS = ('api_key', 'secret_key', 'webhook_id', 'webhook_secret')
TERMINAL_STATES = ('paid', 'cancelled', 'refunded')


def now():
    return datetime.now(timezone.utc)


def require_fresh_otp(actor, otp):
    require_admin(actor)
    if (not actor.twofa_enabled or not actor.twofa_secret or not otp
            or not pyotp.TOTP(decrypt_secret(actor.twofa_secret)).verify(str(otp), valid_window=1)):
        raise ProviderError('staff_2fa_required')


async def integration_version(db, integration, *, version_id=None):
    statement = select(AggregatorIntegrationVersion).where(
        AggregatorIntegrationVersion.integration_id == integration.id)
    statement = statement.where(AggregatorIntegrationVersion.id == version_id) if version_id else statement.where(
        AggregatorIntegrationVersion.version == integration.active_version)
    row = await db.scalar(statement)
    if not row:
        raise ProviderError('integration_version_missing')
    return row


async def adapter_for(db, version):
    await local.require_execution(db)
    factory = db.info.get('external_aggregator_adapter_factory')
    if (version.environment != 'sandbox' or version.retired_at or version.country != 'TJ'
            or version.currency != 'TJS' or not callable(factory)):
        raise ProviderError('provider_contract_unconfirmed')
    adapter = factory(version.adapter_id, version)
    if not isinstance(adapter, AggregatorAdapter):
        raise ProviderError('provider_contract_unconfirmed')
    return adapter


def credentials(version):
    value = json.loads(decrypt_secret(version.credentials_encrypted))
    if set(value) != set(CREDENTIAL_FIELDS) or not all(isinstance(v, str) and v for v in value.values()):
        raise ProviderError('credentials_unavailable')
    return value


async def save_integration(db, *, actor, account, form, otp):
    require_fresh_otp(actor, otp)
    if account.working_country != 'TJ' or account.working_currency != 'TJS' or account.is_archived:
        raise ProviderError('external_aggregator_requires_tj_cabinet')
    await scope_lock(db, f'aggregator-integration:{account.id}')
    integration = await db.scalar(select(AggregatorIntegration).where(
        AggregatorIntegration.aggregator_id == account.id).with_for_update())
    previous = await integration_version(db, integration) if integration else None
    if integration and await db.scalar(select(AggregatorExecution.id).join(
            AggregatorIntegrationVersion, AggregatorExecution.integration_version_id == AggregatorIntegrationVersion.id
        ).where(AggregatorIntegrationVersion.integration_id == integration.id,
                AggregatorExecution.state.notin_(TERMINAL_STATES)).limit(1)):
        raise ProviderError('rotation_requires_resolving_outstanding_orders')
    values = credentials(previous) if previous else {}
    for field in CREDENTIAL_FIELDS:
        value = str(form.get(field) or '').strip()
        if value:
            if len(value) > 2048 or any(ord(c) < 32 for c in value):
                raise ProviderError('invalid_credential')
            values[field] = value
    if set(values) != set(CREDENTIAL_FIELDS):
        raise ProviderError('four_credentials_required')
    base_url = validate_base_url(str(form.get('base_url') or ''))
    if any(value in base_url for value in values.values()):
        raise ProviderError('credentials_cannot_appear_in_url')
    # QA factories never open a socket. Real addresses must resolve publicly even
    # for storage; a future adapter must independently pin DNS for each send.
    if not callable(db.info.get('external_aggregator_adapter_factory')):
        await asyncio.wait_for(asyncio.to_thread(public_addresses, urlsplit(base_url).hostname), 5)
    merchant_ids = sorted({str(UUID(str(v))) for v in form.get('merchant_ids', [])})
    for identity in merchant_ids:
        merchant = await db.get(Merchant, UUID(identity))
        if not merchant or merchant.working_country != 'TJ' or merchant.is_archived:
            raise ProviderError('merchant_country_mismatch')
    allowed_ips = [str(ipaddress.ip_network(value, strict=True)) for value in form.get('allowed_ips', [])]
    environment = str(form.get('environment') or 'sandbox')
    adapter_id = str(form.get('adapter_id') or '').strip()
    api_version = str(form.get('api_version') or '').strip()
    if environment not in ('sandbox', 'production') or not 1 <= len(adapter_id) <= 80 or not 1 <= len(api_version) <= 80:
        raise ProviderError('invalid_integration_configuration')
    if not integration:
        integration = AggregatorIntegration(aggregator_id=account.id, active_version=1,
            status='configured', routing_enabled=False)
        db.add(integration)
        await db.flush()
    else:
        integration.active_version += 1
        previous.retired_at = now()
    version = AggregatorIntegrationVersion(integration_id=integration.id, version=integration.active_version,
        country='TJ', currency='TJS', base_url=base_url, adapter_id=adapter_id, api_version=api_version,
        environment=environment, credentials_encrypted=encrypt_secret(json.dumps(values)),
        merchant_ids=merchant_ids, allowed_methods=['c2c'], allowed_ips=allowed_ips, created_by=actor.id)
    db.add(version)
    for currency in ('TJS', 'USDT'):
        await db.execute(insert(AggregatorWallet).values(aggregator_id=account.id, currency=currency,
            balance=0, hold=0).on_conflict_do_nothing(constraint='uq_aggregator_wallet_currency'))
    integration.status = 'awaiting_verification'
    integration.routing_enabled = False
    integration.last_error = None
    await audit(db, 'aggregator_integration_version_created', 'aggregator', actor.id, account.id,
        details={'version': integration.active_version, 'changed_fields': [key for key in CREDENTIAL_FIELDS if form.get(key)],
                 'credentials': 'encrypted; values excluded'})
    await db.flush()
    return integration


async def verify_integration(db, *, integration):
    version = await integration_version(db, integration)
    integration.last_checked_at = now()
    try:
        adapter = await adapter_for(db, version)
        verified, code = await asyncio.wait_for(adapter.verify_connection(credentials(version)), 15)
    except (ValueError, asyncio.TimeoutError):
        verified, code = False, 'verification_not_supported'
    integration.status = 'connected' if verified else 'connection_error'
    integration.last_error = None if verified else code if code == 'verification_not_supported' else 'verification_failed'
    integration.routing_enabled = False
    return verified


async def wallet(db, aggregator_id, *, lock=False):
    query = select(AggregatorWallet).where(AggregatorWallet.aggregator_id == aggregator_id,
                                          AggregatorWallet.currency == 'USDT')
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    value = await db.scalar(query)
    if not value:
        raise ProviderError('aggregator_usdt_wallet_missing')
    return value


async def move_wallet(db, aggregator_id, *, kind, amount, operation_id, key, description):
    await local.require_execution(db)
    account = await db.get(AggregatorAccount, aggregator_id)
    if not account or account.working_country != 'TJ' or account.working_currency != 'TJS':
        raise ProviderError('aggregator_wallet_country_mismatch')
    target = await wallet(db, aggregator_id, lock=True)
    existing = await db.scalar(select(AggregatorWalletEntry).where(AggregatorWalletEntry.idempotency_key == key))
    if existing:
        if (existing.wallet_id, existing.operation_id, existing.entry_type, existing.amount) != (target.id, operation_id, kind, amount):
            raise ProviderError('aggregator_wallet_idempotency_conflict')
        return existing
    target.balance, target.hold = collateral_delta(target.balance, target.hold, kind, amount)
    entry = AggregatorWalletEntry(wallet_id=target.id, aggregator_id=aggregator_id, operation_id=operation_id,
        entry_type=kind, amount=amount, currency='USDT', balance_after=target.balance, hold_after=target.hold,
        idempotency_key=key, description=description[:500])
    db.add(entry)
    await db.flush()
    return entry


async def executor_movement(db, deposit, row, kind, *, available=False):
    if (row.executor_type != 'aggregator' or deposit.requisites_id is not None
            or deposit.financial_executor_type != 'aggregator' or deposit.financial_executor_id != row.executor_id):
        raise ProviderError('external_executor_identity_mismatch')
    await move_wallet(db, row.executor_id, kind='debit_available' if available and kind == 'debit' else kind,
        amount=Decimal(row.financial_context['reserve_usdt']), operation_id=deposit.id,
        key=f'local-deposit:{deposit.id}:{kind}', description='TJ external aggregator '+kind)
    deposit.metadata_json = {**(deposit.metadata_json or {}), 'trader_hold_status':
        'released' if kind == 'release' else 'settled', 'trader_hold_released': kind == 'release',
        'trader_settled': kind == 'debit'}
    if kind == 'debit':
        execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.deposit_id == deposit.id))
        execution.state = 'paid'
    return None


async def prepare(db, *, merchant, data, aggregator_id, operation_id, idem, fingerprint,
                  merchant_rule, merchant_plan, fixed_quote, created_at, expires_at, client_ip):
    await local.require_execution(db)
    account = await db.get(AggregatorAccount, UUID(str(aggregator_id)))
    if (not account or account.working_country != 'TJ' or account.working_currency != 'TJS'
            or account.status != 'active' or account.is_archived):
        raise ProviderError('aggregator_not_available')
    integration = await db.scalar(select(AggregatorIntegration).where(
        AggregatorIntegration.aggregator_id == account.id).with_for_update())
    if not integration or integration.status != 'connected' or not integration.routing_enabled:
        raise ProviderError('aggregator_integration_not_ready')
    version = await integration_version(db, integration)
    await adapter_for(db, version)
    if (str(merchant.id) not in version.merchant_ids or merchant.working_country != 'TJ'
            or data.currency != 'TJS' or data.method.value != 'c2c' or 'c2c' not in version.allowed_methods):
        raise ProviderError('aggregator_route_scope_mismatch')
    if not max(Decimal(10), account.min_payment_amount) <= data.amount <= account.max_payment_amount:
        raise ProviderError('aggregator_amount_limit')
    await scope_lock(db, f'aggregator-limit:{account.id}')
    for limit, start in ((account.daily_limit, now().replace(hour=0, minute=0, second=0, microsecond=0)),
                         (account.monthly_limit, now().replace(day=1, hour=0, minute=0, second=0, microsecond=0))):
        if limit is not None:
            used = await db.scalar(select(func.coalesce(func.sum(Deposit.amount), 0)).join(
                AggregatorExecution, AggregatorExecution.deposit_id == Deposit.id).where(
                AggregatorExecution.aggregator_id == account.id, Deposit.created_at >= start,
                Deposit.status.notin_(('failed', 'cancelled', 'expired', 'refunded'))))
            if used + data.amount > limit:
                raise ProviderError('aggregator_turnover_limit')
    executor_rule, executor_plan = await local.personal_rule(db, party='aggregator', entity_id=account.id, amount=data.amount)
    context = local.financial_context(amount=data.amount, merchant_rule=merchant_rule, trader_rule=executor_rule,
        merchant_plan=merchant_plan, trader_plan=executor_plan, fixed_quote=fixed_quote)
    context['executor_plan'] = context.pop('trader_plan')
    for key in ('fee_tjs', 'fee_usdt', 'rounding_delta_usdt'):
        context['executor_'+key] = context.pop('trader_'+key)
    context.update(format_version=2, executor_type='aggregator', executor_id=str(account.id),
                   integration_version_id=str(version.id), external_id=str(operation_id))
    deposit = Deposit(id=operation_id, merchant_id=merchant.id, external_id=data.external_id,
        idempotency_key=idem, request_fingerprint=fingerprint, amount=data.amount, currency='TJS', method='c2c',
        status='pending', requisites_id=None, financial_executor_type='aggregator', financial_executor_id=account.id,
        client_ip=client_ip, created_at=created_at, expires_at=expires_at,
        metadata_json={**(data.metadata or {}), 'processing_route': 'external_aggregator'})
    db.add(deposit)
    await db.flush()
    snapshot = await create_operation_fee_snapshot(db, deposit=deposit, merchant_rule=merchant_rule,
        executor_rule=executor_rule, executor_type='aggregator', executor_id=account.id, financial_context=context)
    from app.services.rolling import create_tj_pending_allocation
    await create_tj_pending_allocation(db, deposit=deposit, snapshot=snapshot)
    execution = AggregatorExecution(deposit_id=deposit.id, aggregator_id=account.id,
        integration_version_id=version.id, external_id=str(operation_id), state='prepared', attempts=0)
    db.add(execution)
    await move_wallet(db, account.id, kind='hold', amount=Decimal(context['reserve_usdt']),
        operation_id=deposit.id, key=f'local-deposit:{deposit.id}:hold', description='TJ external aggregator reserve')
    await audit(db, 'external_aggregator_reserved', 'deposit', None, deposit.id,
                details={'aggregator_id': str(account.id), 'integration_version_id': str(version.id)})
    return deposit


async def public_requisites(db, deposit):
    execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.deposit_id == deposit.id))
    if not execution:
        raise ProviderError('external_execution_missing')
    details = json.loads(decrypt_secret(execution.requisites_encrypted)) if execution.requisites_encrypted else None
    return {'route': 'external_aggregator', 'processing_status': execution.state,
            'provider_order_id': execution.provider_order_id, 'requisites': details}


async def independent_reconciliation(db, aggregator_id):
    target = await wallet(db, aggregator_id, lock=True)
    entries = (await db.scalars(select(AggregatorWalletEntry).where(
        AggregatorWalletEntry.aggregator_id == aggregator_id))).all()
    balance = hold = Decimal(0)
    for entry in entries:
        if entry.entry_type not in ('credit', 'hold', 'release', 'debit', 'debit_available') or entry.amount <= 0:
            raise ProviderError('unknown_aggregator_journal_movement')
        if entry.entry_type == 'credit':
            balance += entry.amount
        elif entry.entry_type in ('debit', 'debit_available'):
            balance -= entry.amount
        if entry.entry_type == 'hold':
            hold += entry.amount
        elif entry.entry_type in ('release', 'debit'):
            hold -= entry.amount
    return {'aggregator_id': str(aggregator_id), 'currency': 'USDT', 'journal_balance': str(balance),
            'journal_hold': str(hold), 'balance': str(target.balance), 'hold': str(target.hold),
            'reconciled': balance == target.balance and hold == target.hold}


async def save_executor_tariff(db, *, actor, account, tiers, effective_from, otp):
    require_fresh_otp(actor, otp)
    from app.services.country_tariffs import validate_tiers
    if account.working_country != 'TJ' or account.is_archived or account.status != 'active':
        raise ProviderError('aggregator_tariff_scope_mismatch')
    tiers = validate_tiers(tiers, basis='local_amount')
    if (Decimal(tiers[0]['min']) != 10 or not effective_from.tzinfo
            or (tiers[-1]['max'] is not None and Decimal(tiers[-1]['max']) <= account.max_payment_amount)):
        raise ProviderError('aggregator_tariff_requires_complete_range_from_10')
    key = f'tj-tj:aggregator:{account.id}'
    await scope_lock(db, key)
    previous = await db.scalar(select(TariffPlan).where(TariffPlan.scope_key == key)
        .order_by(TariffPlan.version.desc()).limit(1))
    plan = TariffPlan(scope_key=key, version=previous.version+1 if previous else 1,
        party='aggregator', country='TJ', currency='TJS', basis='local_amount', direction='tj-tj',
        entity_id=account.id, tiers=tiers, reference={'method':'c2c', 'executor':'external_aggregator'},
        effective_from=effective_from, is_active=True, created_by=actor.id)
    db.add(plan)
    await db.flush()
    await audit(db, 'aggregator_executor_tariff_created', 'aggregator', actor.id, account.id,
        details={'plan_id': str(plan.id), 'version': plan.version, 'tiers': tiers})
    return plan


async def own_context(db, account, *, request=None):
    """The provider sees only its obligation, never Merchant rates or margin."""
    integration = await db.scalar(select(AggregatorIntegration).where(AggregatorIntegration.aggregator_id == account.id))
    version = await integration_version(db, integration) if integration else None
    wallets = list(await db.scalars(select(AggregatorWallet).where(AggregatorWallet.aggregator_id == account.id)))
    operation_query = select(Deposit, AggregatorExecution, OperationFeeSnapshot).join(
        AggregatorExecution, AggregatorExecution.deposit_id == Deposit.id).join(
        OperationFeeSnapshot, OperationFeeSnapshot.deposit_id == Deposit.id).where(
        AggregatorExecution.aggregator_id == account.id, Deposit.currency == 'TJS')
    filters = request.query_params if request else {}
    from datetime import timedelta, time as day_time
    from sqlalchemy import Numeric, String, cast, or_
    from urllib.parse import urlencode
    if filters.get('tj_period') in ('1', '7', '30', '90'):
        operation_query = operation_query.where(Deposit.created_at >= now() - timedelta(days=int(filters['tj_period'])))
    for field, end in [('tj_from', False), ('tj_to', True)]:
        if filters.get(field):
            try:
                value = datetime.combine(datetime.strptime(filters[field], '%Y-%m-%d').date(), day_time.min, timezone.utc)
            except ValueError as exc:
                from fastapi import HTTPException
                raise HTTPException(422, 'Дата должна иметь формат ГГГГ-ММ-ДД') from exc
            operation_query = operation_query.where(Deposit.created_at < value + timedelta(days=1)) if end else operation_query.where(Deposit.created_at >= value)
    scoped = operation_query.with_only_columns(Deposit.id, Deposit.status).subquery()
    counts = dict((await db.execute(select(scoped.c.status, func.count()).group_by(scoped.c.status))).all())
    total = sum(counts.values())
    success = counts.get('paid', 0)
    failed = sum(counts.get(s, 0) for s in ('failed', 'cancelled', 'expired', 'refunded'))
    completed = success + failed
    finance = OperationFeeSnapshot.financial_context
    paid = (await db.execute(select(func.coalesce(func.sum(Deposit.amount), 0),
        func.coalesce(func.sum(cast(finance['debit_usdt'].as_string(), Numeric(38, 12))), 0),
        func.coalesce(func.sum(cast(finance['executor_fee_usdt'].as_string(), Numeric(38, 12))), 0))
        .join(OperationFeeSnapshot, OperationFeeSnapshot.deposit_id == Deposit.id)
        .where(Deposit.id.in_(select(scoped.c.id)), Deposit.status == 'paid'))).one()
    statistics = {'total': total, 'successful': success, 'failed': failed, 'active': total - completed,
        'conversion': Decimal(success) * 100 / Decimal(completed) if completed else Decimal(0),
        'turnover_tjs': paid[0], 'settled_usdt': paid[1], 'fee_usdt': paid[2]}
    if filters.get('tj_status') in ('created', 'pending', 'paid', 'failed', 'cancelled', 'expired', 'refunded', 'appeal_opened'):
        operation_query = operation_query.where(Deposit.status == filters['tj_status'])
    search = filters.get('tj_query', '').strip()[:120]
    if search:
        operation_query = operation_query.where(or_(cast(Deposit.id, String).contains(search, autoescape=True),
            Deposit.external_id.contains(search, autoescape=True), AggregatorExecution.provider_order_id.contains(search, autoescape=True)))
    filtered_count = await db.scalar(select(func.count()).select_from(operation_query.subquery()))
    try:
        page = max(1, min(int(filters.get('tj_page', '1')), 1000000))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (filtered_count + 24) // 25)
    page = min(page, pages)
    operations = (await db.execute(operation_query.order_by(Deposit.created_at.desc(), Deposit.id.desc()).offset((page - 1) * 25).limit(25))).all()
    def page_url(number):
        values = {k: v for k, v in filters.items() if k.startswith('tj_') or k == 'section'}
        return '?' + urlencode({**values, 'tj_page': number})
    own_operations = [{'id': str(dep.id), 'amount_tjs':dep.amount, 'status':dep.status,
        'provider_order_id':exe.provider_order_id, 'external_state':exe.state,
        'debit_usdt':snap.financial_context['debit_usdt'], 'reserve_usdt':snap.financial_context['reserve_usdt'],
        'fee_tjs':snap.financial_context['executor_fee_tjs'], 'created_at':dep.created_at} for dep,exe,snap in operations]
    count, turnover = (await db.execute(select(func.count(),func.coalesce(func.sum(Deposit.amount),0)).join(
        AggregatorExecution, AggregatorExecution.deposit_id == Deposit.id).where(
        AggregatorExecution.aggregator_id == account.id, Deposit.status == 'paid'))).one()
    entries = list(await db.scalars(select(AggregatorWalletEntry).where(AggregatorWalletEntry.aggregator_id == account.id)
        .order_by(AggregatorWalletEntry.created_at.desc()).limit(100)))
    plan = await local.personal_plan(db, party='aggregator', entity_id=account.id)
    return {'account':account, 'integration':integration, 'version':version,
        'wallets': {w.currency:{'balance':w.balance,'hold':w.hold,'available':w.balance-w.hold} for w in wallets},
        'operations':own_operations, 'journal':entries, 'plan':public_plan(plan) if plan else None,
        'successful_count':count,'turnover_tjs':turnover, 'statistics': statistics,
        'pagination': {'page': page, 'pages': pages, 'count': filtered_count,
            'previous': page_url(page - 1) if page > 1 else None, 'next': page_url(page + 1) if page < pages else None}}


def payment_request(deposit, execution):
    return PaymentRequest(external_id=execution.external_id, amount=deposit.amount,
        currency='TJS', method='c2c', callback_url=f'/api/v1/external-aggregators/{execution.integration_version_id}/webhook')


async def verified_event_for(db, deposit, status):
    """A request-supplied flag cannot authorize a financial transition."""
    identity = (deposit.metadata_json or {}).get('external_verified_event_id')
    if not identity:
        return None
    event = await db.get(AggregatorProviderEvent, UUID(identity))
    execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.deposit_id == deposit.id))
    if (not event or not execution or event.execution_id != execution.id
            or event.integration_version_id != execution.integration_version_id
            or event.normalized_status != status or event.status != 'verified'):
        return None
    return event


async def apply_result(db, *, execution_id, result, event_id, payload_hash, lease_token=None, create_response=False):
    """Locks Deposit -> execution -> executor wallet -> Merchant. Never performs IO."""
    if not isinstance(result, ProviderResult):
        raise ProviderError('invalid_provider_result')
    initial = await db.get(AggregatorExecution, execution_id)
    if not initial:
        raise ProviderError('unknown_provider_operation')
    deposit = await db.scalar(select(Deposit).where(Deposit.id == initial.deposit_id).with_for_update()
                              .execution_options(populate_existing=True))
    execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.id == execution_id)
                                .with_for_update().execution_options(populate_existing=True))
    if lease_token and execution.lease_token != lease_token:
        return 'stale_worker_result'
    if (not isinstance(result.amount, Decimal) or not result.amount.is_finite()
            or result.external_id != execution.external_id or result.currency != 'TJS' or result.amount != deposit.amount
            or not isinstance(result.provider_order_id, str) or not 1 <= len(result.provider_order_id) <= 160
            or (execution.provider_order_id and execution.provider_order_id != result.provider_order_id)):
        raise ProviderError('provider_operation_scope_mismatch')
    prior = await db.scalar(select(AggregatorProviderEvent).where(
        AggregatorProviderEvent.integration_version_id == execution.integration_version_id,
        AggregatorProviderEvent.event_id == event_id))
    if prior:
        if prior.payload_hash != payload_hash or prior.execution_id != execution.id:
            execution.last_error = 'conflicting_provider_event'
            execution.state = 'manual_review'
            await audit(db, 'external_aggregator_event_conflict', 'deposit', None, deposit.id)
            return 'conflict'
        return 'duplicate'
    version = await db.get(AggregatorIntegrationVersion, execution.integration_version_id)
    integration = await db.get(AggregatorIntegration, version.integration_id)
    if version.retired_at or integration.status == 'suspended':
        raise ProviderError('integration_suspended_or_retired')
    adapter = await adapter_for(db, version)
    status = adapter.normalize_status(result.status)
    if status not in ('pending', 'paid', 'cancelled', 'unknown'):
        raise ProviderError('unsupported_provider_status')
    event = AggregatorProviderEvent(integration_version_id=version.id, execution_id=execution.id,
        event_id=event_id, payload_hash=payload_hash, normalized_status=status,
        no_credit_confirmed=result.no_credit_confirmed, status='verified')
    db.add(event)
    execution.provider_order_id = result.provider_order_id
    if result.requisites is not None:
        details = adapter.normalize_requisites(result.requisites)
        # Adapter output must be a minimal allowlisted payload, never arbitrary HTML/JSON.
        allowed = {'bank_id', 'bank_name', 'card_number', 'bank_account', 'recipient_name', 'method', 'country', 'currency'}
        if (not isinstance(details, dict) or set(details)-allowed or details.get('country') != 'TJ'
                or details.get('currency') != 'TJS' or details.get('method') != 'c2c'
                or details.get('bank_id') not in ('tj-dushanbe-city', 'tj-korti-milli')
                or not (details.get('card_number') or details.get('bank_account'))
                or not all(isinstance(v, str) and 0 < len(v) <= 200 for v in details.values())):
            raise ProviderError('invalid_external_requisites')
        from app.core.ge_bank_requisites import luhn_valid
        card = details.get('card_number')
        account = details.get('bank_account')
        if (bool(card) == bool(account) or (card and (not card.isascii() or not card.isdigit()
                or not 13 <= len(card) <= 19 or len(set(card)) == 1 or not luhn_valid(card)))):
            raise ProviderError('invalid_external_card_or_account')
        serialized = json.dumps(details, sort_keys=True)
        if execution.requisites_encrypted and decrypt_secret(execution.requisites_encrypted) != serialized:
            raise ProviderError('external_requisites_identity_conflict')
        if not execution.requisites_encrypted:
            execution.requisites_encrypted = encrypt_secret(serialized)
    await db.flush()
    if create_response:
        # Even a PAID string in HTTP create response is not payment evidence.
        event.status = 'create_response'
        if execution.state not in TERMINAL_STATES and execution.state != 'manual_review':
            execution.state = 'pending'
    elif deposit.status in ('paid', 'refunded'):
        if (deposit.status, status) not in (('paid', 'paid'), ('refunded', 'paid')):
            execution.last_error = 'contradictory_terminal_status'
            execution.state = 'manual_review'
        event.status = 'observed'
    elif status == 'paid' and result.payment_confirmed:
        if deposit.status not in ('created', 'pending'):
            execution.state = 'manual_review'
            execution.last_error = 'late_payment_requires_staff_appeal'
            event.status = 'observed'
        else:
            deposit.metadata_json = {**(deposit.metadata_json or {}), 'external_verified_event_id': str(event.id)}
            from app.services.deposit_confirmation import confirm_deposit_payment
            applied = await confirm_deposit_payment(db, deposit.id, actor_id=None, actor_ip='',
                audit_action='external_aggregator_paid', description='Verified external aggregator payment', commit=False)
            execution.state = 'paid' if applied.confirmed else 'manual_review'
    elif status == 'cancelled' and result.no_credit_confirmed:
        deposit.metadata_json = {**(deposit.metadata_json or {}), 'external_verified_event_id': str(event.id)}
        from app.services.deposit_lifecycle import finalize_unsuccessful_deposit
        await finalize_unsuccessful_deposit(db, deposit, reason='provider_verified_no_credit',
            target_status='cancelled', actor_role='external_aggregator')
        execution.state = 'cancelled' if deposit.status == 'cancelled' else 'manual_review'
    elif status == 'pending':
        if execution.state not in TERMINAL_STATES and execution.state != 'manual_review':
            execution.state = 'pending'
    else:
        execution.state = 'unknown'
        execution.last_error = 'provider_result_requires_reconciliation'
    integration.last_webhook_at = now() if not lease_token else integration.last_webhook_at
    execution.lease_token = None
    execution.lease_until = None
    execution.next_attempt_at = now()+timedelta(seconds=30) if execution.state == 'pending' else None
    await audit(db, 'external_aggregator_result_recorded', 'deposit', None, deposit.id,
                details={'event_id': str(event.id), 'normalized_status': status, 'state': execution.state})
    return execution.state


async def receive_webhook(db, *, version_id, body, headers, source_ip):
    await local.require_execution(db)
    if len(body) > 65536:
        raise ProviderError('provider_webhook_too_large')
    version = await db.get(AggregatorIntegrationVersion, version_id)
    if not version or version.retired_at:
        raise ProviderError('unknown_provider_integration')
    integration = await db.get(AggregatorIntegration, version.integration_id)
    if integration.status not in ('connected', 'connection_error'):
        raise ProviderError('provider_integration_not_active')
    if version.allowed_ips and not any(ipaddress.ip_address(source_ip) in ipaddress.ip_network(net) for net in version.allowed_ips):
        raise ProviderError('provider_ip_denied')
    adapter = await adapter_for(db, version)
    keys = credentials(version)
    verified = adapter.validate_webhook(body, headers, keys)
    if (not isinstance(verified, VerifiedWebhook) or verified.webhook_id != keys['webhook_id']
            or not isinstance(verified.occurred_at, datetime) or not verified.occurred_at.tzinfo
            or abs((now()-verified.occurred_at).total_seconds()) > 300
            or not 1 <= len(verified.event_id) <= 160):
        raise ProviderError('invalid_provider_webhook')
    execution = await db.scalar(select(AggregatorExecution).where(
        AggregatorExecution.integration_version_id == version.id,
        AggregatorExecution.external_id == verified.result.external_id))
    if not execution:
        raise ProviderError('unknown_provider_operation')
    return await apply_result(db, execution_id=execution.id, result=verified.result,
                              event_id=verified.event_id, payload_hash=hashlib.sha256(body).hexdigest())


async def dispatch_one(session_factory, execution_id):
    """Crash-safe three-stage outbox; no financial lock spans an external call.

    sending with an expired lease is reconciled, never blindly sent again.
    Even adapters claiming idempotent create are not retried without a documented
    authoritative lookup contract. A missing lookup leaves hold intact.
    """
    async with session_factory() as db:
        await local.require_execution(db)
        execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.id == execution_id)
            .with_for_update(skip_locked=True))
        if (not execution or execution.state in TERMINAL_STATES or execution.state == 'manual_review'
                or (execution.lease_until and execution.lease_until > now())
                or (execution.next_attempt_at and execution.next_attempt_at > now())):
            return 'not_claimed'
        version = await db.get(AggregatorIntegrationVersion, execution.integration_version_id)
        integration = await db.get(AggregatorIntegration, version.integration_id)
        if version.retired_at or integration.status == 'suspended':
            return 'suspended'
        adapter = await adapter_for(db, version)
        mode = 'create' if execution.state in ('retry_not_sent','retry_safe') or (execution.state == 'prepared' and execution.attempts == 0) else 'reconcile'
        if mode == 'reconcile' and not execution.provider_order_id and not adapter.supports_status_by_external_id:
            execution.state = 'manual_review'
            execution.last_error = 'safe_provider_lookup_unavailable'
            await db.commit()
            return 'manual_review'
        token = uuid4()
        execution.lease_token = token
        execution.lease_until = now()+timedelta(seconds=45)
        execution.state = 'sending' if mode == 'create' else 'reconciling'
        execution.attempts += 1
        deposit = await db.get(Deposit, execution.deposit_id)
        request, keys, order_id = payment_request(deposit, execution), credentials(version), execution.provider_order_id
        await db.commit()
    try:
        if mode == 'create':
            result = await asyncio.wait_for(adapter.create_payment(request, keys), 20)
        else:
            result = await asyncio.wait_for(adapter.reconcile_payment(request, order_id, keys), 20)
    except Exception as error:
        # Remote text and exception messages may contain credentials or bodies.
        async with session_factory() as db:
            await local.require_execution(db)
            execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.id == execution_id).with_for_update())
            if execution.lease_token == token:
                safe_retry = isinstance(error, ProviderRequestNotSent) or (
                    mode == 'reconcile' and isinstance(error, ProviderOrderAbsent) and adapter.supports_idempotent_create)
                execution.state = ('retry_not_sent' if isinstance(error, ProviderRequestNotSent) else 'retry_safe') if safe_retry else 'unknown'
                if execution.attempts >= 8:
                    execution.state = 'manual_review'
                execution.last_error = 'provider_result_unknown'
                execution.lease_until = None
                execution.lease_token = None
                execution.next_attempt_at = now()+timedelta(seconds=min(300, 2**min(execution.attempts, 8)))
                await db.commit()
        return execution.state
    async with session_factory() as db:
        await local.require_execution(db)
        digest = hashlib.sha256(repr(result).encode()).hexdigest()
        try:
            state = await apply_result(db, execution_id=execution_id, result=result,
                event_id='outbox:'+str(token), payload_hash=digest, lease_token=token, create_response=mode == 'create')
            await db.commit()
            return state
        except (ValueError, ArithmeticError, IntegrityError):
            await db.rollback()
            await local.require_execution(db)
            execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.id == execution_id).with_for_update())
            if execution.lease_token == token:
                execution.state = 'unknown'
                execution.last_error = 'invalid_provider_result_requires_reconciliation'
                execution.lease_token = None
                execution.lease_until = None
                execution.next_attempt_at = now()+timedelta(seconds=30)
                await db.commit()
            return execution.state


async def recover_outbox(session_factory, *, limit=50):
    async with session_factory() as db:
        await local.require_execution(db)
        ids = list(await db.scalars(select(AggregatorExecution.id).where(
            AggregatorExecution.state.in_(('prepared', 'sending', 'unknown', 'pending', 'reconciling','retry_not_sent','retry_safe'))
        ).where(
            or_(AggregatorExecution.next_attempt_at.is_(None), AggregatorExecution.next_attempt_at <= now()),
            or_(AggregatorExecution.lease_until.is_(None), AggregatorExecution.lease_until <= now()),
        ).order_by(AggregatorExecution.next_attempt_at.asc().nullsfirst(), AggregatorExecution.created_at).limit(min(100, limit))))
    return [await dispatch_one(session_factory, identity) for identity in ids]
