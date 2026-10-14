"""Append-only local currency grids. Individual versions override country defaults."""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from app.models import Merchant, TariffPlan, User
from app.services.audit import audit
from app.services.crossborder_money import CrossBorderError, local_amount, number, percent, usdt


def require_admin(actor):
    if not actor or actor.role not in ('admin', 'superadmin') or not actor.is_active or actor.is_locked or actor.is_archived:
        raise CrossBorderError('Необходимы права Admin или SuperAdmin')


async def scope_lock(db, scope):
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))'), {'scope': scope})


def dimensions(party):
    if party not in ('merchant', 'trader'):
        raise CrossBorderError('Неизвестная сторона тарифа')
    return ('KZ', 'KZT') if party == 'merchant' else ('GE', 'GEL')


def scope_key(party, entity_id=None, *, basis="base_usdt"):
    country, _ = dimensions(party)
    return f'kz-ge:{party}:{country}:{entity_id or "default"}' + (':base_usdt' if basis == 'base_usdt' else '')


def tier_amount(value, basis):
    if basis == 'local_amount':
        return local_amount(value)
    value = number(value)
    if value <= 0 or value != usdt(value):
        raise CrossBorderError('Граница USDT должна быть положительной, точность до 8 знаков')
    return usdt(value)


def validate_tiers(tiers, *, basis='base_usdt'):
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 100:
        raise CrossBorderError('Нужно от 1 до 100 диапазонов')
    normalized = []
    previous = None
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict) or set(tier) != {'min', 'max', 'percent'}:
            raise CrossBorderError('Диапазон: min, max, percent')
        minimum = tier_amount(tier['min'], basis)
        maximum = tier_amount(tier['max'], basis) if tier['max'] is not None else None
        if maximum is not None and maximum <= minimum:
            raise CrossBorderError('Верхняя граница должна быть выше нижней')
        if index and (previous is None or minimum != previous):
            raise CrossBorderError('Диапазоны должны идти подряд, без разрывов и пересечений')
        previous = maximum
        normalized.append({'min': format(minimum, 'f'), 'max': format(maximum, 'f') if maximum else None,
                           'percent': str(percent(tier['percent']))})
    return normalized


def tier_for(tiers, amount, *, basis='base_usdt'):
    amount = tier_amount(amount, basis)
    for tier in validate_tiers(tiers, basis=basis):
        if Decimal(tier['min']) <= amount and (tier['max'] is None or amount < Decimal(tier['max'])):
            return tier
    raise CrossBorderError('Сумма вне поддерживаемых тарифных диапазонов')


def public_plan(plan):
    return {'id': str(plan.id), 'scope_key': plan.scope_key, 'version': plan.version,
            'party': plan.party, 'country': plan.country, 'currency': plan.currency,
            'direction': plan.direction, 'basis': plan.basis, 'entity_id': str(plan.entity_id) if plan.entity_id else None,
            'tiers': plan.tiers, 'reference': plan.reference, 'active': plan.is_active,
            'effective_from': plan.effective_from.isoformat(),
            'created_by': str(plan.created_by) if plan.created_by else None,
            'created_at': plan.created_at.isoformat() if plan.created_at else None}


async def display_plan(db, plan):
    """Read-only local equivalents, never persisted as canonical tariff bounds."""
    result = public_plan(plan)
    if plan.basis != 'base_usdt':
        return result
    from app.services.binance_p2p import PARSER_ID, GEL_PARSER_ID
    from app.services.rate_registry import parser_state
    state = await parser_state(db, PARSER_ID if plan.country == 'KZ' else GEL_PARSER_ID, refresh=False, persist=False)
    result['display_currency'] = 'KZT' if plan.country == 'KZ' else 'GEL'
    result['display_tiers'] = []
    if state['available']:
        fx = Decimal(state['quote']['rate'])
        result['display_rate'] = str(fx)
        result['display_fetched_at'] = state['quote']['fetched_at']
        result['display_tiers'] = [{**t, 'min': str((Decimal(t['min'])*fx).quantize(Decimal('.01'))),
            'max': str((Decimal(t['max'])*fx).quantize(Decimal('.01'))) if t['max'] else None} for t in plan.tiers]
    return result


async def resolve_plan(db, party, entity_id, *, at=None):
    at = at or datetime.now(timezone.utc)
    if party == 'merchant':
        merchant = await db.get(Merchant, entity_id)
        if merchant and merchant.tariff_plan_id:
            from app.services.merchant_cabinets import validate_plan
            return await validate_plan(db, merchant, merchant.tariff_plan_id)
    for owner in (entity_id, None):
        key = scope_key(party, owner)
        legacy_key = scope_key(party, owner, basis='local_amount')
        plans = (await db.execute(select(TariffPlan).where(
            TariffPlan.scope_key.in_((key, legacy_key)), TariffPlan.effective_from <= at,
        ).order_by(TariffPlan.effective_from.desc(), TariffPlan.version.desc()))).scalars().all()
        plan = next((p for p in plans if p.basis == 'base_usdt'), None)
        legacy = next((p for p in plans if p.basis == 'local_amount'), None)
        if legacy and (not plan or (plan.reference.get('source') == 'constant_percentage_migration'
                       and plan.reference.get('legacy_plan_id') != str(legacy.id))):
            raise CrossBorderError('Локальная тарифная сетка требует явного утверждения границ BASE_USDT')
        if plan:
            if not plan.is_active:
                raise CrossBorderError('Действующий тарифный план отключён')
            validate_tiers(plan.tiers)
            return plan
    raise CrossBorderError('Тарифный план не настроен')


def reference_proposal(*, party, local_per_usdt, reference_date, local_tiers):
    country, local_currency = dimensions(party)
    fx = number(local_per_usdt)
    try:
        dated = date.fromisoformat(str(reference_date))
    except (ValueError, TypeError) as exc:
        raise CrossBorderError('Требуется дата справочного курса') from exc
    if fx <= 0 or dated > datetime.now(timezone.utc).date():
        raise CrossBorderError('Нужен положительный справочный курс с прошедшей или текущей датой')
    local_tiers = validate_tiers(local_tiers, basis='local_amount')
    converted = [{'min': str(usdt(Decimal(t['min']) / fx)),
                  'max': str(usdt(Decimal(t['max']) / fx)) if t['max'] else None,
                  'percent': t['percent']} for t in local_tiers]
    return {'country': country, 'currency': 'USDT', 'basis': 'base_usdt', 'tiers': validate_tiers(converted),
            'reference': {'source': 'approved_local_to_usdt', 'local_currency': local_currency,
                          'local_per_usdt': str(fx), 'date': dated.isoformat(), 'local_tiers': local_tiers},
            'requires_approval': True}


async def create_plan(db, *, actor, party, tiers, effective_from, entity_id=None,
                      is_active=True, reference=None, approve_reference=False, payment_method='kz-ge'):
    require_admin(actor)
    if payment_method != 'kz-ge':
        raise CrossBorderError('Метод не соответствует направлению KZ → GE')
    country, currency = dimensions(party)
    if entity_id:
        entity_id = UUID(str(entity_id))
        entity = await db.get(Merchant if party == 'merchant' else User, entity_id)
        if (not entity or entity.working_country != country or entity.is_archived
                or (party == 'trader' and (entity.role != 'trader' or not entity.is_active or entity.is_locked))):
            raise CrossBorderError('Индивидуальный тариф не соответствует стране кабинета')
    if effective_from.tzinfo is None:
        raise CrossBorderError('Время действия должно содержать часовой пояс')
    tiers = validate_tiers(tiers)
    reference = dict(reference or {})
    if reference:
        if not approve_reference or reference.get('source') not in ('approved_local_to_usdt', 'canonical_usdt_reference'):
            raise CrossBorderError('Преобразованные диапазоны требуют явного подтверждения')
        if reference['source'] == 'approved_local_to_usdt':
            proposal = reference_proposal(party=party, local_per_usdt=reference.get('local_per_usdt'),
                reference_date=reference.get('date'), local_tiers=reference.get('local_tiers'))
            if tiers != proposal['tiers']:
                raise CrossBorderError('Диапазоны отличаются от подтверждаемого валютного пересчёта')
            reference = proposal['reference']
        else:
            fx = number(reference.get('local_per_usdt'))
            try:
                dated = date.fromisoformat(str(reference.get('date')))
            except ValueError as exc:
                raise CrossBorderError('Требуется дата справочного курса') from exc
            if fx <= 0 or dated > datetime.now(timezone.utc).date():
                raise CrossBorderError('Некорректный справочный курс или дата')
            reference = {'source': 'canonical_usdt_reference', 'local_currency': currency,
                'local_per_usdt': str(fx), 'date': dated.isoformat(),
                'local_tiers': [{**t, 'min': str((Decimal(t['min']) * fx).quantize(Decimal('.01'))),
                    'max': str((Decimal(t['max']) * fx).quantize(Decimal('.01'))) if t['max'] else None} for t in tiers]}
        reference = {**reference, 'approved_by': str(actor.id),
                     'approved_at': datetime.now(timezone.utc).isoformat()}
    # Canonical USDT bounds need no FX conversion. Explicit legacy conversions
    # above retain their approval validation; automatic quotes never rewrite tiers.
    key = scope_key(party, entity_id)
    await scope_lock(db, key)
    previous = (await db.execute(select(TariffPlan).where(TariffPlan.scope_key == key)
        .order_by(TariffPlan.version.desc()).limit(1))).scalar_one_or_none()
    plan = TariffPlan(scope_key=key, version=(previous.version + 1 if previous else 1),
        party=party, country=country, currency='USDT', basis='base_usdt', direction='kz-ge', entity_id=entity_id,
        tiers=tiers, reference=reference, effective_from=effective_from.astimezone(timezone.utc),
        is_active=is_active, created_by=actor.id)
    db.add(plan)
    await db.flush()
    if party == 'merchant' and entity_id and entity.tariff_plan_id and effective_from <= datetime.now(timezone.utc):
        entity.tariff_plan_id = plan.id
        await db.flush()
    await audit(db, 'country_tariff_version_created', 'country_tariff_plan', actor.id, plan.id,
                details={'old': public_plan(previous) if previous else None, 'new': public_plan(plan)})
    return plan
