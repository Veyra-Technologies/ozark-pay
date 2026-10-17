"""Versioned parser policy and immutable, independently verifiable observations.

Transport stays in the existing Binance/Rapira services. Reads of saved financial
quotes never invoke a provider or replace their fixed value.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.models import ParserConfiguration, RateObservation
from app.services import binance_p2p, rapira
from app.services.audit import audit
from app.services.country_tariffs import require_admin, scope_lock
from app.services.crossborder_money import CrossBorderError

PARSER_POLICY = {
    'rapira_green_tjs': {'source': 'RAPIRA_GREEN_UNCONFIRMED', 'pair': 'USDT/TJS',
        'positions': [1, 2, 3], 'unit': 'TJS per 1 USDT', 'contract_confirmed': False},
    'rapira': {'source': 'RAPIRA', 'pair': 'USDT/RUB', 'side': 'ask',
               'provider_field': 'askPrice', 'unit': 'RUB per 1 USDT'},
    binance_p2p.PARSER_ID: {'source': 'BINANCE_P2P', 'pair': 'USDT/KZT', 'side': 'BUY',
        'advertiser_side': 'SELL', 'payment_method': 'KaspiBank', 'control_amount': '100000',
        'ad_count': 10, 'unit': 'KZT per 1 USDT'},
    binance_p2p.GEL_PARSER_ID: {'source': 'BINANCE_P2P', 'pair': 'USDT/GEL', 'side': 'BUY',
        'advertiser_side': 'SELL', 'payment_method': 'TBCbank', 'control_amount': '500',
        'ad_count': 10, 'unit': 'GEL per 1 USDT', 'purpose': 'informational'},
    'xe': {'source': 'XE', 'pair': 'USD/GEL', 'unit': 'GEL per 1 USD', 'purpose': 'local_display'},
}


async def configuration(db, parser_id):
    config = (await db.execute(select(ParserConfiguration).where(ParserConfiguration.parser_id == parser_id)
        .order_by(ParserConfiguration.version.desc()).limit(1))).scalar_one_or_none()
    if not config:
        raise CrossBorderError('Конфигурация парсера не найдена')
    return config


def public_config(row):
    return {'id': str(row.id), 'parser_id': row.parser_id, 'version': row.version,
            'country': row.country, 'direction': row.direction, 'active': row.is_active,
            'parameters': {**PARSER_POLICY[row.parser_id], **row.parameters},
            'created_by': str(row.created_by) if row.created_by else None,
            'created_at': row.created_at.isoformat() if row.created_at else None}


async def configure(db, *, actor, parser_id, refresh_seconds, ttl_seconds, is_active):
    require_admin(actor)
    if parser_id == 'rapira_green_tjs' and is_active:
        raise CrossBorderError('Rapira Green 1–3: источник и сторона рынка не подтверждены; включение запрещено')
    if not 15 <= refresh_seconds <= ttl_seconds <= 120:
        raise CrossBorderError('Интервал: от 15 секунд; TTL не меньше интервала и не больше 120 секунд')
    if parser_id == 'xe':
        raise CrossBorderError('XE настраивается в существующем разделе валютных курсов')
    if parser_id == 'rapira' and ttl_seconds > settings.ROLLING_RAPIRA_MAX_AGE_SECONDS:
        raise CrossBorderError('TTL превышает допустимый возраст финансового курса Rapira')
    await scope_lock(db, 'parser-config:' + parser_id)
    old = await configuration(db, parser_id)
    row = ParserConfiguration(parser_id=parser_id, version=old.version + 1, country=old.country,
        direction=old.direction, parameters={'refresh_seconds': refresh_seconds, 'ttl_seconds': ttl_seconds},
        is_active=is_active, created_by=actor.id)
    db.add(row)
    await db.flush()
    from app.services.parser_assignments import advance_configuration
    await advance_configuration(db, actor=actor, old=old, new=row)
    await audit(db, 'parser_configuration_version_created', 'parser_configuration', actor.id, row.id,
                details={'old': public_config(old), 'new': public_config(row)})
    return row


async def _rapira_state(config, refresh):
    key = 'ozark:rapira:strict:v1'
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
    quote = None
    try:
        raw = await redis.get(key + ':quote')
        quote = json.loads(raw) if raw else None
        error = await redis.get(key + ':error')
        if refresh and settings.RAPIRA_RATES_ENABLED and not await redis.exists(key + ':next'):
            token = secrets.token_hex(16)
            lease = max(30, int(settings.RAPIRA_RATES_TIMEOUT_SECONDS * 4 + 15))
            if await redis.set(key + ':lock', token, nx=True, ex=lease):
                try:
                    if await redis.set(key + ':next', '1', nx=True, ex=config.parameters['refresh_seconds']):
                        try:
                            value = await rapira._fetch_strict_rolling_ask_quote()
                            quote = {'rate': str(value.rate), 'provider_field': value.provider_field,
                                     'provider_value': str(value.provider_value if value.provider_value is not None else value.rate), 'side': value.side, 'pair': value.symbol,
                                     'fetched_at': value.fetched_at.isoformat(),
                                     'provider_timestamp': value.provider_timestamp.isoformat() if value.provider_timestamp else None}
                            await redis.set(key + ':quote', json.dumps(quote), ex=604800)
                            await redis.delete(key + ':error')
                            error = None
                        except rapira.RollingRateUnavailable:
                            error = 'rapira_unavailable'
                            await redis.set(key + ':error', error, ex=604800)
                finally:
                    await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, key + ':lock', token)
        return {'quote': quote, 'last_error': error or (None if settings.RAPIRA_RATES_ENABLED else 'rapira_disabled')}
    except (RedisError, ValueError, TypeError):
        return {'quote': quote, 'last_error': 'rate_cache_unavailable'}
    finally:
        await redis.aclose()


async def parser_state(db, parser_id, *, refresh=False, persist=True):
    if parser_id == 'rapira_green_tjs':
        # Never fall through to Rapira RUB, XE or Binance, even when a DB row is active.
        return {'configuration': {'parser_id': parser_id, 'country': 'TJ', 'active': False,
                'parameters': PARSER_POLICY[parser_id]}, 'available': False, 'status': 'blocked',
                'last_error': 'tj_contract_unconfirmed', 'quote': None}
    try:
        config = await configuration(db, parser_id)
    except CrossBorderError:
        return {'configuration': {'parser_id': parser_id, 'active': False, 'parameters': PARSER_POLICY.get(parser_id, {})},
                'available': False, 'status': 'unavailable', 'last_error': 'parser_configuration_missing', 'quote': None}
    state = {'configuration': public_config(config), 'available': False, 'status': 'unavailable',
             'last_error': None, 'quote': None}
    if not config.is_active:
        return {**state, 'last_error': 'parser_disabled'}
    if parser_id in binance_p2p.PROFILES:
        provider = await binance_p2p.get_state(refresh=refresh, profile=binance_p2p.PROFILES[parser_id],
            refresh_seconds=config.parameters['refresh_seconds'], max_age=config.parameters['ttl_seconds'])
    elif parser_id == 'rapira':
        provider = await _rapira_state(config, refresh)
    else:
        return {**state, 'last_error': 'local_display_parser_only'}
    quote = provider.get('quote')
    state['last_error'] = provider.get('last_error') or provider.get('code')
    if not quote:
        return state
    try:
        fetched = binance_p2p.timestamp(quote['fetched_at'])
        now = datetime.now(timezone.utc)
        expires = fetched + timedelta(seconds=config.parameters['ttl_seconds'])
        if quote.get('provider_timestamp'):
            expires = min(expires, binance_p2p.timestamp(quote['provider_timestamp']) + timedelta(seconds=config.parameters['ttl_seconds']))
        fresh = fetched <= now < expires
        if parser_id in binance_p2p.PROFILES:
            fresh = fresh and provider.get('available', False) and binance_p2p.quote_is_fresh(quote, at=now, profile=binance_p2p.PROFILES[parser_id], max_age=config.parameters['ttl_seconds'])
        else:
            fresh = fresh and settings.RAPIRA_RATES_ENABLED and quote.get('provider_field') == 'askPrice' and quote.get('pair') == 'USDT/RUB' and quote.get('side') == 'ask'
            binance_p2p.positive_decimal(quote['rate'])
        snapshot = {**quote, **PARSER_POLICY[parser_id], 'configuration_id': str(config.id),
                    'configuration_version': config.version, 'country': config.country, 'direction': config.direction,
                    'expires_at': expires.isoformat(), 'fetched_at': fetched.isoformat(), 'validation_status': 'verified'}
        state.update(available=bool(fresh), status='current' if fresh else 'stale',
                     quote=snapshot, age_seconds=int((now - fetched).total_seconds()))
        if fresh and persist:
            fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
            statement = insert(RateObservation).values(configuration_id=config.id, fingerprint=fingerprint,
                snapshot={**snapshot, 'fixed_at': now.isoformat()}, fetched_at=fetched, fixed_at=now, expires_at=expires)
            await db.execute(statement.on_conflict_do_nothing(constraint='uq_rate_observation'))
            observation = (await db.execute(select(RateObservation).where(RateObservation.configuration_id == config.id,
                RateObservation.fingerprint == fingerprint))).scalar_one()
            state['quote'] = {**observation.snapshot, 'id': str(observation.id)}
        return state
    except (ValueError, KeyError, TypeError):
        return {**state, 'available': False, 'status': 'unavailable', 'last_error': 'invalid_provider_quote'}


async def require_observation(db, parser_id):
    state = await parser_state(db, parser_id, refresh=True)
    if not state['available']:
        raise CrossBorderError('awaiting_rate: свежая подтверждённая котировка недоступна')
    return state['quote']


async def optional_observation(db, parser_id):
    # Informational providers must never delay or expire a valid financial quote.
    # The same scheduled parser refreshes their shared cache independently.
    state = await parser_state(db, parser_id, refresh=False)
    if not state['available']:
        raise CrossBorderError('Информационный курс GEL недоступен или устарел')
    return state['quote']
