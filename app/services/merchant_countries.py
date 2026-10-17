"""One immutable country per business cabinet, assigned only by staff."""
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models import BusinessAccount, Merchant, ParserConfiguration
from app.services.audit import audit
from app.services.binance_p2p import PARSER_ID
from app.services.country_tariffs import require_admin, scope_lock
from app.services.crossborder_money import CrossBorderError

COUNTRY_PARSERS = {'RU': ('rapira',), 'KZ': (PARSER_ID,), 'GE': ('xe',), 'TJ': ('bybit_p2p_tjs_green_avg3',)}
COUNTRY_CURRENCIES = {'RU': 'RUB', 'KZ': 'KZT', 'GE': 'GEL', 'TJ': 'TJS'}


async def validate_parser(db, country, parser_id):
    if country not in COUNTRY_PARSERS or parser_id not in COUNTRY_PARSERS[country]:
        raise CrossBorderError('Необходимо выбрать страну и совместимый парсер')
    config = (await db.execute(select(ParserConfiguration).where(ParserConfiguration.parser_id == parser_id)
        .order_by(ParserConfiguration.version.desc()).limit(1))).scalar_one_or_none()
    if not config or (not config.is_active and country != 'TJ') or config.country != country:
        raise CrossBorderError('Парсер отключён или не настроен')
    return config


async def assign_country(db, *, actor, merchant, country, parser_id, business_id=None):
    require_admin(actor)
    await validate_parser(db, country, parser_id)
    if merchant.working_country:
        raise CrossBorderError('Страна кабинета уже зафиксирована навсегда')
    if country == 'TJ' and await db.scalar(text("""SELECT
        EXISTS(SELECT 1 FROM balances WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM deposits WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM payouts WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM ledger_entries WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM merchant_settlements WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM merchant_rolling_accounts WHERE merchant_id=:id) OR
        EXISTS(SELECT 1 FROM crossborder_operations WHERE merchant_id=:id)
        """), {'id':merchant.id}):
        raise CrossBorderError('Для TJ нужен новый отдельный кабинет без прежних кошельков и финансовой истории')
    business_id = business_id or merchant.business_id
    if business_id:
        business = await db.get(BusinessAccount, business_id)
        if not business:
            raise CrossBorderError('Бизнес-аккаунт не найден')
    else:
        business = BusinessAccount(name=merchant.name, created_by=actor.id)
        db.add(business)
        await db.flush()
    await scope_lock(db, f'merchant-country:{business.id}:{country}')
    if (await db.execute(select(Merchant.id).where(Merchant.business_id == business.id,
            Merchant.working_country == country, Merchant.id != merchant.id))).scalar_one_or_none():
        raise CrossBorderError('У этого бизнеса уже есть кабинет данной страны')
    merchant.business_id, merchant.working_country = business.id, country
    merchant.working_currency = COUNTRY_CURRENCIES[country]
    merchant.rate_parser_id = parser_id
    merchant.country_assigned_at = datetime.now(timezone.utc)
    if country == 'TJ':
        # Legacy RUB percentage is not an approved TJ tariff.
        merchant.merchant_commission_percent = 0
    await db.flush()
    await audit(db, 'merchant_country_assigned', 'merchant', actor.id, merchant.id,
                details={'country': country, 'parser_id': parser_id, 'business_id': str(business.id)})
    from app.services.parser_assignments import assign
    await assign(db, actor=actor, owner_type='merchant', owner_id=merchant.id,
        parser_id=parser_id, purpose='informational' if merchant.working_country == 'GE' else 'financial',
        is_active=country != 'TJ')
    if country == 'TJ':
        from app.services.tj_cabinets import provision_balances
        await provision_balances(db, merchant=merchant)
    return merchant


async def change_parser(db, *, actor, merchant, parser_id):
    require_admin(actor)
    await validate_parser(db, merchant.working_country, parser_id)
    old = merchant.rate_parser_id
    merchant.rate_parser_id = parser_id
    await audit(db, 'merchant_parser_changed', 'merchant', actor.id, merchant.id,
                details={'country': merchant.working_country, 'old': old, 'new': parser_id})
    from app.services.parser_assignments import assign
    await assign(db, actor=actor, owner_type='merchant', owner_id=merchant.id,
        parser_id=parser_id, purpose='informational' if merchant.working_country == 'GE' else 'financial',
        is_active=merchant.working_country != 'TJ')
    return merchant


def require_country(merchant, country):
    if not merchant or not merchant.working_country:
        raise CrossBorderError('Страна мерчанта требует ручного назначения администратором')
    if merchant.working_country != country:
        raise CrossBorderError('Операция недоступна для страны этого кабинета')
