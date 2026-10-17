"""TJ configuration uses the existing cabinet, tariff and wallet models.

Payment admission is intentionally blocked independently of these preparations.
No initial percentage, maximum check or QR contract is invented here.
"""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models import Balance, FeeRule, Merchant, TariffPlan, TraderWallet, User
from app.services.audit import audit
from app.services.country_tariffs import require_admin, scope_lock, validate_tiers, public_plan
from app.services.crossborder_money import CrossBorderError


async def provision_balances(db, *, merchant=None, trader=None):
    owner = merchant or trader
    if not owner or owner.working_country != 'TJ':
        raise CrossBorderError('Нужен отдельный кабинет TJ')
    model = Balance if merchant else TraderWallet
    owner_field = 'merchant_id' if merchant else 'trader_id'
    constraint = 'uq_balance_merchant_currency' if merchant else 'uq_trader_wallet_currency'
    for currency in ('TJS', 'USDT'):
        values = {owner_field: owner.id, 'currency': currency}
        values.update({'available': 0, 'frozen': 0} if merchant else {'balance': 0, 'hold': 0})
        await db.execute(insert(model).values(**values).on_conflict_do_nothing(constraint=constraint))


async def create_plan(db, *, actor, party, tiers, effective_from, entity_id=None):
    require_admin(actor)
    if party not in ('merchant', 'trader'):
        raise CrossBorderError('Нужна сторона merchant или trader')
    if effective_from.tzinfo is None:
        raise CrossBorderError('Время действия должно содержать часовой пояс')
    tiers = validate_tiers(tiers, basis='local_amount')
    if Decimal(tiers[0]['min']) < 10:
        raise CrossBorderError('Границы тарифов TJ начинаются не ниже 10 TJS')
    entity_id = UUID(str(entity_id)) if entity_id else None
    if entity_id:
        entity = await db.get(Merchant if party == 'merchant' else User, entity_id)
        if (not entity or entity.working_country != 'TJ' or entity.is_archived
                or (party == 'trader' and (entity.role != 'trader' or not entity.is_active or entity.is_locked))):
            raise CrossBorderError('Тариф принадлежит только кабинету TJ соответствующей роли')
    key = f'tj-tj:{party}:{entity_id or "default"}'
    await scope_lock(db, key)
    previous = await db.scalar(select(TariffPlan).where(TariffPlan.scope_key == key)
        .order_by(TariffPlan.version.desc()).limit(1))
    plan = TariffPlan(scope_key=key, version=previous.version + 1 if previous else 1,
        party=party, country='TJ', currency='TJS', basis='local_amount', direction='tj-tj',
        entity_id=entity_id, tiers=tiers, reference={'method': 'c2c'},
        effective_from=effective_from.astimezone(timezone.utc), is_active=True, created_by=actor.id)
    db.add(plan)
    await db.flush()
    await audit(db, 'country_tariff_version_created', 'country_tariff_plan', actor.id, plan.id,
                details={'old': public_plan(previous) if previous else None, 'new': public_plan(plan)})
    return plan


async def assign_trader_plan(db, *, actor, trader, plan_id):
    require_admin(actor)
    await scope_lock(db, f'tj-trader-tariff:{trader.id}')
    trader = await db.scalar(select(User).where(User.id == trader.id).with_for_update())
    plan = await db.get(TariffPlan, UUID(str(plan_id)))
    now = datetime.now(timezone.utc)
    if (not trader or trader.role != 'trader' or trader.working_country != 'TJ'
            or trader.is_archived or not trader.is_active or trader.is_locked
            or not plan or plan.party != 'trader' or plan.country != 'TJ'
            or plan.currency != 'TJS' or plan.basis != 'local_amount' or plan.direction != 'tj-tj'
            or plan.entity_id not in (None, trader.id) or not plan.is_active or plan.effective_from > now):
        raise CrossBorderError('Несовместимый тариф или кабинет TJ')
    from app.services.fee_tiers import create_fee_rule
    old = (await db.execute(select(FeeRule).where(FeeRule.entity_type == 'trader',
        FeeRule.entity_id == trader.id, FeeRule.currency == 'TJS', FeeRule.is_active.is_(True))
        .with_for_update())).scalars().all()
    version = max((row.version for row in old), default=0) + 1
    for row in old:
        row.is_active = False
        if row.effective_from < now:
            row.effective_to = now
    await db.flush()
    for tier in plan.tiers:
        await create_fee_rule(db, actor_id=actor.id, entity_type='trader', entity_id=trader.id,
            fee_side='executor_fee', payment_method='c2c', currency='TJS',
            min_amount=Decimal(tier['min']), max_amount=Decimal(tier['max']) if tier['max'] else None,
            rate_percent=Decimal(tier['percent']), effective_from=now, effective_to=None, version=version)
    await audit(db, 'tj_trader_tariff_assigned', 'user', actor.id, trader.id,
                details={'plan_id': str(plan.id), 'plan_version': plan.version, 'fee_version': version})
    return plan
