"""Read-only financial preview built on existing profiles and versioned FeeRule.

Persisting an immutable quote does not create an order, reserve collateral, post
ledger entries or enqueue a webhook. Payment routing remains disabled.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from sqlalchemy import select, or_

from app.core.config import settings
from app.models import (CrossBorderQuote, Merchant, User, TeamLeadTraderAssignment,
                        TeamLeadMerchantAssignment)
from app.services.crossborder import DIRECTIONS
from app.services.fee_tiers import resolve_fee_rule, validate_margin, FeeConfigurationError
from app.services import binance_p2p, xe


class CrossBorderUnavailable(ValueError):
    def __init__(self, code, reason):
        self.code = code
        super().__init__(reason)


def amount_kzt(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999999.99') or amount != amount.quantize(Decimal('.01')):
            raise ValueError()
        return amount
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CrossBorderUnavailable('invalid_amount', 'Укажите положительную сумму KZT, не более двух знаков после запятой') from exc


def calculate(amount, currency, bundle, merchant_rule, trader_rule, *, at=None,
              merchant_quote=None):
    at = at or xe.utcnow()
    if not xe.bundle_is_fresh(bundle, at=at):
        raise xe.XeUnavailable('xe_stale')
    if currency not in ('AMD', 'GEL'):
        raise CrossBorderUnavailable('invalid_currency', 'Неизвестная валюта приёма')
    validate_margin(merchant_rule, trader_rule)
    with localcontext() as ctx:
        ctx.prec = 50
        amount = amount_kzt(amount)
        if merchant_quote is not None and not binance_p2p.quote_is_fresh(merchant_quote, at=at):
            raise binance_p2p.BinanceP2PUnavailable('binance_p2p_stale')
        sending_rate = (binance_p2p.positive_decimal(merchant_quote['rate'])
                        if merchant_quote is not None else xe.positive(bundle['rates']['KZT']))
        receiving_rate = xe.positive(bundle['rates'][currency])
        equivalent = amount / sending_rate
        receiving = equivalent * receiving_rate
        usdt = lambda value: value.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)
        gross = usdt(equivalent)
        merchant_fee = usdt(equivalent * Decimal(merchant_rule.rate_percent) / 100)
        trader_fee = usdt(equivalent * Decimal(trader_rule.rate_percent) / 100)
        if gross <= 0:
            raise CrossBorderUnavailable('amount_too_small', 'Сумма меньше точности расчёта USDT')
        tariffs = []
        for rule in (merchant_rule, trader_rule):
            tariffs.append({'rule_id': str(rule.id), 'version': rule.version,
                'entity_type': rule.entity_type, 'entity_id': str(rule.entity_id),
                'fee_side': rule.fee_side, 'direction': rule.direction,
                'currency': rule.currency, 'payment_method': rule.payment_method,
                'rate_percent': str(rule.rate_percent), 'min_amount': str(rule.min_amount),
                'max_amount': str(rule.max_amount) if rule.max_amount is not None else None,
                'effective_from': rule.effective_from.isoformat(),
                'effective_to': rule.effective_to.isoformat() if rule.effective_to else None})
        kzt_quote = ({
            'rate': merchant_quote['rate'],
            'unit': merchant_quote['unit'],
            'source': merchant_quote['source'],
            'rate_type': merchant_quote['rate_type'],
            'parser_id': merchant_quote['parser_id'],
            'parser_label': merchant_quote['parser_label'],
            'provider_timestamp': merchant_quote.get('provider_timestamp'),
            'fetched_at': merchant_quote['fetched_at'],
            'ad_count': merchant_quote['ad_count'],
        } if merchant_quote is not None else {
            'rate': bundle['rates']['KZT'], 'unit': 'KZT per 1 USDT',
            'source': 'xe', 'rate_type': 'mid-market',
            'provider_timestamp': bundle['provider_timestamp'],
            'fetched_at': bundle['fetched_at'],
        })
        receiving_quote = {
            'rate': bundle['rates'][currency], 'unit': f'{currency} per 1 USDT',
            'source': 'xe', 'rate_type': 'mid-market',
            'provider_timestamp': bundle['provider_timestamp'],
            'fetched_at': bundle['fetched_at'],
        }
        return {'version': 2 if merchant_quote is not None else 1,
            'pay_currency': 'KZT', 'pay_amount': str(amount),
            'settlement_currency': 'USDT', 'collateral_currency': 'USDT',
            'receive_currency': currency, 'usdt_equivalent_unrounded': str(equivalent),
            'receive_amount_unrounded': str(receiving),
            'usdt_equivalent': str(gross), 'receive_amount': str(receiving.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
            'merchant_fee_usdt': str(merchant_fee), 'trader_fee_usdt': str(trader_fee),
            'merchant_receivable_usdt': str(gross - merchant_fee),
            'platform_income_usdt': str(merchant_fee - trader_fee),
            'tariffs': tariffs, 'fee_base': 'unrounded_usdt_equivalent',
            'rounding': {'mode': 'ROUND_HALF_UP', 'KZT': '0.01', 'AMD': '0.01', 'GEL': '0.01', 'USDT': '0.000001', 'decimal_precision': 50},
            'quotes': {'KZT': kzt_quote, currency: receiving_quote},
            'provider_evidence': ({'merchant': deepcopy(merchant_quote),
                                   'receiving': deepcopy(bundle)}
                                  if merchant_quote is not None else deepcopy(bundle)),
            'bank_route_ready': False, 'funds_reserved': False,
            'referrals': [], 'payment_creation_enabled': False}


async def create_preview(db, *, actor, direction_id, merchant_id, trader_id, amount):
    if actor.role != 'superadmin':
        raise CrossBorderUnavailable('forbidden', 'Недостаточно прав для расчёта направления')
    direction = next((r for r in DIRECTIONS if r['id'] == direction_id), None)
    if not direction:
        raise CrossBorderUnavailable('invalid_direction', 'Выберите направление')
    amount = amount_kzt(amount)
    merchant = await db.get(Merchant, merchant_id)
    owner = await db.get(User, merchant.owner_id) if merchant else None
    trader = await db.get(User, trader_id)
    active = lambda u: u and u.is_active and not u.is_locked and not u.is_archived
    if not merchant or merchant.is_archived or not active(owner) or owner.role != 'merchant':
        raise CrossBorderUnavailable('merchant_unavailable', 'Мерчант недоступен')
    if not active(trader) or trader.role != 'trader' or trader.working_country != direction['trader_country'] or trader.working_currency != direction['receive_currency'] or trader.working_rate_source != 'xe':
        raise CrossBorderUnavailable('trader_profile_mismatch', 'Выберите активного трейдера соответствующей страны с источником XE')
    now = xe.utcnow()
    merchant_quote = None
    rate_setting = None
    if direction_id == 'kz-ge':
        from app.services.crossborder_rate_settings import get_setting
        rate_setting = await get_setting(db, merchant.id, direction_id)
        if rate_setting is None:
            raise CrossBorderUnavailable(
                'merchant_rate_parser_required',
                'Назначьте мерчанту совместимый парсер курса для направления Казахстан → Грузия',
            )
        if (rate_setting.parser_id != binance_p2p.PARSER_ID
                or rate_setting.merchant_country != 'KZ'
                or rate_setting.destination_country != 'GE'
                or rate_setting.pay_currency != 'KZT'):
            raise CrossBorderUnavailable(
                'merchant_rate_parser_incompatible',
                'Назначенный парсер несовместим со страной, валютой или направлением',
            )
        merchant_quote = await binance_p2p.require_quote()
    bundle = await xe.require_bundle()
    with localcontext() as ctx:
        ctx.prec = 50
        sending_rate = (binance_p2p.positive_decimal(merchant_quote['rate'])
                        if merchant_quote is not None else xe.positive(bundle['rates']['KZT']))
        equivalent = amount / sending_rate
    try:
        rules = [await resolve_fee_rule(db, entity_type=kind, entity_id=identity,
            fee_side=side, payment_method='bank_transfer', currency='USDT',
            direction=direction_id, amount=equivalent, at=now)
            for kind, identity, side in [('merchant', merchant.id, 'merchant_fee'), ('trader', trader.id, 'executor_fee')]]
    except FeeConfigurationError as exc:
        raise CrossBorderUnavailable('crossborder_tariff_missing', 'Нужны отдельные действующие тарифы мерчанта и трейдера в USDT для этого направления; тарифы RUB не применяются') from exc
    # Existing referral assignments settle in RUB. Applying those percentages in
    # USDT implicitly would migrate a tariff. Block until explicit scoped terms
    # exist, rather than silently omitting a referral or inventing new shares.
    trader_ref = await db.scalar(select(TeamLeadTraderAssignment.id).where(
        TeamLeadTraderAssignment.trader_id == trader.id,
        TeamLeadTraderAssignment.effective_from <= now,
        or_(TeamLeadTraderAssignment.effective_to.is_(None), TeamLeadTraderAssignment.effective_to > now)).limit(1))
    merchant_ref = await db.scalar(select(TeamLeadMerchantAssignment.id).where(
        TeamLeadMerchantAssignment.merchant_id == merchant.id,
        TeamLeadMerchantAssignment.valid_from <= now,
        or_(TeamLeadMerchantAssignment.valid_to.is_(None), TeamLeadMerchantAssignment.valid_to > now)).limit(1))
    if trader_ref or merchant_ref:
        raise CrossBorderUnavailable('crossborder_referral_terms_required', 'Участнику назначен TeamLead с тарифом RUB. Для трансграна нужны отдельные условия распределения USDT')
    snapshot = calculate(amount, direction['receive_currency'], bundle, *rules,
                         at=now, merchant_quote=merchant_quote)
    expiry_candidates = [now + timedelta(seconds=settings.CROSSBORDER_QUOTE_TTL_SECONDS),
        xe.timestamp(bundle['provider_timestamp']) + timedelta(seconds=settings.XE_MAX_AGE_SECONDS),
        xe.timestamp(bundle['fetched_at']) + timedelta(seconds=settings.XE_MAX_AGE_SECONDS),
        *(r.effective_to for r in rules if r.effective_to)]
    if merchant_quote is not None:
        expiry_candidates.append(
            binance_p2p.timestamp(merchant_quote['fetched_at'])
            + timedelta(seconds=settings.BINANCE_P2P_MAX_AGE_SECONDS))
    expiry = min(expiry_candidates)
    if expiry <= xe.utcnow():
        raise xe.XeUnavailable('xe_stale')
    snapshot.update(direction=direction_id, created_at=now.isoformat(), expires_at=expiry.isoformat(),
                    merchant_id=str(merchant.id), trader_id=str(trader.id))
    if rate_setting is not None:
        snapshot['merchant_rate_setting'] = {
            'setting_id': str(rate_setting.id),
            'direction': rate_setting.direction,
            'merchant_country': rate_setting.merchant_country,
            'destination_country': rate_setting.destination_country,
            'pay_currency': rate_setting.pay_currency,
            'parser_id': rate_setting.parser_id,
        }
    row = CrossBorderQuote(actor_id=actor.id, merchant_id=merchant.id, trader_id=trader.id,
        direction=direction_id, snapshot=snapshot, created_at=now, expires_at=expiry)
    db.add(row)
    await db.flush()
    return row


def public_quote(row):
    return {'available': True, 'quote_id': str(row.id), 'expired': row.expires_at <= xe.utcnow(),
            'snapshot': deepcopy(row.snapshot), 'bank_route_ready': False}
