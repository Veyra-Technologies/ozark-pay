"""TJS currency policy for the existing Deposit engine; no production admission.

There is deliberately no fixture or environment switch here. An isolated QA
harness must inject a quote provider into its database dependency. Both the
application and PostgreSQL independently restrict execution to named QA databases.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.core.tajikistan import PARSER_ID
from app.models import FeeRule, LedgerEntry, OperationFeeSnapshot, ParserConfiguration, PlatformLedgerEntry, RateObservation, Requisite, User
from app.services.crossborder_money import CrossBorderError, base_amount, number, usdt
from app.services.country_tariffs import public_plan, tier_for
from app.services.fee_tiers import FeeConfigurationError, fee_amount
from app.services.tj_cabinets import personal_plan
from app.services.trader_finance import move_usdt_wallet


async def require_execution(db):
    provider = db.info.get('local_financial_quote_provider')
    if settings.ENV != 'test' or not callable(provider):
        raise CrossBorderError('tj_contract_unconfirmed')
    database, role = (await db.execute(text('SELECT current_database(),current_user'))).one()
    if not database.startswith('ozark_tj_wave3_') or role != 'ozark_tj_test':
        raise CrossBorderError('tj_contract_unconfirmed')
    await db.execute(text("SELECT set_config('ozark.tj_isolated_qa','on',true)"))
    return provider


async def quote(db):
    provider = await require_execution(db)
    value = copy.deepcopy(await provider())
    # The fixture is supplied by the external QA harness, never by a request body.
    if not isinstance(value, dict) or value.get('synthetic_qa') is not True:
        raise CrossBorderError('QA requires explicitly synthetic provenance')
    if (value.get('source') != PARSER_ID or value.get('pair') != 'USDT/TJS'
            or value.get('unit') != 'TJS per 1 USDT' or value.get('validation_status') != 'verified'
            or value.get('positions') != [1, 2, 3] or value.get('side') not in ('0', '1')
            or value.get('request_side') not in ('0', '1') or not value.get('contract_reference')):
        raise CrossBorderError('invalid Bybit quote dimensions or contract')
    try:
        UUID(str(value['observation_id']))
        UUID(str(value['configuration_id']))
        check(type(value['configuration_version']) is int and value['configuration_version'] > 0)
        ids, prices = value['ad_ids'], value['raw_values']
        check(len(ids) == len(prices) == len(set(ids)) == 3 and all(isinstance(i, str) and 0 < len(i) <= 128 for i in ids))
        prices = [number(p) for p in prices]
        check(all(p > 0 for p in prices))
        rate = number(value['rate'])
        check(rate > 0 and rate == (sum(prices) / 3).quantize(Decimal('.000000000001')))
        start, end = (datetime.fromisoformat(value[name]) for name in ('fetched_at', 'expires_at'))
        check(start.tzinfo and end.tzinfo and start <= datetime.now(timezone.utc) < end)
        check(0 < (end-start).total_seconds() <= 120)
    except (KeyError, TypeError, ValueError, AssertionError) as exc:
        raise CrossBorderError('invalid or expired Bybit quote') from exc
    config = await db.get(ParserConfiguration, UUID(value['configuration_id']))
    if (not config or config.parser_id != PARSER_ID or config.version != value['configuration_version']
            or config.country != 'TJ' or config.direction != 'tj-tj'):
        raise CrossBorderError('quote configuration mismatch')
    identity = UUID(value['observation_id'])
    fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    await db.execute(insert(RateObservation).values(id=identity, configuration_id=config.id,
        fingerprint=fingerprint, snapshot=value, fetched_at=start, fixed_at=datetime.now(timezone.utc),
        expires_at=end).on_conflict_do_nothing(index_elements=['id']))
    persisted = await db.get(RateObservation, identity)
    if persisted.snapshot != value:
        raise CrossBorderError('fixed observation identity mismatch')
    return value


def check(value):
    if not value:
        raise ValueError('invalid financial proof')


async def personal_rule(db, *, party, entity_id, amount):
    """Materialize an approved immutable plan tier for the existing FeeSnapshot FK.

    Inactive adapter rows never participate in the legacy default-rate resolver.
    The active, date-effective personal TariffPlan remains the sole TJ authority.
    """
    plan = await personal_plan(db, party=party, entity_id=entity_id)
    if not plan:
        raise FeeConfigurationError('active individual TJ tariff is required')
    tier = tier_for(plan.tiers, amount, basis='local_amount')
    identity = uuid5(plan.id, str(tier['min']))
    values = dict(id=identity, merchant_id=entity_id if party == 'merchant' else None,
        method='c2c', percent=Decimal(tier['percent']), fixed=0, entity_type=party, entity_id=entity_id,
        fee_side='merchant_fee' if party == 'merchant' else 'executor_fee', payment_method='c2c',
        currency='TJS', direction=None, min_amount=Decimal(tier['min']),
        max_amount=Decimal(tier['max']) if tier['max'] else None, rate_percent=Decimal(tier['percent']),
        effective_from=plan.effective_from, effective_to=None, is_active=False,
        version=plan.version, created_by=plan.created_by, country_tariff_plan_id=plan.id)
    await db.execute(insert(FeeRule).values(**values).on_conflict_do_nothing(index_elements=['id']))
    rule = await db.get(FeeRule, identity)
    if (rule.country_tariff_plan_id != plan.id or rule.entity_id != entity_id
            or rule.rate_percent != Decimal(tier['percent']) or rule.currency != 'TJS'):
        raise FeeConfigurationError('personal tariff materialization mismatch')
    return rule, plan


def financial_context(*, amount, merchant_rule, trader_rule, merchant_plan, trader_plan, fixed_quote):
    rate = Decimal(fixed_quote['rate'])
    merchant_fee = fee_amount(amount, merchant_rule.rate_percent)
    trader_fee = fee_amount(amount, trader_rule.rate_percent)
    debit = base_amount(amount-trader_fee, rate)
    payable = base_amount(amount-merchant_fee, rate)
    if merchant_rule.rate_percent < trader_rule.rate_percent or debit < payable:
        raise FeeConfigurationError('negative platform margin')
    return {'format_version': 1, 'country': 'TJ', 'local_currency': 'TJS',
        'accounting_currency': 'USDT', 'quote': copy.deepcopy(fixed_quote),
        'merchant_plan': public_plan(merchant_plan), 'trader_plan': public_plan(trader_plan),
        'amount_tjs': str(amount), 'base_usdt': str(base_amount(amount, rate)),
        'merchant_fee_tjs': str(merchant_fee), 'trader_fee_tjs': str(trader_fee),
        'merchant_fee_usdt': str(usdt(merchant_fee/rate)), 'trader_fee_usdt': str(usdt(trader_fee/rate)),
        'reserve_usdt': str(debit), 'debit_usdt': str(debit),
        'merchant_payable_usdt': str(payable), 'platform_margin_usdt': str(usdt(debit-payable)),
        'merchant_rounding_delta_usdt': str(usdt(base_amount(amount,rate)-payable-usdt(merchant_fee/rate))),
        'trader_rounding_delta_usdt': str(usdt(base_amount(amount,rate)-debit-usdt(trader_fee/rate))),
        'rounding': 'TJS 0.01 / USDT 0.00000001 ROUND_HALF_UP; margin is debit minus credit'}


async def snapshot(db, deposit):
    await require_execution(db)
    row = await db.scalar(select(OperationFeeSnapshot).where(OperationFeeSnapshot.deposit_id == deposit.id))
    if (not row or row.currency != 'TJS' or row.merchant_id != deposit.merchant_id
            or not row.financial_context or row.financial_context.get('country') != 'TJ'):
        raise CrossBorderError('immutable TJ financial snapshot is missing')
    return row


async def trader_movement(db, deposit, kind, *, available=False):
    row = await snapshot(db, deposit)
    if row.executor_type == 'aggregator':
        from app.services.aggregator_execution import executor_movement
        return await executor_movement(db, deposit, row, kind, available=available)
    req = await db.get(Requisite, deposit.requisites_id)
    trader = await db.get(User, row.executor_id)
    if (not req or not trader or row.executor_type != 'trader' or req.trader_id != trader.id
            or req.country != 'TJ' or req.currency != 'TJS' or trader.working_country != 'TJ'):
        raise CrossBorderError('TJ settlement owner/currency mismatch')
    context = row.financial_context
    movement = 'debit_available' if available and kind == 'debit' else kind
    await move_usdt_wallet(db, trader.id, kind=movement, amount=Decimal(context['reserve_usdt']),
        operation_id=deposit.id, key=f'local-deposit:{deposit.id}:{kind}', description='TJ '+kind)
    metadata = dict(deposit.metadata_json or {})
    metadata.update(trader_hold_status='released' if kind == 'release' else 'settled',
        trader_hold_released=kind == 'release', trader_settled=kind == 'debit')
    deposit.metadata_json = metadata
    return trader


async def validate_confirmation(db, deposit, actor_id, evidence):
    row = await snapshot(db, deposit)
    if row.executor_type == 'aggregator':
        from app.services.aggregator_execution import verified_event_for
        if actor_id is not None or not await verified_event_for(db, deposit, 'paid'):
            raise CrossBorderError('verified provider payment evidence is required')
        return
    actor = await db.get(User, actor_id) if actor_id else None
    if (not actor or not actor.is_active or actor.is_locked or actor.is_archived
            or not (actor.role in ('admin', 'superadmin') or (actor.role == 'trader' and actor.id == row.executor_id))):
        raise CrossBorderError('confirmation actor is not authorised')
    from app.models import Appeal
    if await db.scalar(select(Appeal.id).where(Appeal.operation_id == deposit.id,
            Appeal.status.in_(('opened', 'in_review'))).limit(1)):
        raise CrossBorderError('resolve the active appeal before direct confirmation')
    evidence = evidence or {}
    try:
        received = datetime.fromisoformat(str(evidence['received_at']))
        check(received.tzinfo and deposit.created_at <= received <= datetime.now(timezone.utc))
        check(number(evidence['amount']) == deposit.amount)
        check(str(evidence['requisite_id']) == str(deposit.requisites_id))
        check(evidence.get('bank_credit_confirmed') is True)
        check(4 <= len(str(evidence['reference']).strip()) <= 200)
    except (KeyError, ValueError, TypeError, AssertionError) as exc:
        raise CrossBorderError('check amount, requisite, bank receipt time and confirmation reference') from exc
    deposit.metadata_json = {**(deposit.metadata_json or {}), 'local_confirmation': {
        'amount': str(deposit.amount), 'received_at': received.isoformat(),
        'requisite_id': str(deposit.requisites_id), 'reference': str(evidence['reference']).strip(),
        'bank_credit_confirmed': True, 'actor_id': str(actor.id), 'source': 'manual_bank_statement_review'}}


async def settle_credit(db, deposit, fee_snapshot, description):
    from app.services.ledger import credit, record_fee, record_platform_executor_fee, record_platform_income
    context = (await snapshot(db, deposit)).financial_context
    if fee_snapshot.settlement_status != 'pending':
        raise CrossBorderError('local settlement already applied')
    payable = Decimal(context['merchant_payable_usdt'])
    margin = Decimal(context['platform_margin_usdt'])
    from app.services.rolling import apply_merchant_financing
    financing = await apply_merchant_financing(db, deposit=deposit, snapshot=fee_snapshot,
        merchant_payable_rub=deposit.amount-fee_snapshot.merchant_fee_amount, description=description)
    await record_fee(db, deposit.merchant_id, fee_snapshot.merchant_fee_amount, deposit.id,
        f'local-deposit:{deposit.id}:merchant-fee', description, currency='TJS')
    await record_platform_executor_fee(db, deposit.merchant_id, fee_snapshot.executor_fee_amount,
        deposit.id, f'local-deposit:{deposit.id}:executor-fee', description, currency='TJS')
    await record_platform_income(db, deposit.merchant_id, margin, deposit.id,
        f'local-deposit:{deposit.id}:margin', description, currency='USDT')
    await db.flush()
    fee_snapshot.settlement_status = 'settled'
    fee_snapshot.settled_at = datetime.now(timezone.utc)
    fee_snapshot.ledger_reference = await db.scalar(select(LedgerEntry.id).where(
        LedgerEntry.operation_id == deposit.id, LedgerEntry.merchant_id == deposit.merchant_id,
        LedgerEntry.currency == 'USDT', LedgerEntry.entry_type == 'credit'))
    public = {key:context[key] for key in ('local_currency','accounting_currency','amount_tjs',
        'base_usdt','merchant_fee_tjs','merchant_fee_usdt','merchant_payable_usdt')}
    deposit.metadata_json = {**(deposit.metadata_json or {}), 'local_financial_result': public,
        'rolling_financing': {key:str(value) if isinstance(value, Decimal) else value
                             for key,value in financing.items() if key != 'quote'},
        'merchant_fee_amount': str(fee_snapshot.merchant_fee_amount),
        'merchant_payable_amount': str(deposit.amount-fee_snapshot.merchant_fee_amount),
        'merchant_payable_currency': 'TJS', 'accounting_currency': 'USDT'}
    await db.flush()
    return public


async def reverse_credit(db, deposit, *, actor, reason, reference):
    """Accounting inverse only, called by the locked Deposit refund transition."""
    from app.services.ledger import debit_available, LedgerError
    row = await snapshot(db, deposit)
    if (actor.role not in ('admin', 'superadmin') or not actor.is_active or actor.is_locked
            or actor.is_archived or not 4 <= len(reason.strip()) <= 400
            or not 4 <= len(reference.strip()) <= 200):
        raise CrossBorderError('authorised refund review, reason and reference required')
    if deposit.status == 'refunded':
        previous = (deposit.metadata_json or {}).get('local_refund', {})
        if previous.get('reason') != reason or previous.get('reference') != reference:
            raise CrossBorderError('refund identity conflict')
        return False
    if deposit.status != 'paid' or row.settlement_status != 'settled':
        raise CrossBorderError('only a paid deposit can be refunded')
    from app.models import Appeal
    if await db.scalar(select(Appeal.id).where(Appeal.operation_id == deposit.id,
            Appeal.status.in_(('opened', 'in_review'))).limit(1)):
        raise CrossBorderError('resolve the open appeal before refund')
    context = row.financial_context
    from app.services.trader_finance import get_usdt_wallet
    # Same order as confirmation: Deposit -> trader wallet -> merchant balance.
    if row.executor_type == 'aggregator':
        from app.services.aggregator_execution import wallet
        await wallet(db, row.executor_id, lock=True)
    else:
        await get_usdt_wallet(db, row.executor_id, for_update=True)
    try:
        from app.services.rolling import reverse_paid_allocation
        allocation = await reverse_paid_allocation(db, deposit=deposit, actor_id=actor.id, reason=reason)
        if not allocation:
            await debit_available(db, deposit.merchant_id, Decimal(context['merchant_payable_usdt']),
                deposit.id, f'local-deposit:{deposit.id}:merchant-refund', reason, currency='USDT', entry_type='local_refund')
    except LedgerError as exc:
        raise CrossBorderError('Merchant USDT assets are unavailable for an accounting refund') from exc
    movement = move_usdt_wallet
    if row.executor_type == 'aggregator':
        from app.services.aggregator_execution import move_wallet
        movement = move_wallet
    await movement(db, row.executor_id, kind='credit', amount=Decimal(context['debit_usdt']),
        operation_id=deposit.id, key=f'local-deposit:{deposit.id}:trader-refund', description=reason)
    if row.executor_type == 'aggregator':
        from app.models import AggregatorExecution
        execution = await db.scalar(select(AggregatorExecution).where(AggregatorExecution.deposit_id == deposit.id))
        execution.state = 'refunded'
    db.add(PlatformLedgerEntry(merchant_id=deposit.merchant_id, operation_id=deposit.id,
        entry_type='local_refund', amount=-Decimal(context['platform_margin_usdt']), currency='USDT',
        idempotency_key=f'local-deposit:{deposit.id}:margin-refund', description=reason[:500]))
    db.add(LedgerEntry(merchant_id=deposit.merchant_id, operation_id=deposit.id,
        entry_type='local_fee_reversal', amount=-row.merchant_fee_amount, currency='TJS',
        idempotency_key=f'local-deposit:{deposit.id}:fee-refund', description=reason[:500]))
    row.settlement_status = 'reversed'
    deposit.status = 'refunded'
    deposit.metadata_json = {**(deposit.metadata_json or {}), 'local_refund': {
        'reason': reason, 'reference': reference, 'actor_id': str(actor.id),
        'accounting_only': True, 'at': datetime.now(timezone.utc).isoformat()}}
    return True
