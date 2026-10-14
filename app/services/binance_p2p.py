"""Shared Binance P2P quote for the Kazakhstan merchant side of kz-ge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

PARSER_ID = 'binance_p2p_kaspi_buy_100000_avg10'
PARSER_LABEL = 'Binance P2P · Kaspi Bank · Покупка USDT · 100 000 ₸ · Среднее 10'
ASSET = 'USDT'
FIAT = 'KZT'
TRADE_TYPE = 'BUY'
PAY_TYPE = 'KaspiBank'
FILTER_AMOUNT = Decimal('100000')
TARGET_AD_COUNT = 10

GEL_PARSER_ID = 'binance_p2p_tbc_buy_500_avg10'

@dataclass(frozen=True)
class Profile:
    parser_id: str
    label: str
    fiat: str
    payment_method: str
    amount: Decimal

KZT_PROFILE = Profile(PARSER_ID, PARSER_LABEL, FIAT, PAY_TYPE, FILTER_AMOUNT)
# Verified against Binance's public response on 2026-10-11: identifier TBCbank,
# tradeMethodName TBC Bank. Case is significant; no bank-name fallback.
GEL_PROFILE = Profile(GEL_PARSER_ID, 'Binance P2P · TBC Bank · Покупка USDT · 500 GEL · Среднее 10',
                      'GEL', 'TBCbank', Decimal('500'))
PROFILES = {p.parser_id: p for p in (KZT_PROFILE, GEL_PROFILE)}
ERRORS = {
    'binance_p2p_disabled': 'Парсер Binance P2P отключён',
    'binance_p2p_invalid_response': 'Binance P2P вернул некорректный ответ',
    'binance_p2p_insufficient_ads': 'Недостаточно предложений',
    'binance_p2p_rate_limited': 'Binance P2P временно ограничил частоту запросов',
    'binance_p2p_unavailable': 'Binance P2P временно недоступен',
    'binance_p2p_cache_unavailable': 'Общий кэш Binance P2P временно недоступен',
    'binance_p2p_pending': 'Котировка Binance P2P обновляется',
    'binance_p2p_stale': 'Курс Binance P2P устарел',
}


class BinanceP2PUnavailable(ValueError):
    def __init__(self, code: str, *, actual_count: int | None = None, retry_after: int = 0):
        self.code = code
        self.actual_count = actual_count
        self.retry_after = retry_after
        reason = ERRORS.get(code, ERRORS['binance_p2p_unavailable'])
        if code == 'binance_p2p_insufficient_ads' and actual_count is not None:
            reason = f'{reason}: найдено {actual_count} из {TARGET_AD_COUNT}'
        super().__init__(reason)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BinanceP2PUnavailable('binance_p2p_invalid_response') from exc


def positive_decimal(value) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal('1e15'):
            raise ValueError()
        return number
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BinanceP2PUnavailable('binance_p2p_invalid_response') from exc


def request_payload(page: int, profile: Profile = KZT_PROFILE) -> dict:
    return {
        'asset': ASSET,
        'fiat': profile.fiat,
        'tradeType': TRADE_TYPE,
        'payTypes': [profile.payment_method],
        'transAmount': str(profile.amount),
        'page': page,
        'rows': settings.BINANCE_P2P_PAGE_SIZE,
        'countries': [],
        'proMerchantAds': False,
        'publisherType': None,
    }


def parse_page(payload: dict, seen_ids: set[str] | None = None, profile: Profile = KZT_PROFILE) -> list[dict]:
    """Return suitable ads in provider order; malformed individual ads are skipped."""
    if (not isinstance(payload, dict) or payload.get('success') is not True
            or str(payload.get('code')) != '000000' or not isinstance(payload.get('data'), list)):
        raise BinanceP2PUnavailable('binance_p2p_invalid_response')
    seen_ids = seen_ids if seen_ids is not None else set()
    accepted = []
    for row in payload['data']:
        if not isinstance(row, dict) or not isinstance(row.get('adv'), dict):
            continue
        adv = row['adv']
        # Search BUY is the taker's side. The advertiser must sell USDT.
        if (adv.get('asset') != ASSET or adv.get('fiatUnit') != profile.fiat
                or adv.get('tradeType') != 'SELL'
                or adv.get('advStatus') not in (None, 1, '1', 'ONLINE')
                or adv.get('isTradable') is not True):
            continue
        ad_id = str(adv.get('advNo') or '').strip()
        if not ad_id or ad_id in seen_ids:
            continue
        try:
            price = positive_decimal(adv.get('price'))
            minimum = positive_decimal(adv.get('minSingleTransAmount'))
            maximum = positive_decimal(
                adv.get('dynamicMaxSingleTransAmount') or adv.get('maxSingleTransAmount')
            )
            surplus = positive_decimal(adv.get('surplusAmount'))
        except BinanceP2PUnavailable:
            continue
        methods = []
        for method in adv.get('tradeMethods') or []:
            if isinstance(method, dict):
                identifier = str(method.get('identifier') or '').strip()
                if identifier and identifier not in methods:
                    methods.append(identifier)
        if (profile.payment_method not in methods or minimum > profile.amount or maximum < profile.amount
                or surplus * price < profile.amount):
            continue
        seen_ids.add(ad_id)
        accepted.append({
            'asset': ASSET, 'fiat': profile.fiat, 'advertiser_side': 'SELL', 'active': True,
            'is_tradable': True, 'provider_status': str(adv['advStatus']) if adv.get('advStatus') is not None else None,
            'id': ad_id,
            'price': str(price),
            'min_amount': str(minimum),
            'max_amount': str(maximum),
            'surplus_usdt': str(surplus),
            'payment_methods': methods,
        })
    return accepted


def build_quote(ads: list[dict], *, fetched_at: datetime | None = None, profile: Profile = KZT_PROFILE) -> dict:
    if len(ads) < TARGET_AD_COUNT:
        raise BinanceP2PUnavailable('binance_p2p_insufficient_ads', actual_count=len(ads))
    selected = ads[:TARGET_AD_COUNT]
    with localcontext() as context:
        context.prec = 50
        rate = sum((positive_decimal(ad['price']) for ad in selected), Decimal('0')) / Decimal(TARGET_AD_COUNT)
    return {
        'source': 'binance_p2p',
        'parser_id': profile.parser_id,
        'parser_label': profile.label,
        'rate_type': 'simple_mean_first_10_suitable_ads',
        'asset': ASSET,
        'fiat': profile.fiat,
        'trade_type': TRADE_TYPE,
        'user_operation': 'buy_usdt_for_' + profile.fiat.lower(),
        'payment_method': profile.payment_method,
        'amount_filter': str(profile.amount),
        'rate': str(rate),
        'unit': profile.fiat + ' per 1 USDT',
        'ad_count': TARGET_AD_COUNT,
        'ads': selected,
        'request': {
            'asset': ASSET,
            'fiat': profile.fiat,
            'tradeType': TRADE_TYPE,
            'payTypes': [profile.payment_method],
            'transAmount': str(profile.amount),
            'proMerchantAds': False,
            'publisherType': None,
        },
        # The current response has no trustworthy market timestamp. Do not
        # synthesize one; fetched_at is explicitly our server observation.
        'provider_timestamp': None,
        'fetched_at': (fetched_at or utcnow()).isoformat(),
    }


def quote_is_fresh(quote: dict | None, *, at: datetime | None = None, max_age: int | None = None, profile: Profile = KZT_PROFILE) -> bool:
    if not quote:
        return False
    at = at or utcnow()
    try:
        if (quote.get('source') != 'binance_p2p' or quote.get('parser_id') != profile.parser_id
                or quote.get('asset') != ASSET or quote.get('fiat') != profile.fiat
                or quote.get('trade_type') != TRADE_TYPE or quote.get('payment_method') != profile.payment_method
                or quote.get('amount_filter') != str(profile.amount)
                or quote.get('unit') != profile.fiat + ' per 1 USDT'
                or quote.get('request', {}).get('payTypes') != [profile.payment_method]
                or quote.get('request', {}).get('transAmount') != str(profile.amount)
                or quote.get('request', {}).get('tradeType') != TRADE_TYPE
                or quote.get('request', {}).get('fiat') != profile.fiat
                or quote.get('provider_timestamp') is not None
                or quote.get('ad_count') != TARGET_AD_COUNT or len(quote.get('ads', [])) != TARGET_AD_COUNT):
            return False
        ids = [str(ad['id']) for ad in quote['ads']]
        if len(set(ids)) != TARGET_AD_COUNT:
            return False
        for ad in quote['ads']:
            if (ad.get('asset') != ASSET or ad.get('fiat') != profile.fiat
                    or ad.get('advertiser_side') != 'SELL' or ad.get('active') is not True
                    or ad.get('is_tradable') is not True
                    or ad.get('provider_status') not in (None, '1', 'ONLINE')):
                return False
            price = positive_decimal(ad['price'])
            minimum = positive_decimal(ad['min_amount'])
            maximum = positive_decimal(ad['max_amount'])
            surplus = positive_decimal(ad['surplus_usdt'])
            if (profile.payment_method not in ad['payment_methods'] or minimum > profile.amount
                    or maximum < profile.amount or surplus * price < profile.amount):
                return False
        with localcontext() as context:
            context.prec = 50
            expected = sum((positive_decimal(ad['price']) for ad in quote['ads']), Decimal('0')) / Decimal(TARGET_AD_COUNT)
        if positive_decimal(quote['rate']) != expected:
            return False
        age = (at - timestamp(quote['fetched_at'])).total_seconds()
        return 0 <= age <= (max_age if max_age is not None else settings.BINANCE_P2P_MAX_AGE_SECONDS)
    except (KeyError, TypeError, BinanceP2PUnavailable):
        return False


def _retry_after(response: httpx.Response) -> int:
    raw = response.headers.get('Retry-After', '')
    return min(86400, max(1, int(raw))) if raw.isdigit() else settings.BINANCE_P2P_RETRY_AFTER_MAX_SECONDS


async def _post_page(client: httpx.AsyncClient, page: int, profile: Profile = KZT_PROFILE) -> dict:
    attempts = settings.BINANCE_P2P_RETRIES + 1
    for attempt in range(attempts):
        try:
            response = await client.post(settings.BINANCE_P2P_URL, json=request_payload(page, profile))
            if response.status_code in (401, 403, 451):
                raise BinanceP2PUnavailable('binance_p2p_unavailable')
            if response.status_code == 429:
                delay = _retry_after(response)
                if attempt + 1 < attempts and delay <= settings.BINANCE_P2P_RETRY_AFTER_MAX_SECONDS:
                    await asyncio.sleep(delay)
                    continue
                raise BinanceP2PUnavailable('binance_p2p_rate_limited', retry_after=delay)
            response.raise_for_status()
            try:
                return json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
            except (ValueError, TypeError, UnicodeError) as exc:
                raise BinanceP2PUnavailable('binance_p2p_invalid_response') from exc
        except BinanceP2PUnavailable:
            raise
        except httpx.HTTPError as exc:
            if attempt + 1 >= attempts:
                raise BinanceP2PUnavailable('binance_p2p_unavailable') from exc
            await asyncio.sleep(settings.BINANCE_P2P_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    raise BinanceP2PUnavailable('binance_p2p_unavailable')


async def _fetch_quote(profile: Profile = KZT_PROFILE) -> dict:
    ads: list[dict] = []
    seen_ids: set[str] = set()
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
               'User-Agent': 'Mozilla/5.0 (compatible; OzarkPay/1.0)'}
    async with httpx.AsyncClient(timeout=settings.BINANCE_P2P_TIMEOUT_SECONDS,
                                 follow_redirects=False, headers=headers) as client:
        for page in range(1, settings.BINANCE_P2P_MAX_PAGES + 1):
            payload = await _post_page(client, page) if profile == KZT_PROFILE else await _post_page(client, page, profile)
            ads.extend(parse_page(payload, seen_ids, profile))
            if len(ads) >= TARGET_AD_COUNT:
                return build_quote(ads, profile=profile)
            if not payload['data']:
                break
    raise BinanceP2PUnavailable('binance_p2p_insufficient_ads', actual_count=len(ads))


def _error_payload(exc: BinanceP2PUnavailable) -> str:
    return json.dumps({'code': exc.code, 'actual_count': exc.actual_count,
                       'retry_after': exc.retry_after})


def _decode_error(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {'code': str(data)}
    except (ValueError, TypeError):
        return {'code': str(raw)}


def _reason(code: str | None, actual_count: int | None = None) -> str:
    reason = ERRORS.get(code, 'Свежая котировка Binance P2P')
    if code == 'binance_p2p_insufficient_ads' and actual_count is not None:
        return f'{reason}: найдено {actual_count} из {TARGET_AD_COUNT}'
    return reason


async def get_state(*, refresh: bool = True, refresh_seconds: int | None = None,
                    max_age: int | None = None, profile: Profile = KZT_PROFILE) -> dict:
    refresh_seconds = refresh_seconds or settings.BINANCE_P2P_REFRESH_SECONDS
    max_age = max_age or settings.BINANCE_P2P_MAX_AGE_SECONDS
    if not settings.BINANCE_P2P_ENABLED:
        return {'available': False, 'status': 'unavailable', 'code': 'binance_p2p_disabled',
                'reason': ERRORS['binance_p2p_disabled'], 'quote': None, 'actual_count': 0}
    endpoint_scope = hashlib.sha256(
        settings.BINANCE_P2P_URL.encode('utf-8')).hexdigest()[:12]
    key = f'ozark:binance-p2p:v2:{profile.parser_id}:{endpoint_scope}'
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True,
                           socket_connect_timeout=2, socket_timeout=3)
    quote = None
    try:
        raw = await redis.get(key + ':quote')
        quote = json.loads(raw) if raw else None
        error = _decode_error(await redis.get(key + ':error'))
        if refresh and not await redis.exists(key + ':next'):
            token = secrets.token_hex(16)
            lease = max(15, math.ceil(settings.BINANCE_P2P_MAX_PAGES * (
                settings.BINANCE_P2P_TIMEOUT_SECONDS * (settings.BINANCE_P2P_RETRIES + 1)
                + settings.BINANCE_P2P_RETRY_AFTER_MAX_SECONDS * settings.BINANCE_P2P_RETRIES
                + settings.BINANCE_P2P_RETRY_BACKOFF_SECONDS
                * sum(2 ** attempt for attempt in range(settings.BINANCE_P2P_RETRIES))
                + 15
            )))
            if await redis.set(key + ':lock', token, nx=True, ex=lease):
                try:
                    if await redis.set(key + ':next', '1', nx=True,
                                       ex=refresh_seconds):
                        try:
                            new_quote = await _fetch_quote() if profile == KZT_PROFILE else await _fetch_quote(profile)
                            if not quote_is_fresh(new_quote, max_age=max_age, profile=profile):
                                raise BinanceP2PUnavailable('binance_p2p_stale')
                            quote = new_quote
                            await redis.set(key + ':quote', json.dumps(quote), ex=7 * 86400)
                            await redis.delete(key + ':error')
                            error = None
                        except BinanceP2PUnavailable as exc:
                            error = {'code': exc.code, 'actual_count': exc.actual_count,
                                     'retry_after': exc.retry_after}
                            await redis.set(key + ':error', _error_payload(exc), ex=7 * 86400)
                            if exc.retry_after:
                                await redis.expire(key + ':next', max(
                                    refresh_seconds, exc.retry_after))
                finally:
                    await redis.eval(
                        "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
                        1, key + ':lock', token)
        fresh = quote_is_fresh(quote, max_age=max_age, profile=profile)
        code = None if fresh else ((error or {}).get('code') or
                                   ('binance_p2p_stale' if quote else 'binance_p2p_pending'))
        count = ((error or {}).get('actual_count')
                 if (error or {}).get('actual_count') is not None
                 else (quote.get('ad_count') if quote else None))
        return {'available': fresh, 'status': 'current' if fresh else ('stale' if quote else 'unavailable'),
                'code': code, 'reason': _reason(code, (error or {}).get('actual_count')),
                'quote': quote, 'actual_count': count,
                'refresh_seconds': refresh_seconds,
                'max_age_seconds': max_age,
                'next_refresh_seconds': max(0, await redis.ttl(key + ':next')),
                'last_error': error}
    except (RedisError, ValueError, TypeError, KeyError):
        return {'available': False, 'status': 'stale' if quote else 'unavailable',
                'code': 'binance_p2p_cache_unavailable',
                'reason': ERRORS['binance_p2p_cache_unavailable'], 'quote': quote,
                'actual_count': quote.get('ad_count') if quote else None}
    finally:
        await redis.aclose()


async def require_quote() -> dict:
    state = await get_state()
    if not state['available'] or not quote_is_fresh(state['quote']):
        error = state.get('last_error') or {}
        raise BinanceP2PUnavailable(state['code'] or 'binance_p2p_stale',
                                    actual_count=error.get('actual_count'),
                                    retry_after=error.get('retry_after') or 0)
    return state['quote']
