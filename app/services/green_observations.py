"""Provider-independent validation only; no unconfirmed endpoint or RUB fallback.

This utility does not fetch any provider quote. A transport/market contract must be
confirmed separately before these normalized values may enter financial routing.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
from uuid import UUID

from app.core.tajikistan import DIRECTION, PARSER_ID


@dataclass(frozen=True)
class GreenObservation:
    observation_id: UUID
    configuration_id: UUID
    configuration_version: int
    source_reference: str
    market_side: str
    values: tuple[Decimal, Decimal, Decimal]
    rate: Decimal
    fetched_at: datetime
    expires_at: datetime

    def snapshot(self):
        return {'id': str(self.observation_id), 'configuration_id': str(self.configuration_id),
                'configuration_version': self.configuration_version, 'source': PARSER_ID,
                'source_reference': self.source_reference, 'side': self.market_side,
                'country': 'TJ', 'direction': DIRECTION, 'pair': 'USDT/TJS',
                'unit': 'TJS per 1 USDT', 'positions': [1, 2, 3],
                'raw_values': [str(v) for v in self.values], 'rate': str(self.rate),
                'fetched_at': self.fetched_at.isoformat(), 'expires_at': self.expires_at.isoformat()}

    def require_fresh(self, at=None):
        at = at or datetime.now(timezone.utc)
        if not at.tzinfo or not self.fetched_at <= at < self.expires_at:
            raise ValueError('Котировка Bybit P2P Green отсутствует, устарела или имеет будущее время.')
        return self


def normalize_observation(*, values, positions, unit, observation_id, configuration_id,
                          configuration_version, source_reference, market_side,
                          fetched_at, ttl_seconds, now=None):
    """Validate three already-normalized comparable quotes, preserving their order.

    market_side and source_reference are mandatory provenance, not defaults or
    a claim that an arbitrary feed is Bybit P2P Green. There is no runtime endpoint
    accepting manual quotes; this function is used for contract QA only today.
    """
    if len(values) != 3 or tuple(positions) != (1, 2, 3) or unit != 'TJS per 1 USDT':
        raise ValueError('Нужны ровно котировки 1–3 одной величины TJS за 1 USDT.')
    if not source_reference or market_side not in ('buy_usdt', 'sell_usdt') or configuration_version < 1:
        raise ValueError('Требуется идентифицированная версия источника и подтверждённая сторона рынка.')
    if not fetched_at.tzinfo or not 1 <= ttl_seconds <= 120:
        raise ValueError('Нужны время с часовым поясом и ограниченный срок актуальности.')
    try:
        if any(isinstance(v, (float, bool)) for v in values):
            raise ValueError('Передавайте Decimal или десятичные строки, не float.')
        parsed = tuple(Decimal(v) for v in values)
        if any(not v.is_finite() or v <= 0 for v in parsed):
            raise ValueError('Все три котировки должны быть положительными и конечными.')
        with localcontext() as ctx:
            ctx.prec = 50
            mean = (sum(parsed) / Decimal(3)).quantize(Decimal('.000000000001'))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Некорректные значения котировок.') from exc
    if mean <= 0:
        raise ValueError('Курс меньше поддерживаемой точности.')
    result = GreenObservation(UUID(str(observation_id)), UUID(str(configuration_id)),
        configuration_version, source_reference, market_side, parsed, mean, fetched_at,
        fetched_at + timedelta(seconds=ttl_seconds))
    return result.require_fresh(now)


def base_usdt(amount_tjs, observation: GreenObservation, *, at=None):
    """Dimensionally explicit QA calculation; not an enabled payment adapter."""
    from app.core.tajikistan import validate_amount
    amount = validate_amount(amount_tjs)
    observation.require_fresh(at)
    with localcontext() as ctx:
        ctx.prec = 50
        return (amount / observation.rate).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP)
