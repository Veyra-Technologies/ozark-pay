"""TJ calculation preparation using the existing rounding and commission rules.

No reservation, debit, webhook or manual payment route is provided here.
"""
from dataclasses import dataclass
from decimal import Decimal

from app.core.tajikistan import validate_amount
from app.services.crossborder_money import CrossBorderError, base_amount, number, usdt
from app.services.country_tariffs import tier_for
from app.services.fee_tiers import fee_amount


@dataclass(frozen=True)
class TjCalculation:
    amount_tjs: Decimal
    base_usdt: Decimal
    commission_tjs: Decimal
    commission_usdt: Decimal
    plan_id: str
    plan_version: int
    observation_id: str


def calculation(*, amount, plan, cabinet_id, quote):
    amount = validate_amount(amount)
    if (not plan or str(plan.entity_id) != str(cabinet_id) or plan.country != 'TJ'
            or plan.currency != 'TJS' or plan.direction != 'tj-tj' or plan.basis != 'local_amount'):
        raise CrossBorderError('Нужен индивидуальный тариф этого кабинета TJ')
    if (quote.get('country') != 'TJ' or quote.get('pair') != 'USDT/TJS'
            or quote.get('unit') != 'TJS per 1 USDT' or quote.get('validation_status') != 'verified'
            or quote.get('source') not in ('BYBIT_P2P', 'bybit_p2p_tjs_green_avg3') or not quote.get('id')):
        raise CrossBorderError('Нужен подтверждённый снимок Bybit TJS/USDT')
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if not plan.is_active or plan.effective_from > now:
        raise CrossBorderError('Индивидуальный тариф ещё не действует')
    fetched = datetime.fromisoformat(quote['fetched_at'])
    expires = datetime.fromisoformat(quote['expires_at'])
    if not fetched.tzinfo or not expires.tzinfo or not fetched <= now < expires:
        raise CrossBorderError('Котировка отсутствует или устарела')
    rate = number(quote['rate'])
    if rate <= 0:
        raise CrossBorderError('Некорректный курс')
    tier = tier_for(plan.tiers, amount, basis='local_amount')
    fee = fee_amount(amount, Decimal(tier['percent']))
    return TjCalculation(amount, base_amount(amount, rate), fee, usdt(fee/rate),
                         str(plan.id), plan.version, str(quote['id']))
