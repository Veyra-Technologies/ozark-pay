"""Country-safe assignments; existing RU fee and KZ settlement engines remain authoritative."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from app.core.merchant_catalog import MERCHANT_COUNTRIES
from app.models import Merchant, TariffPlan, FeeRule
from app.services.audit import audit
from app.services.country_tariffs import require_admin, scope_lock, validate_tiers, public_plan
from app.services.crossborder_money import CrossBorderError


def validate_dimensions(country, currency, parser_id, methods):
    policy = MERCHANT_COUNTRIES.get(country)
    if not policy or currency != policy['currency'] or parser_id not in policy['parsers']:
        raise CrossBorderError('Страна, валюта и парсер несовместимы')
    if not methods or len(methods) != len(set(methods)) or not set(methods) <= policy['methods'].keys():
        raise CrossBorderError('Методы не разрешены для страны мерчанта')


async def validate_plan(db, merchant, plan_id):
    plan = await db.get(TariffPlan, UUID(str(plan_id))) if plan_id else None
    if (not plan or plan.party != 'merchant' or plan.country != merchant.working_country
            or plan.entity_id not in (None, merchant.id) or not plan.is_active
            or plan.effective_from > datetime.now(timezone.utc)
            or (plan.country == 'KZ' and (plan.basis != 'base_usdt' or plan.direction != 'kz-ge'))
            or (plan.country == 'RU' and (plan.basis != 'local_amount' or plan.direction != 'ru'))
            or (plan.country == 'TJ' and (plan.entity_id != merchant.id or plan.basis != 'local_amount' or plan.direction != 'tj-tj' or plan.currency != 'TJS'))):
        raise CrossBorderError('Тариф не принадлежит стране и кабинету либо ещё не действует')
    return plan


async def apply_ru_plan(db, actor, merchant, plan, methods):
    from app.services.fee_tiers import create_fee_rule
    now = datetime.now(timezone.utc)
    old = (await db.execute(select(FeeRule).where(FeeRule.entity_type == 'merchant',
        FeeRule.entity_id == merchant.id, FeeRule.currency == plan.currency, FeeRule.is_active.is_(True))
        .with_for_update())).scalars().all()
    for rule in old:
        rule.is_active = False
        if rule.effective_from < now:
            rule.effective_to = now
        rule.updated_by = actor.id
    await db.flush()
    for method in methods:
        for tier in plan.tiers:
            await create_fee_rule(db, actor_id=actor.id, entity_type='merchant', entity_id=merchant.id,
                fee_side='merchant_fee', payment_method=method, currency=plan.currency,
                min_amount=Decimal(tier['min']), max_amount=Decimal(tier['max']) if tier['max'] else None,
                rate_percent=Decimal(tier['percent']), effective_from=now, effective_to=None,
                version=max((r.version for r in old), default=0) + 1)


async def configure(db, *, actor, merchant, currency, parser_id, tariff_plan_id, methods):
    require_admin(actor)
    await scope_lock(db, f'merchant-cabinet:{merchant.id}')
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant.id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if merchant.is_archived:
        raise CrossBorderError('Кабинет архивирован')
    from app.services.crossborder_operations import merchant_access
    await merchant_access(db, actor, merchant.id)
    validate_dimensions(merchant.working_country, currency, parser_id, methods)
    plan = await validate_plan(db, merchant, tariff_plan_id)
    from app.services.merchant_countries import change_parser
    if merchant.rate_parser_id != parser_id:
        await change_parser(db, actor=actor, merchant=merchant, parser_id=parser_id)
    previous = {'tariff_plan_id': str(merchant.tariff_plan_id), 'methods': merchant.payment_methods}
    if merchant.working_country in ('RU', 'TJ'):
        await apply_ru_plan(db, actor, merchant, plan, methods)
    merchant.working_currency = currency
    merchant.tariff_plan_id = plan.id
    merchant.payment_methods = list(methods)
    await db.flush()
    await audit(db, 'merchant_cabinet_configured', 'merchant', actor.id, merchant.id,
        details={'previous': previous, 'country': merchant.working_country, 'currency': currency,
                 'parser_id': parser_id, 'tariff_plan_id': str(plan.id), 'methods': methods})
    return merchant


async def catalog(db, merchant_id=None):
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(TariffPlan).where(TariffPlan.party == 'merchant',
        TariffPlan.country.in_(MERCHANT_COUNTRIES), TariffPlan.is_active.is_(True),
        TariffPlan.effective_from <= now).order_by(TariffPlan.created_at.desc()))).scalars().all()
    return {country: {**policy, 'plans': [public_plan(p) for p in rows
        if p.country == country and p.entity_id in (None, merchant_id)
        and ((country == 'KZ' and p.basis == 'base_usdt') or (country == 'RU' and p.direction == 'ru')
             or (country == 'TJ' and p.direction == 'tj-tj' and p.currency == 'TJS' and p.basis == 'local_amount'))]}
        for country, policy in MERCHANT_COUNTRIES.items()}


async def create_ru_plan(db, *, actor, tiers, entity_id=None):
    require_admin(actor)
    tiers = validate_tiers(tiers, basis='local_amount')
    merchant = await db.get(Merchant, entity_id) if entity_id else None
    if entity_id and (not merchant or merchant.working_country != 'RU' or merchant.is_archived):
        raise CrossBorderError('Тариф RUB доступен только мерчанту RU')
    key = f'ru:merchant:{entity_id or "default"}'
    await scope_lock(db, key)
    previous = (await db.execute(select(TariffPlan).where(TariffPlan.scope_key == key)
        .order_by(TariffPlan.version.desc()).limit(1))).scalar_one_or_none()
    plan = TariffPlan(scope_key=key, version=previous.version + 1 if previous else 1,
        party='merchant', country='RU', currency='RUB', basis='local_amount', direction='ru',
        entity_id=entity_id, tiers=tiers, reference={}, effective_from=datetime.now(timezone.utc),
        is_active=True, created_by=actor.id)
    db.add(plan)
    await db.flush()
    if merchant:
        await configure(db, actor=actor, merchant=merchant, currency='RUB', parser_id=merchant.rate_parser_id,
            tariff_plan_id=plan.id, methods=merchant.payment_methods or list(MERCHANT_COUNTRIES['RU']['methods']))
    await audit(db, 'country_tariff_version_created', 'country_tariff_plan', actor.id, plan.id, details=public_plan(plan))
    return plan
