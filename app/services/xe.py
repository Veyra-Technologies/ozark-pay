"""Official XE mid-market data. Only the server talks to XE.

One coherent provider response supplies KZT/AMD/GEL per USDT. Redis coordinates
the refresh window, in-flight lease and monthly returned-rate budget across all
web/worker processes. Failed fetches never rewrite the last successful quote.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

BASE_URL = 'https://xecdapi.xe.com/v1/'
CURRENCIES = ('KZT', 'AMD', 'GEL')
ERRORS = {
    'xe_not_connected': 'XE не подключён',
    'xe_quota_not_configured': 'Не указан лимит тарифа XE',
    'xe_auth': 'XE отклонил доступ: проверьте ключи и возможности тарифа',
    'xe_demo_response': 'XE вернул демонстрационные котировки; нужен тариф с реальными рыночными данными',
    'xe_currency_unsupported': 'XE не подтвердил поддержку USDT, KZT, AMD и GEL для этого доступа',
    'xe_invalid_response': 'XE вернул некорректную котировку',
    'xe_unavailable': 'XE временно недоступен',
    'xe_rate_limited': 'Достигнут лимит запросов XE',
    'xe_monthly_quota': 'Исчерпан настроенный месячный лимит XE',
    'xe_cache_unavailable': 'Общий кэш котировок временно недоступен',
    'xe_pending': 'Котировки XE обновляются',
    'xe_stale': 'Курс XE устарел',
}


class XeUnavailable(ValueError):
    def __init__(self, code, *, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(ERRORS.get(code, ERRORS['xe_unavailable']))


def utcnow():
    return datetime.now(timezone.utc)


def connection_status():
    missing = [name for name in ('XE_API_ACCESS_ID', 'XE_API_ACCESS_KEY')
               if not getattr(settings, name).get_secret_value().strip()]
    if missing:
        return {'code': 'xe_not_connected', 'missing': missing}
    if not settings.XE_MONTHLY_RATE_LIMIT:
        return {'code': 'xe_quota_not_configured', 'missing': ['XE_MONTHLY_RATE_LIMIT']}
    return {'code': None, 'missing': []}


def refresh_seconds():
    # A normalized response may contain four rates. Reserve quota headroom for
    # discovery and a direct-pair probe; never infer a purchased live-data tier.
    budget = max(1, settings.XE_MONTHLY_RATE_LIMIT - 64)
    return max(settings.XE_REFRESH_SECONDS, math.ceil(31 * 86400 * 4 / budget))


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise XeUnavailable('xe_invalid_response') from exc


def positive(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal('1e15'):
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise XeUnavailable('xe_invalid_response') from exc


def parse_bundle(payload, base, *, fetched_at=None):
    fetched_at = fetched_at or utcnow()
    if not isinstance(payload, dict) or payload.get('from') != base or positive(payload.get('amount')) != 1:
        raise XeUnavailable('xe_invalid_response')
    provider_at = timestamp(payload.get('timestamp'))
    if provider_at > fetched_at + timedelta(seconds=5):
        raise XeUnavailable('xe_invalid_response')
    rows = payload.get('to')
    if not isinstance(rows, list):
        raise XeUnavailable('xe_invalid_response')
    quotes = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('quotecurrency') in quotes:
            raise XeUnavailable('xe_invalid_response')
        if row.get('timestamp') is not None and timestamp(row['timestamp']) != provider_at:
            raise XeUnavailable('xe_invalid_response')
        quotes[row.get('quotecurrency')] = positive(row.get('mid'))
    expected = set(CURRENCIES) | ({'USDT'} if base == 'USD' else set())
    if base not in ('USDT', 'USD') or set(quotes) != expected:
        raise XeUnavailable('xe_invalid_response')
    # XE free-trial credentials may authenticate successfully while returning
    # the same placeholder (for example 1.2345) for every counter currency.
    # KZT, AMD and GEL cannot all have an identical market value per USDT.
    if len({quotes[currency] for currency in expected}) == 1:
        raise XeUnavailable('xe_demo_response')
    with localcontext() as ctx:
        ctx.prec = 50
        divisor = quotes['USDT'] if base == 'USD' else Decimal('1')
        normalized = {currency: str(positive(quotes[currency] / divisor)) for currency in CURRENCIES}
    return {'source': 'xe', 'rate_type': 'mid-market', 'base': 'USDT',
            'rates': normalized, 'provider_base': base,
            'provider_rates': {k: str(v) for k, v in quotes.items()},
            'normalization': 'local_per_USD / USDT_per_USD' if base == 'USD' else 'direct',
            'provider_timestamp': provider_at.isoformat(), 'fetched_at': fetched_at.isoformat()}


def bundle_is_fresh(bundle, *, at=None):
    if not bundle:
        return False
    at = at or utcnow()
    try:
        if bundle.get('source') != 'xe' or bundle.get('rate_type') != 'mid-market' or bundle.get('base') != 'USDT':
            return False
        if set(bundle['rates']) != set(CURRENCIES):
            return False
        for value in bundle['rates'].values():
            positive(value)
        for key in ('provider_timestamp', 'fetched_at'):
            age = (at - timestamp(bundle[key])).total_seconds()
            if age < -5 or age > settings.XE_MAX_AGE_SECONDS:
                return False
        return True
    except (KeyError, TypeError, XeUnavailable):
        return False


async def _budget(redis, key, cost):
    now = utcnow()
    month_key = f'{key}:budget:{now:%Y-%m}'
    allowed = await redis.eval("""
      local used = tonumber(redis.call('GET', KEYS[1]) or '0')
      if used + tonumber(ARGV[1]) > tonumber(ARGV[2]) then return 0 end
      redis.call('INCRBY', KEYS[1], ARGV[1]); redis.call('EXPIRE', KEYS[1], 2764800)
      return 1
    """, 1, month_key, cost, settings.XE_MONTHLY_RATE_LIMIT)
    if not allowed:
        raise XeUnavailable('xe_monthly_quota')


async def _request(client, redis, key, endpoint, params, cost):
    await _budget(redis, key, cost)
    try:
        response = await client.get(endpoint, params=params)
        if response.status_code == 401:
            raise XeUnavailable('xe_auth')
        if response.status_code == 403:
            try:
                provider_code = response.json().get('code')
            except (ValueError, AttributeError, TypeError):
                provider_code = None
            raise XeUnavailable('xe_monthly_quota' if provider_code == 3 else 'xe_auth')
        if response.status_code == 429:
            retry = response.headers.get('Retry-After', '')
            reset = response.headers.get('X-RateLimit-Reset', '')
            if retry.isdigit():
                retry_after = int(retry)
            elif reset.isdigit():
                retry_after = max(1, int(reset) - int(utcnow().timestamp()))
            else:
                retry_after = 3600
            raise XeUnavailable('xe_rate_limited', retry_after=min(86400, retry_after))
        if response.status_code in (400, 404, 422):
            raise XeUnavailable('xe_currency_unsupported')
        response.raise_for_status()
        return json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
    except (httpx.HTTPError, UnicodeError) as exc:
        # Never propagate request headers, URLs with credentials, bodies or keys.
        raise XeUnavailable('xe_unavailable') from exc
    except (ValueError, TypeError) as exc:
        if isinstance(exc, XeUnavailable):
            raise
        raise XeUnavailable('xe_invalid_response') from exc


async def _fetch_bundle(redis, key):
    auth = httpx.BasicAuth(settings.XE_API_ACCESS_ID.get_secret_value(), settings.XE_API_ACCESS_KEY.get_secret_value())
    async with httpx.AsyncClient(base_url=BASE_URL, auth=auth, timeout=settings.XE_TIMEOUT_SECONDS,
                                 follow_redirects=False) as client:
        supported = await redis.get(key + ':currencies')
        if supported is None:
            payload = await _request(client, redis, key, 'currencies.json/', {'crypto': 'true'}, 1)
            if not isinstance(payload, dict) or not isinstance(payload.get('currencies'), list):
                raise XeUnavailable('xe_invalid_response')
            supported = [r.get('iso') for r in payload['currencies'] if isinstance(r, dict) and r.get('is_obsolete') is False]
            await redis.set(key + ':currencies', json.dumps(supported), ex=86400)
        else:
            supported = json.loads(supported)
        if not set((*CURRENCIES, 'USDT')).issubset(supported):
            raise XeUnavailable('xe_currency_unsupported')
        mode = await redis.get(key + ':mode')
        params = {'amount': '1', 'decimal_places': '20', 'crypto': 'true'}
        if mode != 'USD':
            try:
                payload = await _request(client, redis, key, 'convert_from.json/',
                    dict(params, **{'from': 'USDT', 'to': ','.join(CURRENCIES)}), 3)
                return parse_bundle(payload, 'USDT')
            except XeUnavailable as exc:
                if exc.code != 'xe_currency_unsupported':
                    raise
        # All four rates share the same timestamp in one response. There is no
        # peg assumption and no bridge assembled from unrelated providers.
        payload = await _request(client, redis, key, 'convert_from.json/',
            dict(params, **{'from': 'USD', 'to': ','.join((*CURRENCIES, 'USDT'))}), 4)
        bundle = parse_bundle(payload, 'USD')
        await redis.set(key + ':mode', 'USD', ex=86400)
        return bundle


async def get_state(*, refresh=True):
    configured = connection_status()
    if configured['code']:
        return {'available': False, 'code': configured['code'], 'reason': ERRORS[configured['code']],
                'missing': configured['missing'], 'bundle': None}
    key = 'ozark:xe:v1:' + hashlib.sha256(settings.XE_API_ACCESS_ID.get_secret_value().encode()).hexdigest()[:16]
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
    bundle = None
    try:
        raw = await redis.get(key + ':quote')
        bundle = json.loads(raw) if raw else None
        error = await redis.get(key + ':error')
        if refresh and not await redis.exists(key + ':next'):
            token = secrets.token_hex(16)
            lease = math.ceil(settings.XE_TIMEOUT_SECONDS * 3 + 15)
            if await redis.set(key + ':lock', token, nx=True, ex=lease):
                try:
                    # Recheck after acquiring the lease to avoid a racing reader.
                    if await redis.set(key + ':next', '1', nx=True, ex=refresh_seconds()):
                        try:
                            new_bundle = await _fetch_bundle(redis, key)
                            previous_at = timestamp(bundle['provider_timestamp']) if bundle else None
                            if previous_at and timestamp(new_bundle['provider_timestamp']) < previous_at:
                                raise XeUnavailable('xe_invalid_response')
                            # A stale provider response is a failed refresh. It must
                            # never replace the last successful cached quote.
                            if not bundle_is_fresh(new_bundle):
                                raise XeUnavailable('xe_stale')
                            bundle = new_bundle
                            await redis.set(key + ':quote', json.dumps(bundle), ex=7 * 86400)
                            await redis.delete(key + ':error')
                            error = None
                        except XeUnavailable as exc:
                            error = exc.code
                            await redis.set(key + ':error', error, ex=7 * 86400)
                            if exc.retry_after:
                                await redis.expire(key + ':next', max(refresh_seconds(), exc.retry_after))
                finally:
                    await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, key + ':lock', token)
        fresh = bundle_is_fresh(bundle)
        code = None if fresh else (error or ('xe_stale' if bundle else 'xe_pending'))
        return {'available': fresh, 'code': code, 'reason': ERRORS.get(code, 'Свежая котировка XE'),
                'bundle': bundle, 'refresh_seconds': refresh_seconds(),
                'next_refresh_seconds': max(0, await redis.ttl(key + ':next')), 'last_error': error}
    except (RedisError, ValueError, TypeError, KeyError):
        return {'available': False, 'code': 'xe_cache_unavailable', 'reason': ERRORS['xe_cache_unavailable'], 'bundle': bundle}
    finally:
        await redis.aclose()


async def require_bundle():
    state = await get_state()
    if not state['available'] or not bundle_is_fresh(state['bundle']):
        raise XeUnavailable(state['code'] or 'xe_stale')
    return state['bundle']
