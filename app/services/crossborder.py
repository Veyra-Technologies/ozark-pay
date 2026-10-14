"""Preparation catalog. No payment service imports or consumes these drafts."""
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from app.models.crossborder import CrossBorderDraft
from app.models import User, Merchant, MerchantCrossBorderRateSetting

DIRECTIONS = (
    {'id': 'kz-ge', 'merchant_country': 'KZ', 'merchant_country_label': 'Казахстан',
     'trader_country': 'GE', 'trader_country_label': 'Грузия', 'pay_currency': 'KZT', 'receive_currency': 'GEL'},
)


def require_live_direction(direction_id: str):
    if direction_id != 'kz-ge':
        raise ValueError('crossborder_direction_disabled')


async def draft_context(db, user, role):
    if role != 'superadmin' or user.role != 'superadmin':
        return {'directions': [], 'profile': {}}
    catalog = [dict(row, status='preparing', enabled=False) for row in DIRECTIONS]
    profile = {}
    if role == 'superadmin' and user.role == 'superadmin':
        rows = (await db.execute(select(CrossBorderDraft).where(CrossBorderDraft.scope == 'direction'))).scalars()
        by_key = {row.owner_key: row.data for row in rows}
        for item in catalog:
            item['draft'] = by_key.get(item['id'], {})
    from app.services.xe import get_state
    from app.services.binance_p2p import get_state as get_binance_state, PARSER_LABEL
    state = await get_state()
    # Beat and the dedicated status endpoint refresh the shared quote. Merely
    # rendering a cabinet never starts another provider request.
    binance_state = await get_binance_state(refresh=False)
    merchants = (await db.execute(select(Merchant).join(User, User.id == Merchant.owner_id).where(
        Merchant.is_archived.is_(False), User.is_active.is_(True), User.is_locked.is_(False), User.is_archived.is_(False)).order_by(Merchant.name))).scalars().all()
    traders = (await db.execute(select(User).where(User.role == 'trader',
        User.working_country == 'GE', User.working_rate_source == 'xe',
        User.is_active.is_(True), User.is_locked.is_(False), User.is_archived.is_(False)).order_by(User.email))).scalars().all()
    settings_rows = (await db.execute(select(MerchantCrossBorderRateSetting))).scalars().all()
    merchant_names = {merchant.id: merchant.name for merchant in merchants}
    rate_settings = [{
        'id': str(row.id), 'merchant_id': str(row.merchant_id),
        'merchant_name': merchant_names.get(row.merchant_id, 'Архивный мерчант'),
        'direction': row.direction, 'destination_country': row.destination_country,
        'pay_currency': row.pay_currency, 'parser_id': row.parser_id,
        'parser_label': PARSER_LABEL if row.parser_id else row.parser_id,
    } for row in settings_rows]
    return {'directions': catalog, 'profile': profile, 'rates': state,
            'binance_rates': binance_state, 'rate_settings': rate_settings,
            'merchants': merchants, 'traders': traders}


async def save_draft(db, *, scope, owner_key, data, actor_id):
    statement = insert(CrossBorderDraft).values(scope=scope, owner_key=owner_key,
        data=data, status='preparing', updated_by=actor_id)
    await db.execute(statement.on_conflict_do_update(
        constraint='uq_crossborder_draft_owner',
        set_={'data': data, 'updated_by': actor_id, 'updated_at': func.now()}))
