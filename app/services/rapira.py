from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import settings


RAPIRA_SYMBOLS = {'USDT/RUB', 'USDTRUB', 'USDT_RUB'}
RUB_USDT_SYMBOLS = {'RUB/USDT', 'RUBUSDT', 'RUB_USDT'}
_cache_value: Decimal | None = None
_cache_expires_at: datetime | None = None
_last_success_value: Decimal | None = None
_last_success_at: datetime | None = None


@dataclass(frozen=True)
class RapiraRateQuote:
    rate_rub: Decimal
    source: str
    updated_at: datetime
    stale: bool
    cache_seconds: int


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _pick_price(row: dict[str, Any]) -> Decimal | None:
    for key in ('askPrice', 'last', 'close', 'bidPrice', 'price', 'rate'):
        price = _to_decimal(row.get(key))
        if price:
            return price
    return None


def _normalize_symbol(value: Any) -> str:
    return str(value or '').upper().replace('-', '/').strip()


def parse_rapira_rub_usdt_rate(payload: Any) -> Decimal | None:
    rows = payload.get('data') if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = rows.get('items') or rows.get('rates') or rows.get('data') or list(rows.values())
    if not isinstance(rows, list):
        return None

    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_symbol(row.get('symbol') or row.get('pair') or row.get('market'))
        price = _pick_price(row)
        if not price:
            continue
        compact = symbol.replace('/', '')
        if symbol in RAPIRA_SYMBOLS or compact in RAPIRA_SYMBOLS:
            return price
        if symbol in RUB_USDT_SYMBOLS or compact in RUB_USDT_SYMBOLS:
            return Decimal('1') / price
        base = _normalize_symbol(row.get('baseCurrency'))
        quote = _normalize_symbol(row.get('quoteCurrency'))
        if base == 'RUB' and quote == 'USDT':
            return Decimal('1') / price
        if base == 'USDT' and quote == 'RUB':
            return price
    return None


async def fetch_rapira_rub_usdt_rate() -> Decimal | None:
    global _cache_value, _cache_expires_at, _last_success_value, _last_success_at
    now = datetime.now(timezone.utc)
    if _cache_value and _cache_expires_at and _cache_expires_at > now:
        return _cache_value

    url = settings.RAPIRA_RATES_URL.strip()
    if not settings.RAPIRA_RATES_ENABLED or not url:
        return None

    timeout = httpx.Timeout(settings.RAPIRA_RATES_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(url, headers={'accept': 'application/json'})
            response.raise_for_status()
            parsed = parse_rapira_rub_usdt_rate(response.json())
    except (httpx.HTTPError, ValueError):
        return None

    if parsed and parsed > 0:
        _cache_value = parsed
        _last_success_value = parsed
        _last_success_at = now
        _cache_expires_at = now + timedelta(seconds=max(5, settings.RAPIRA_RATES_CACHE_SECONDS))
        return parsed
    return None


def _configured_fallback_rate() -> Decimal:
    rate = _to_decimal(settings.SETTLEMENT_USDT_RUB_RATE)
    return rate if rate else Decimal('100.0000')


async def get_rapira_rub_usdt_quote() -> RapiraRateQuote:
    now = datetime.now(timezone.utc)
    if _cache_value and _cache_expires_at and _cache_expires_at > now:
        return RapiraRateQuote(
            rate_rub=_cache_value,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=False,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    live_rate = await fetch_rapira_rub_usdt_rate()
    if live_rate:
        return RapiraRateQuote(
            rate_rub=live_rate,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=False,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    if _last_success_value:
        return RapiraRateQuote(
            rate_rub=_last_success_value,
            source='Rapira',
            updated_at=_last_success_at or now,
            stale=True,
            cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
        )

    return RapiraRateQuote(
        rate_rub=_configured_fallback_rate(),
        source='SETTLEMENT_USDT_RUB_RATE',
        updated_at=now,
        stale=True,
        cache_seconds=settings.RAPIRA_RATES_CACHE_SECONDS,
    )
