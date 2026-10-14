"""Cabinet parser assignments and their immutable configuration history."""
from sqlalchemy import select

from app.models import Merchant, ParserAssignment, User
from app.services.audit import audit
from app.services.binance_p2p import PARSER_ID, GEL_PARSER_ID
from app.services.country_tariffs import require_admin, scope_lock
from app.services.crossborder_money import CrossBorderError


def public_assignment(row):
    return {**{k: str(getattr(row, k)) if getattr(row, k) is not None else None
               for k in ('id', 'merchant_id', 'trader_id', 'configuration_id', 'created_by')},
            **{k: getattr(row, k) for k in ('scope_key', 'version', 'country', 'parser_id', 'purpose',
                 'configuration_version', 'is_active', 'reason')},
            'created_at': row.created_at.isoformat() if row.created_at else None}


async def current(db, *, merchant_id=None, trader_id=None):
    query = select(ParserAssignment)
    if merchant_id:
        query = query.where(ParserAssignment.merchant_id == merchant_id)
    if trader_id:
        query = query.where(ParserAssignment.trader_id == trader_id)
    return (await db.execute(query.distinct(ParserAssignment.scope_key)
        .order_by(ParserAssignment.scope_key, ParserAssignment.version.desc()))).scalars().all()


async def assign(db, *, actor, owner_type, owner_id, parser_id, purpose, is_active=True,
                 reason='admin_assignment'):
    require_admin(actor)
    if owner_type not in ('merchant', 'trader') or purpose not in ('financial', 'informational'):
        raise CrossBorderError('Некорректное назначение парсера')
    owner = await db.get(Merchant if owner_type == 'merchant' else User, owner_id)
    if not owner or not owner.working_country or (owner_type == 'trader' and owner.role != 'trader'):
        raise CrossBorderError('Кабинет или страна не найдены')
    country = owner.working_country
    allowed = ({'RU': ['rapira'], 'KZ': [PARSER_ID], 'GE': ['xe']}.get(country, [])
               if owner_type == 'merchant' else ([GEL_PARSER_ID, PARSER_ID] if country == 'GE' else []))
    required_purpose = 'financial' if owner_type == 'merchant' and country in ('RU', 'KZ') else 'informational'
    if parser_id not in allowed or purpose != required_purpose:
        raise CrossBorderError('Парсер или назначение не соответствует стране кабинета')
    from app.services.rate_registry import configuration
    await scope_lock(db, 'parser-config:' + parser_id)
    config = await configuration(db, parser_id)
    if is_active and not config.is_active:
        raise CrossBorderError('Конфигурация парсера отключена')
    key = f'{owner_type}:{owner_id}:{purpose}:{parser_id}'
    await scope_lock(db, 'parser-assignment:' + key)
    old = await db.scalar(select(ParserAssignment).where(ParserAssignment.scope_key == key)
        .order_by(ParserAssignment.version.desc()).limit(1))
    row = ParserAssignment(scope_key=key, version=old.version + 1 if old else 1,
        merchant_id=owner_id if owner_type == 'merchant' else None,
        trader_id=owner_id if owner_type == 'trader' else None, country=country, parser_id=parser_id,
        purpose=purpose, configuration_id=config.id, configuration_version=config.version,
        is_active=is_active, created_by=actor.id, reason=reason)
    db.add(row)
    if owner_type == 'merchant' and purpose == 'financial':
        owner.rate_parser_id = parser_id  # Preserve the established primary parser contract.
    await db.flush()
    await audit(db, 'parser_assignment_changed', 'parser_assignment', actor.id, row.id,
                details={'old': public_assignment(old) if old else None, 'new': public_assignment(row)})
    return row


async def advance_configuration(db, *, actor, old, new):
    # Retain each cabinet's enabled/disabled decision across global config changes.
    for row in await current(db):
        if row.parser_id == new.parser_id:
            await scope_lock(db, 'parser-assignment:' + row.scope_key)
            revised = ParserAssignment(scope_key=row.scope_key, version=row.version + 1,
                merchant_id=row.merchant_id, trader_id=row.trader_id, country=row.country,
                parser_id=row.parser_id, purpose=row.purpose, configuration_id=new.id,
                configuration_version=new.version, is_active=row.is_active, created_by=actor.id,
                reason=f'configuration_version:{new.version}')
            db.add(revised)
            await db.flush()
            await audit(db, 'parser_assignment_changed', 'parser_assignment', actor.id, revised.id,
                details={'old': public_assignment(row), 'new': public_assignment(revised)})


async def require_assignment(db, *, merchant_id=None, trader_id=None, parser_id, purpose):
    rows = await current(db, merchant_id=merchant_id, trader_id=trader_id)
    row = next((r for r in rows if r.parser_id == parser_id and r.purpose == purpose), None)
    if not row or not row.is_active:
        raise CrossBorderError('Парсер не назначен кабинету или отключён')
    from app.services.rate_registry import configuration
    config = await configuration(db, parser_id)
    if not config.is_active or config.id != row.configuration_id:
        raise CrossBorderError('Назначение парсера требует актуальной конфигурации')
    return row


async def provision_trader(db, *, actor, trader):
    if trader.role == 'trader' and trader.working_country == 'GE':
        db.add(trader)
        await db.flush()
        # Never reactivate or replace an existing explicit decision.
        if not any(r.parser_id == GEL_PARSER_ID for r in await current(db, trader_id=trader.id)):
            await assign(db, actor=actor, owner_type='trader', owner_id=trader.id,
                parser_id=GEL_PARSER_ID, purpose='informational', reason='GE_country_default')


async def require_legacy_rapira_assignment(db, merchant_id):
    """Honor cabinet decisions without inventing assignments for legacy owners."""
    rows = await current(db, merchant_id=merchant_id)
    if any(row.parser_id == 'rapira' and row.purpose == 'financial' for row in rows):
        await require_assignment(db, merchant_id=merchant_id, parser_id='rapira', purpose='financial')
