"""Read-only documented Bybit P2P transport. No inferred green side or fallback.

The account must have permitted P2P API access. Both request/ad side and order
equivalence require external verification before this adapter can fetch a quote.
Even a verified quote does not open TJ payment admission in this release.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from uuid import uuid4

import httpx

from app.core.config import settings
from app.core.tajikistan import PARSER_ID

ENDPOINT = 'https://api.bybit.com/v5/p2p/item/online'
OFFICIAL_API_HOSTS = frozenset(('api.bybit.com', 'api.bytick.com', 'api.bybit.nl', 'api.bybit.tr',
    'api.bybit.kz', 'api.bybitgeorgia.ge', 'api.bybit.ae', 'api.bybit.eu', 'api.bybit.id', 'api.manepa.jp'))
PARSER_VERSION = 'green-avg3-v1'


class BybitUnavailable(ValueError):
    pass


def api_base_url():
    from urllib.parse import urlsplit
    value = settings.BYBIT_P2P_API_BASE_URL.rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_API_HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise BybitUnavailable('bybit_official_host_required')
    return value


async def readonly_request(key, secret, *, side=None):
    """Only API-key information or listing advertisements; never order endpoints."""
    path = '/v5/user/query-api' if side is None else '/v5/p2p/item/online'
    if side is not None and side not in ('0', '1'):
        raise BybitUnavailable('bybit_invalid_side')
    body = '' if side is None else json.dumps({'tokenId': 'USDT', 'currencyId': 'TJS',
        'side': side, 'page': '1', 'size': '10'}, separators=(',', ':'))
    timestamp, window = str(int(time.time() * 1000)), '5000'
    signature = hmac.new(secret.encode(), (timestamp + key + window + body).encode(), hashlib.sha256).hexdigest()
    headers = {'Content-Type': 'application/json', 'X-BAPI-API-KEY': key,
        'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': window, 'X-BAPI-SIGN': signature}
    try:
        async with httpx.AsyncClient(timeout=settings.BYBIT_P2P_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            response = await client.request('GET' if side is None else 'POST', api_base_url() + path,
                content=body.encode() if side is not None else None, headers=headers)
            if response.status_code != 200:
                raise BybitUnavailable(f'bybit_http_{response.status_code}')
            if len(response.content) > 2_000_000:
                raise BybitUnavailable('bybit_response_too_large')
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, BybitUnavailable):
            raise
        raise BybitUnavailable('bybit_request_failed') from None
    if not isinstance(payload, dict):
        raise BybitUnavailable('bybit_provider_error')
    code = payload.get('ret_code', payload.get('retCode'))
    if type(code) is not int or code != 0:
        # Numeric provider errors are safe. Raw provider text and headers are not reflected.
        raise BybitUnavailable(f'bybit_provider_code_{code}' if type(code) is int else 'bybit_provider_error')
    return payload


def verified_contract():
    return bool(settings.BYBIT_P2P_GREEN_REQUEST_SIDE in ('0', '1')
        and settings.BYBIT_P2P_GREEN_AD_SIDE in ('0', '1')
        and settings.BYBIT_P2P_GREEN_CONTRACT_REFERENCE.strip())


def normalize_ads(payload, *, request_side, ad_side, contract_reference,
                  fetched_at, ttl_seconds=60, now=None):
    """First three ads in the supplied order; fail rather than skip/filter/sort."""
    if request_side not in ('0', '1') or ad_side not in ('0', '1') or not contract_reference:
        raise BybitUnavailable('bybit_green_contract_unconfirmed')
    if not fetched_at.tzinfo or not 1 <= ttl_seconds <= 120:
        raise BybitUnavailable('bybit_invalid_time')
    at = now or datetime.now(timezone.utc)
    expires = fetched_at + timedelta(seconds=ttl_seconds)
    if not at.tzinfo or not fetched_at <= at < expires:
        raise BybitUnavailable('bybit_stale_quote')
    if not isinstance(payload, dict):
        raise BybitUnavailable('bybit_provider_error')
    code = payload.get('ret_code', payload.get('retCode'))
    if type(code) is not int or code != 0 or not isinstance(payload.get('result'), dict):
        raise BybitUnavailable('bybit_provider_error')
    items = payload['result'].get('items')
    if not isinstance(items, list) or len(items) < 3:
        raise BybitUnavailable('bybit_three_ads_required')
    ids, prices = [], []
    for ad in items[:3]:
        if (not isinstance(ad, dict) or ad.get('tokenId') != 'USDT' or ad.get('currencyId') != 'TJS'
                or str(ad.get('side')) != ad_side or not str(ad.get('id') or '').strip()):
            raise BybitUnavailable('bybit_wrong_ad_dimensions')
        value = ad.get('price')
        if not isinstance(value, (str, Decimal)) or len(str(value)) > 64:
            raise BybitUnavailable('bybit_invalid_price')
        try:
            price = Decimal(value)
            if not price.is_finite() or price <= 0 or price.adjusted() > 12:
                raise BybitUnavailable('bybit_invalid_price')
        except InvalidOperation as exc:
            raise BybitUnavailable('bybit_invalid_price') from exc
        ids.append(str(ad['id']))
        prices.append(price)
    if len(set(ids)) != 3:
        raise BybitUnavailable('bybit_duplicate_ad')
    with localcontext() as ctx:
        ctx.prec = 50
        rate = (sum(prices) / Decimal(3)).quantize(Decimal('0.000000000001'))
    if rate <= 0:
        raise BybitUnavailable('bybit_invalid_price')
    # Only the required provenance is retained, never advertiser contact details.
    return {'source': PARSER_ID, 'pair': 'USDT/TJS', 'unit': 'TJS per 1 USDT',
        'request_side': request_side, 'side': ad_side, 'contract_reference': contract_reference,
        'positions': [1, 2, 3], 'ad_ids': ids, 'raw_values': [str(p) for p in prices],
        'rate': str(rate), 'observation_id': str(uuid4()), 'fetched_at': fetched_at.isoformat(),
        'expires_at': expires.isoformat(), 'validation_status': 'verified',
        'ttl_seconds': ttl_seconds, 'parser_version': PARSER_VERSION}


async def fetch_quote(*, ttl_seconds=60):
    if not settings.BYBIT_P2P_ENABLED:
        raise BybitUnavailable('bybit_disabled')
    if not verified_contract():
        raise BybitUnavailable('bybit_green_contract_unconfirmed')
    key = settings.BYBIT_P2P_API_KEY.get_secret_value()
    secret = settings.BYBIT_P2P_API_SECRET.get_secret_value()
    if not key or not secret:
        raise BybitUnavailable('bybit_p2p_credentials_missing')
    started = datetime.now(timezone.utc)
    payload = await readonly_request(key, secret, side=settings.BYBIT_P2P_GREEN_REQUEST_SIDE)
    return normalize_ads(payload, request_side=settings.BYBIT_P2P_GREEN_REQUEST_SIDE,
        ad_side=settings.BYBIT_P2P_GREEN_AD_SIDE,
        contract_reference=settings.BYBIT_P2P_GREEN_CONTRACT_REFERENCE,
        fetched_at=started, ttl_seconds=ttl_seconds)


async def cached_state(config, *, refresh):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
    prefix = f'ozark:bybit:tjs:{config.id}:{config.version}'
    quote, error = None, None
    token = uuid4().hex
    try:
        raw = await redis.get(prefix + ':quote')
        quote = json.loads(raw) if raw else None
        error = await redis.get(prefix + ':error')
        if refresh and await redis.set(prefix + ':lock', token, nx=True, ex=30):
            try:
                if await redis.set(prefix + ':next', '1', nx=True, ex=config.parameters['refresh_seconds']):
                    try:
                        quote = await fetch_quote(ttl_seconds=config.parameters['ttl_seconds'])
                        await redis.set(prefix + ':quote', json.dumps(quote), ex=config.parameters['ttl_seconds'])
                        await redis.delete(prefix + ':error')
                        error = None
                    except BybitUnavailable as exc:
                        error = str(exc)
                        quote = None
                        await redis.delete(prefix + ':quote')
                        await redis.set(prefix + ':error', error, ex=120)
            finally:
                await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, prefix + ':lock', token)
        return {'quote': None if error else quote, 'last_error': error}
    except (RedisError, ValueError, TypeError):
        return {'quote': None, 'last_error': 'bybit_cache_unavailable'}
    finally:
        await redis.aclose()
