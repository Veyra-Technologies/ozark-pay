"""Atomic KZ -> GE accounting; confirmation never changes the fixed KZT base."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select, or_, func
from sqlalchemy.dialects.postgresql import insert

from app.models import (Balance, CrossBorderEvent, CrossBorderOperation, CrossBorderQuote,
    LedgerEntry, Merchant, PlatformLedgerEntry, Requisite, TraderWalletLedgerEntry, User,
    TeamLeadTraderAssignment, TeamLeadMerchantAssignment)
from app.services.audit import audit
from app.services.binance_p2p import PARSER_ID, GEL_PARSER_ID, timestamp
from app.services.country_tariffs import public_plan, resolve_plan, scope_lock, tier_for
from app.services.crossborder_money import CrossBorderError, base_amount, local_amount, result, usdt
from app.services.merchant_countries import require_country, validate_parser
from app.services.rate_registry import require_observation, optional_observation
from app.services.parser_assignments import require_assignment, public_assignment
from app.services.trader_finance import get_usdt_wallet
from app.core.ge_bank_requisites import GE_REQUISITE_METHODS
from app.services.ge_requisite_limits import validate_ge_requisite_limits

STAFF = ('admin', 'superadmin')


def now():
    return datetime.now(timezone.utc)


def active(user):
    return user and user.is_active and not user.is_locked and not user.is_archived


async def merchant_access(db, actor, merchant_id):
    merchant = await db.get(Merchant, merchant_id)
    if not merchant or merchant.is_archived or not active(actor):
        raise CrossBorderError('Кабинет недоступен')
    if actor.role not in STAFF and not (actor.role == 'merchant' and merchant.owner_id == actor.id):
        raise CrossBorderError('Нет доступа к кабинету')
    owner = await db.get(User, merchant.owner_id)
    if not active(owner):
        raise CrossBorderError('Кабинет недоступен')
    return merchant


def scoped_operations(query, actor):
    if not active(actor):
        raise CrossBorderError('Кабинет недоступен')
    if actor.role == 'merchant':
        query = query.join(Merchant, Merchant.id == CrossBorderOperation.merchant_id).where(Merchant.owner_id == actor.id)
    elif actor.role == 'trader':
        query = query.where(CrossBorderOperation.trader_id == actor.id)
    elif actor.role not in STAFF:
        raise CrossBorderError('Нет доступа к трансграну')
    return query


async def visible_operations(db, actor, *, limit=200, offset=0):
    query = scoped_operations(select(CrossBorderOperation), actor)
    return (await db.execute(query.order_by(CrossBorderOperation.created_at.desc(), CrossBorderOperation.id)
                            .limit(limit).offset(offset))).scalars().all()


async def statistics(db, actor):
    # Aggregate the complete authorized scope, independently of list pagination.
    columns = [CrossBorderOperation.status, func.count().label('count')]
    names = ('amount_kzt', 'actual_gel', 'merchant_usdt', 'trader_usdt', 'margin_usdt', 'expenses_usdt')
    columns += [func.coalesce(func.sum(getattr(CrossBorderOperation, name)), 0).label(name) for name in names]
    rows = (await db.execute(scoped_operations(select(*columns), actor).group_by(CrossBorderOperation.status))).mappings().all()
    return {'direction': 'kz-ge', 'amount_currency': 'KZT', 'received_currency': 'GEL', 'settlement_currency': 'USDT',
            'statuses': [{key: int(value) if key == 'count' else str(value) for key, value in row.items()} for row in rows]}


async def operation_access(db, actor, operation_id, *, lock=False):
    query = select(CrossBorderOperation).where(CrossBorderOperation.id == operation_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    operation = (await db.execute(query)).scalar_one_or_none()
    if not operation or not active(actor):
        raise CrossBorderError('Операция недоступна')
    if actor.role in STAFF or (actor.role == 'trader' and actor.id == operation.trader_id):
        return operation
    await merchant_access(db, actor, operation.merchant_id)
    return operation


async def _requisite(db, trader_id, requisite_id, *, merchant_id=None, lock=False):
    trader = await db.get(User, trader_id)
    query = select(Requisite).where(Requisite.id == requisite_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    requisite = (await db.execute(query)).scalar_one_or_none()
    if (not active(trader) or trader.role != 'trader' or trader.working_country != 'GE'
            or trader.working_currency != 'GEL' or not requisite or requisite.trader_id != trader.id
            or requisite.country != 'GE' or requisite.currency != 'GEL' or requisite.method not in GE_REQUISITE_METHODS
            or not requisite.enabled or requisite.is_archived or requisite.status != 'active'
            or requisite.traffic_status not in ('active', 'reinstated_full')
            or trader.trader_traffic_status not in ('active', 'reinstated_full')):
        raise CrossBorderError('Нужен активный грузинский трейдер с доступным банковским реквизитом GEL')
    from app.models import BankDirectory
    bank = await db.scalar(select(BankDirectory).where(BankDirectory.id == requisite.bank_id)
                           .execution_options(populate_existing=True))
    if not bank or bank.country != 'GE' or bank.status != 'active':
        raise CrossBorderError('Банк грузинского реквизита недоступен')
    if merchant_id and trader.trader_assigned_merchants and str(merchant_id) not in [str(value) for value in trader.trader_assigned_merchants]:
        raise CrossBorderError('Трейдер не назначен этому мерчанту')
    return requisite


async def create_quote(db, *, actor, merchant_id, trader_id, requisite_id, amount_kzt,
                       expected_gel=None, expenses_usdt=0, negative_margin_reason=None, merchant_request=None):
    merchant = await merchant_access(db, actor, merchant_id)
    if merchant.payment_methods is not None and 'kz-ge' not in merchant.payment_methods:
        raise CrossBorderError('Метод KZ → GE не разрешён кабинету')
    if expected_gel is not None or usdt(expenses_usdt) != 0 or negative_margin_reason:
        raise CrossBorderError('Расчётные значения определяет сервер; ручные переопределения запрещены')
    require_country(merchant, 'KZ')
    if merchant.rate_parser_id != PARSER_ID:
        raise CrossBorderError('Для KZ → GE необходим Binance P2P Kaspi Bank')
    requisite = await _requisite(db, trader_id, requisite_id, merchant_id=merchant_id)
    at = now()
    trader_referral = await db.scalar(select(TeamLeadTraderAssignment.id).where(
        TeamLeadTraderAssignment.trader_id == trader_id, TeamLeadTraderAssignment.effective_from <= at,
        or_(TeamLeadTraderAssignment.effective_to.is_(None), TeamLeadTraderAssignment.effective_to > at)).limit(1))
    merchant_referral = await db.scalar(select(TeamLeadMerchantAssignment.id).where(
        TeamLeadMerchantAssignment.merchant_id == merchant_id, TeamLeadMerchantAssignment.valid_from <= at,
        or_(TeamLeadMerchantAssignment.valid_to.is_(None), TeamLeadMerchantAssignment.valid_to > at)).limit(1))
    if trader_referral or merchant_referral:
        raise CrossBorderError('Назначен TeamLead с условиями RUB; сначала требуются отдельные условия распределения USDT')
    amount = local_amount(amount_kzt)
    assignment = await require_assignment(db, merchant_id=merchant.id, parser_id=PARSER_ID, purpose='financial')
    observation = await require_observation(db, PARSER_ID)
    base = base_amount(amount, observation['rate'])
    merchant_plan = await resolve_plan(db, 'merchant', merchant.id)
    trader_plan = await resolve_plan(db, 'trader', trader_id)
    merchant_tier = tier_for(merchant_plan.tiers, base)
    trader_tier = tier_for(trader_plan.tiers, base)
    financial = result(base, merchant_tier['percent'], trader_tier['percent'])
    if Decimal(financial['net_margin_usdt']) < 0:
        raise CrossBorderError('Отрицательная маржа: требуется исправление утверждённых тарифов')
    informational = None
    information_error = None
    try:
        gel_assignment = await require_assignment(db, trader_id=trader_id,
            parser_id=GEL_PARSER_ID, purpose='informational')
        informational = await optional_observation(db, GEL_PARSER_ID)
        informational = {**informational, 'assignment': public_assignment(gel_assignment)}
    except CrossBorderError as exc:
        information_error = str(exc)
    expected = (base * Decimal(informational['rate'])).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if informational else None
    created = now()
    expires = timestamp(observation['expires_at'])
    if expires <= created:
        raise CrossBorderError('awaiting_rate: срок котировки истёк')
    quote = CrossBorderQuote(actor_id=actor.id, merchant_id=merchant.id, trader_id=trader_id,
        direction='kz-ge', created_at=created, expires_at=expires,
        snapshot={'format_version': 4, 'merchant_request': merchant_request, 'status': 'preview', 'creation_channel': 'merchant_api' if actor.role == 'merchant' else 'staff_preview',
            'requisite_id': str(requisite_id), 'requisite_method': requisite.method,
            'requisite_bank_id': requisite.bank_id, 'amount_kzt': str(amount),
            'expected_gel': str(expected) if expected is not None else None,
            'informational_rate': informational, 'informational_error': information_error,
            'financial_assignment': public_assignment(assignment),
            'rate': observation, 'merchant_plan': public_plan(merchant_plan), 'trader_plan': public_plan(trader_plan),
            'merchant_tier': merchant_tier, 'trader_tier': trader_tier, **financial,
            'reserved_usdt': financial['trader_usdt'], 'trader_final': financial['trader_usdt'],
            'tier_basis': 'base_usdt', 'conversion_risk': 'trader',
            'negative_margin_approval': None,
            'rounding': 'USDT 0.00000001 ROUND_HALF_UP; base first; local 0.01; tiers [min,max)'})
    db.add(quote)
    await db.flush()
    return quote


def public_quote(row):
    return {'id': str(row.id), 'merchant_id': str(row.merchant_id), 'trader_id': str(row.trader_id),
            'direction': row.direction, 'created_at': row.created_at.isoformat(),
            'expires_at': row.expires_at.isoformat(), 'snapshot': row.snapshot}


def public_operation(row):
    data = {name: str(getattr(row, name)) if getattr(row, name) is not None else None for name in (
        'id', 'merchant_id', 'trader_id', 'requisite_id', 'quote_id', 'merchant_plan_id', 'trader_plan_id',
        'amount_kzt', 'expected_gel', 'actual_gel', 'base_usdt', 'merchant_usdt', 'reserved_usdt',
        'trader_usdt', 'margin_usdt', 'expenses_usdt', 'confirmed_by')}
    for name in ('created_at', 'expires_at', 'received_at', 'confirmed_at'):
        data[name] = getattr(row, name).isoformat() if getattr(row, name) else None
    data.update(status=row.status, direction=row.direction, external_id=row.external_id,
                confirmation_source=row.confirmation_source, confirmation_reference=row.confirmation_reference,
                deviation_gel=format(row.actual_gel - row.expected_gel, '.2f') if row.actual_gel is not None and row.expected_gel is not None else None,
                deviation_percent=str(((row.actual_gel - row.expected_gel) / row.expected_gel * 100).quantize(Decimal('.0001'))) if row.actual_gel is not None and row.expected_gel else None,
                trader_reward_usdt=str(row.base_usdt - row.trader_usdt) if row.trader_usdt is not None else None,
                net_margin_usdt=str(row.margin_usdt - row.expenses_usdt) if row.margin_usdt is not None else None,
                snapshot=row.snapshot)
    return data


def _wallet_entry(db, wallet, operation, suffix, amount):
    db.add(TraderWalletLedgerEntry(wallet_id=wallet.id, trader_id=wallet.trader_id,
        operation_id=operation.id, entry_type='crossborder_' + suffix, amount=amount,
        balance_after=wallet.balance, hold_after=wallet.hold, currency='USDT',
        idempotency_key=f'crossborder:{operation.id}:{suffix}', description='KZ → GE ' + suffix))


async def _wallet(db, trader_id):
    wallet = await get_usdt_wallet(db, trader_id, for_update=True)
    if not wallet:
        raise CrossBorderError('USDT-кошелёк трейдера не пополнен')
    return wallet


async def accept_quote(db, *, actor, quote_id, external_id):
    if not active(actor) or actor.role != 'merchant':
        raise CrossBorderError('Новые операции создаёт только мерчант через HMAC v2 API')
    external_id = (external_id or '').strip()
    if not external_id or len(external_id) > 128 or any(ord(c) < 32 for c in external_id):
        raise CrossBorderError('Требуется уникальный номер заявки, до 128 символов')
    quote = (await db.execute(select(CrossBorderQuote).where(CrossBorderQuote.id == quote_id).with_for_update())).scalar_one_or_none()
    if not quote or quote.direction != 'kz-ge' or quote.snapshot.get('format_version') not in (3, 4):
        raise CrossBorderError('Действующая котировка не найдена')
    merchant = await merchant_access(db, actor, quote.merchant_id)
    require_country(merchant, 'KZ')
    await scope_lock(db, f'crossborder-external:{merchant.id}:{external_id}')
    existing = (await db.execute(select(CrossBorderOperation).where(
        CrossBorderOperation.merchant_id == merchant.id, CrossBorderOperation.external_id == external_id))).scalar_one_or_none()
    if existing:
        if existing.quote_id != quote_id:
            raise CrossBorderError('Номер заявки уже использован с другой котировкой')
        return existing
    if quote.snapshot.get('format_version') != 4 or quote.actor_id != actor.id or quote.snapshot.get('creation_channel') != 'merchant_api':
        raise CrossBorderError('Получите новую котировку через Merchant HMAC v2 API')
    if (await db.execute(select(CrossBorderOperation.id).where(CrossBorderOperation.quote_id == quote_id))).scalar_one_or_none():
        raise CrossBorderError('Котировка уже принята')
    if quote.expires_at <= now():
        raise CrossBorderError('awaiting_rate: котировка устарела; получите новую')
    snapshot = quote.snapshot
    assignment = await require_assignment(db, merchant_id=merchant.id, parser_id=PARSER_ID, purpose='financial')
    if str(assignment.id) != snapshot['financial_assignment']['id']:
        raise CrossBorderError('Назначение парсера изменилось; получите новую котировку')
    config = await validate_parser(db, merchant.working_country, merchant.rate_parser_id)
    if str(config.id) != snapshot['rate'].get('configuration_id'):
        raise CrossBorderError('Конфигурация курса изменилась до принятия; получите новую котировку')
    # A tariff changed between preview and acceptance requires a new reviewable quote.
    for party, entity in (('merchant', merchant.id), ('trader', quote.trader_id)):
        current = await resolve_plan(db, party, entity)
        if str(current.id) != snapshot[party + '_plan']['id']:
            raise CrossBorderError('Тариф изменился до принятия; получите новую котировку')
    requisite = await _requisite(db, quote.trader_id, UUID(snapshot['requisite_id']), merchant_id=merchant.id, lock=True)
    if ('requisite_method' in snapshot and (snapshot['requisite_method'] != requisite.method
            or snapshot['requisite_bank_id'] != requisite.bank_id)):
        raise CrossBorderError('Банковский реквизит изменился; получите новую котировку')
    await validate_ge_requisite_limits(db, requisite, snapshot['expected_gel'], now())
    wallet = await _wallet(db, quote.trader_id)
    reserve = Decimal(snapshot['reserved_usdt'])
    if wallet.balance - wallet.hold < reserve:
        raise CrossBorderError('Недостаточно доступного обеспечения USDT')
    operation = CrossBorderOperation(merchant_id=merchant.id, trader_id=quote.trader_id,
        requisite_id=requisite.id, quote_id=quote.id, external_id=external_id, direction='kz-ge', status='accepted',
        merchant_plan_id=UUID(snapshot['merchant_plan']['id']), trader_plan_id=UUID(snapshot['trader_plan']['id']),
        amount_kzt=Decimal(snapshot['amount_kzt']), expected_gel=Decimal(snapshot['expected_gel']) if snapshot['expected_gel'] else None,
        base_usdt=Decimal(snapshot['base_usdt']), merchant_usdt=Decimal(snapshot['merchant_usdt']),
        reserved_usdt=reserve, expenses_usdt=Decimal(snapshot['expenses_usdt']), snapshot=snapshot,
        created_at=now(), expires_at=now() + timedelta(minutes=30))
    db.add(operation)
    requisite.usage_count += 1
    wallet.hold += reserve
    await db.flush()
    _wallet_entry(db, wallet, operation, 'reserve', reserve)
    await audit(db, 'crossborder_accepted', 'crossborder_operation', actor.id, operation.id,
                details={'quote_id': str(quote.id), 'reserved_usdt': str(reserve)})
    return operation


async def _balance(db, merchant_id):
    await db.execute(insert(Balance).values(merchant_id=merchant_id, currency='USDT', available=0, frozen=0)
                     .on_conflict_do_nothing(constraint='uq_balance_merchant_currency'))
    return (await db.execute(select(Balance).where(Balance.merchant_id == merchant_id,
        Balance.currency == 'USDT').with_for_update().execution_options(populate_existing=True))).scalar_one()


async def _event(db, operation, event_key, action, payload):
    if not event_key or len(event_key) > 100:
        raise CrossBorderError('Требуется идентификатор события до 100 символов')
    key = f'{operation.id}:{event_key}'
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    old = (await db.execute(select(CrossBorderEvent).where(CrossBorderEvent.event_key == key))).scalar_one_or_none()
    if old:
        if old.action != action or old.payload_hash != digest:
            raise CrossBorderError('Идентификатор события уже использован с другими данными')
        return False
    db.add(CrossBorderEvent(operation_id=operation.id, event_key=key, payload_hash=digest, action=action))
    return True


async def confirm(db, *, actor, operation_id, actual_gel, received_at, source, reference, event_key, bank_credit_confirmed=False):
    operation = await operation_access(db, actor, operation_id, lock=True)
    if actor.role not in STAFF and not (actor.role == 'trader' and operation.trader_id == actor.id):
        raise CrossBorderError('Подтверждать поступление может назначенный трейдер или администратор')
    if operation.snapshot.get('format_version') == 4 and source != 'bank_webhook' and bank_credit_confirmed is not True:
        raise CrossBorderError('Подтвердите проверку банковского зачисления; одной суммы GEL недостаточно')
    actual = local_amount(actual_gel)
    if received_at.tzinfo is None or not operation.created_at <= received_at <= now() + timedelta(seconds=5):
        raise CrossBorderError('Нужны дата и время фактического поступления с часовым поясом')
    if source not in ('trader_manual', 'admin_manual', 'bank_webhook') or not (reference or '').strip() or len(reference) > 200:
        raise CrossBorderError('Нужны источник и номер банковского подтверждения')
    if (source == 'trader_manual' and actor.role != 'trader') or (source in ('admin_manual', 'bank_webhook') and actor.role not in STAFF):
        raise CrossBorderError('Источник подтверждения не соответствует роли')
    payload = {'actual_gel': str(actual), 'received_at': received_at.astimezone(timezone.utc).isoformat(),
               'source': source, 'reference': reference.strip()}
    if operation.snapshot.get('format_version') == 4:
        payload['bank_credit_confirmed'] = source == 'bank_webhook' or bank_credit_confirmed
    if not await _event(db, operation, event_key, 'confirm', payload):
        return operation
    if operation.status == 'paid':
        if (operation.actual_gel == actual and operation.received_at == received_at
                and operation.confirmation_source == source and operation.confirmation_reference == reference.strip()):
            return operation
        raise CrossBorderError('Оплаченная операция уже имеет другое подтверждение')
    if operation.status != 'accepted':
        raise CrossBorderError('Операция закрыта; поступление требует отдельного разбирательства')
    tier = (operation.snapshot['trader_tier'] if operation.snapshot.get('format_version') == 4
            else tier_for(operation.snapshot['trader_plan']['tiers'], actual, basis='local_amount'))
    calculation = result(operation.base_usdt, operation.snapshot['merchant_tier']['percent'],
                         tier['percent'], operation.expenses_usdt)
    debit = Decimal(calculation['trader_usdt'])
    margin = debit - operation.merchant_usdt
    if margin < 0 and not operation.snapshot.get('negative_margin_approval'):
        raise CrossBorderError('Нет разрешения отрицательной маржи')
    wallet = await _wallet(db, operation.trader_id)
    balance = await _balance(db, operation.merchant_id)
    if debit > operation.reserved_usdt or wallet.hold < operation.reserved_usdt or wallet.balance < debit:
        raise CrossBorderError('Нарушен резерв USDT; требуется проверка администратора')
    wallet.hold -= operation.reserved_usdt
    wallet.balance -= debit
    balance.available += operation.merchant_usdt
    _wallet_entry(db, wallet, operation, 'release', -operation.reserved_usdt)
    _wallet_entry(db, wallet, operation, 'debit', -debit)
    db.add(LedgerEntry(merchant_id=operation.merchant_id, operation_id=operation.id,
        entry_type='crossborder_credit', amount=operation.merchant_usdt, currency='USDT',
        idempotency_key=f'crossborder:{operation.id}:credit', description='KZ → GE'))
    db.add(PlatformLedgerEntry(merchant_id=operation.merchant_id, operation_id=operation.id,
        entry_type='crossborder_margin', amount=margin, currency='USDT',
        idempotency_key=f'crossborder:{operation.id}:margin', description='KZ → GE gross margin'))
    if operation.expenses_usdt:
        db.add(PlatformLedgerEntry(merchant_id=operation.merchant_id, operation_id=operation.id,
            entry_type='crossborder_expense', amount=-operation.expenses_usdt, currency='USDT',
            idempotency_key=f'crossborder:{operation.id}:expense', description='KZ → GE additional expenses'))
    operation.status, operation.actual_gel = 'paid', actual
    operation.received_at, operation.confirmed_at = received_at, now()
    operation.confirmation_source, operation.confirmation_reference = source, reference.strip()
    operation.confirmed_by, operation.trader_usdt, operation.margin_usdt = actor.id, debit, margin
    await audit(db, 'crossborder_paid', 'crossborder_operation', actor.id, operation.id,
                details={**payload, **calculation, 'trader_tier': tier,
                         'expected_gel': str(operation.expected_gel) if operation.expected_gel is not None else None,
                         'deviation_gel': public_operation(operation)['deviation_gel'],
                         'deviation_percent': public_operation(operation)['deviation_percent'],
                         'conversion_risk': 'trader'})
    await db.flush()
    return operation


async def cancel(db, *, actor, operation_id, reason, event_key):
    operation = await operation_access(db, actor, operation_id, lock=True)
    if not (reason or '').strip():
        raise CrossBorderError('Укажите причину отмены')
    if not await _event(db, operation, event_key, 'cancel', {'reason': reason.strip()}):
        return operation
    if operation.status == 'cancelled':
        return operation
    if operation.status != 'accepted':
        raise CrossBorderError('Отменить можно только принятую неоплаченную операцию')
    wallet = await _wallet(db, operation.trader_id)
    if wallet.hold < operation.reserved_usdt:
        raise CrossBorderError('Нарушен резерв USDT')
    wallet.hold -= operation.reserved_usdt
    _wallet_entry(db, wallet, operation, 'cancel', -operation.reserved_usdt)
    operation.status = 'cancelled'
    await audit(db, 'crossborder_cancelled', 'crossborder_operation', actor.id, operation.id, details={'reason': reason[:500]})
    await db.flush()
    return operation


async def refund(db, *, actor, operation_id, reason, event_key):
    if actor.role not in STAFF:
        raise CrossBorderError('Возврат доступен только администратору')
    operation = await operation_access(db, actor, operation_id, lock=True)
    if not (reason or '').strip():
        raise CrossBorderError('Укажите основание возврата')
    if not await _event(db, operation, event_key, 'refund', {'reason': reason.strip()}):
        return operation
    if operation.status == 'refunded':
        return operation
    if operation.status != 'paid':
        raise CrossBorderError('Вернуть расчёт можно только по оплаченной операции')
    wallet = await _wallet(db, operation.trader_id)
    balance = await _balance(db, operation.merchant_id)
    if balance.available < operation.merchant_usdt:
        raise CrossBorderError('Недостаточно свободных USDT у мерчанта для возврата')
    balance.available -= operation.merchant_usdt
    wallet.balance += operation.trader_usdt
    _wallet_entry(db, wallet, operation, 'refund', operation.trader_usdt)
    db.add(LedgerEntry(merchant_id=operation.merchant_id, operation_id=operation.id, entry_type='crossborder_refund',
        amount=-operation.merchant_usdt, currency='USDT', idempotency_key=f'crossborder:{operation.id}:refund', description=reason[:500]))
    db.add(PlatformLedgerEntry(merchant_id=operation.merchant_id, operation_id=operation.id, entry_type='crossborder_refund',
        amount=-operation.margin_usdt, currency='USDT', idempotency_key=f'crossborder:{operation.id}:refund', description=reason[:500]))
    # External expenses already incurred are not silently reversed by a refund.
    operation.status = 'refunded'
    await audit(db, 'crossborder_refunded', 'crossborder_operation', actor.id, operation.id, details={'reason': reason[:500]})
    await db.flush()
    return operation
