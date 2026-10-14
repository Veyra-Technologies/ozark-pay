from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.config import settings
from app.core.settlement_policy import (
    SETTLEMENT_ACTIVE_STATUSES,
    SETTLEMENT_CURRENCIES,
    SETTLEMENT_FEE_USDT,
    SETTLEMENT_NETWORK,
)
from app.core.tron import TronAddressError, validate_trc20_address
from app.models import (
    AuditLog,
    Balance,
    LedgerEntry,
    Merchant,
    MerchantSettlement,
    PlatformLedgerEntry,
    User,
)
from app.services.audit import audit
from app.services.ledger import (
    LedgerError,
    debit_frozen,
    hold,
    release_hold,
)
from app.services.rapira import (
    RollingRapiraQuote,
    RollingRateUnavailable,
    get_strict_rolling_ask_quote,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

SETTLEMENT_STATUS_PENDING = 'pending'
SETTLEMENT_STATUS_COMPLETED = 'completed'
SETTLEMENT_STATUS_REJECTED = 'rejected'
TWOPLACES = Decimal('0.01')
FOURPLACES = Decimal('0.0001')
MAX_IDEMPOTENCY_KEY_LENGTH = 180
MAX_TX_HASH_LENGTH = 160
FUTURE_SKEW = timedelta(seconds=5)


class MerchantSettlementError(ValueError):
    code = 'merchant_settlement_error'

    def __init__(self, code: str | None = None):
        self.code = code or self.code
        super().__init__(self.code)


class MerchantSettlementRateUnavailable(MerchantSettlementError):
    code = 'merchant_settlement_rate_unavailable'


class MerchantSettlementConflict(MerchantSettlementError):
    code = 'merchant_settlement_conflict'


def money(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.00')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementError('invalid_money') from exc
    if not parsed.is_finite() or abs(parsed) >= Decimal('1e16'):
        raise MerchantSettlementError('invalid_money')
    try:
        result = parsed.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise MerchantSettlementError('invalid_money') from exc
    if abs(result) >= Decimal('1e16'):
        raise MerchantSettlementError('invalid_money')
    return result


def usdt(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.00')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementError('invalid_usdt_amount') from exc
    if not parsed.is_finite() or abs(parsed) >= Decimal('1e16'):
        raise MerchantSettlementError('invalid_usdt_amount')
    try:
        return parsed.quantize(TWOPLACES, rounding=ROUND_DOWN)
    except InvalidOperation as exc:
        raise MerchantSettlementError('invalid_usdt_amount') from exc


def rate(value: Decimal | int | str | None) -> Decimal:
    try:
        parsed = Decimal(value or '0.0000')
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MerchantSettlementRateUnavailable() from exc
    if not parsed.is_finite() or parsed <= 0:
        raise MerchantSettlementRateUnavailable()
    try:
        result = parsed.quantize(FOURPLACES, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise MerchantSettlementRateUnavailable() from exc
    if result <= 0 or result >= Decimal('1e14'):
        raise MerchantSettlementRateUnavailable()
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise MerchantSettlementRateUnavailable()
    return value.astimezone(UTC)


def _validate_live_quote(
    quote: RollingRapiraQuote,
    *,
    now: datetime,
) -> Decimal:
    if (
        quote.symbol != 'USDT/RUB'
        or quote.side != 'ask'
        or quote.source != 'rapira_live'
        or quote.provider_field != 'askPrice'
        or quote.stale
        or quote.freshness_basis not in {'provider_timestamp', 'fetched_at'}
    ):
        raise MerchantSettlementRateUnavailable()
    quote_rate = rate(quote.rate)
    fetched_at = _utc(quote.fetched_at)
    age = now - fetched_at
    max_age = timedelta(seconds=settings.ROLLING_RAPIRA_MAX_AGE_SECONDS)
    if age < -FUTURE_SKEW or age > max_age:
        raise MerchantSettlementRateUnavailable()
    if quote.freshness_basis == 'provider_timestamp':
        if quote.provider_timestamp is None:
            raise MerchantSettlementRateUnavailable()
        provider_age = fetched_at - _utc(quote.provider_timestamp)
        if provider_age < -FUTURE_SKEW or provider_age > max_age:
            raise MerchantSettlementRateUnavailable()
    elif quote.provider_timestamp is not None:
        raise MerchantSettlementRateUnavailable()
    return quote_rate


async def _live_quote() -> RollingRapiraQuote:
    try:
        return await get_strict_rolling_ask_quote()
    except RollingRateUnavailable as exc:
        raise MerchantSettlementRateUnavailable() from exc


async def settlement_rate_rub() -> tuple[Decimal, str]:
    quote = await _live_quote()
    return (
        _validate_live_quote(quote, now=datetime.now(UTC)),
        quote.source,
    )


def settlement_transfer_fee_usdt() -> Decimal:
    return SETTLEMENT_FEE_USDT


def _validated_address(address: str) -> str:
    try:
        return validate_trc20_address(address)
    except TronAddressError as exc:
        raise MerchantSettlementError(exc.code) from exc


def _idempotency_key(value: str) -> str:
    normalized = (value or '').strip()
    if not normalized:
        raise MerchantSettlementError('idempotency_key_required')
    if len(normalized) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise MerchantSettlementError('idempotency_key_too_long')
    return normalized


def _tx_hash(value: str) -> str:
    import re
    normalized = (value or '').strip().lower()
    if not normalized:
        raise MerchantSettlementError('tx_hash_required')
    if not re.fullmatch(r'[0-9a-f]{64}', normalized):
        raise MerchantSettlementError('invalid_tron_txid')
    return normalized


def _active(user):
    return user and user.is_active and not user.is_locked and not user.is_archived


def _country(merchant):
    if not merchant or merchant.working_country not in SETTLEMENT_CURRENCIES:
        raise MerchantSettlementError('merchant_settlement_country_required')
    return merchant.working_country


async def _requesting_merchant(db, merchant_id, requested_by_id):
    merchant = await db.get(Merchant, merchant_id)
    owner = await db.get(User, requested_by_id)
    if not merchant or merchant.is_archived or merchant.owner_id != requested_by_id or not _active(owner) or owner.role != 'merchant':
        raise MerchantSettlementError('permission_denied')
    return merchant


async def _admin(db, actor_id):
    actor = await db.get(User, actor_id)
    if not _active(actor) or actor.role not in ('admin', 'superadmin'):
        raise MerchantSettlementError('permission_denied')
    return actor


async def pending_settlement_total(db, merchant_id, currency='RUB'):
    total = await db.scalar(select(func.coalesce(func.sum(MerchantSettlement.reserve_amount), 0)).where(
        MerchantSettlement.merchant_id == merchant_id, MerchantSettlement.balance_currency == currency,
        MerchantSettlement.status.in_(SETTLEMENT_ACTIVE_STATUSES)))
    return money(total)


async def merchant_settlement_quote(db, merchant_id, *, allow_unavailable=False):
    merchant = await db.get(Merchant, merchant_id)
    country = merchant.working_country if merchant else None
    currency = SETTLEMENT_CURRENCIES.get(country, 'RUB')
    balance = await db.scalar(select(Balance).where(Balance.merchant_id == merchant_id, Balance.currency == currency))
    available = Decimal(balance.available if balance else 0)
    pending = await pending_settlement_total(db, merchant_id, currency)
    fee = settlement_transfer_fee_usdt()
    active_request = await db.scalar(select(MerchantSettlement.id).where(MerchantSettlement.merchant_id == merchant_id,
        MerchantSettlement.status.in_(SETTLEMENT_ACTIVE_STATUSES)).limit(1))
    result = {'country': country, 'balance_currency': currency, 'network': SETTLEMENT_NETWORK,
              'fee_usdt': fee, 'available': available, 'pending': pending, 'rate_available': False,
              'rate_rub': None, 'fee_rub': None, 'available_rub': available if currency == 'RUB' else None,
              'pending_rub': pending if currency == 'RUB' else None, 'max_request_usdt': Decimal(0),
              'rate_source': 'unavailable', 'rate_updated_at': None, 'rate_stale': True, 'freshness_basis': None,
              'requires_fx': currency == 'RUB', 'has_active_request': active_request is not None}
    if country not in SETTLEMENT_CURRENCIES:
        result['error'] = 'merchant_settlement_country_required'
        if not allow_unavailable:
            raise MerchantSettlementError(result['error'])
        return result
    if country == 'KZ':
        result.update(rate_available=True, rate_source='usdt_balance', rate_stale=False,
                      max_request_usdt=usdt(max(Decimal(0), available - fee)))
        return result
    from app.services.crossborder_money import CrossBorderError
    from app.services.parser_assignments import require_legacy_rapira_assignment
    try:
        try:
            await require_legacy_rapira_assignment(db, merchant_id)
        except CrossBorderError as exc:
            raise MerchantSettlementRateUnavailable() from exc
        quote = await _live_quote()
        current = _validate_live_quote(quote, now=datetime.now(UTC))
    except MerchantSettlementRateUnavailable:
        if not allow_unavailable:
            raise
        result.update(error='merchant_settlement_rate_unavailable', rate_rub=Decimal(0), fee_rub=Decimal(0))
        return result
    fee_rub = money(fee * current)
    result.update(rate_available=True, rate_rub=current, fee_rub=fee_rub,
                  max_request_usdt=usdt(max(Decimal(0), (available - fee_rub) / current)),
                  rate_source=quote.source, rate_updated_at=quote.updated_at,
                  rate_stale=False, freshness_basis=quote.freshness_basis)
    return result


async def settlement_preview(db, merchant_id, amount_usdt):
    quote = await merchant_settlement_quote(db, merchant_id, allow_unavailable=True)
    amount = usdt(amount_usdt)
    if amount <= 0:
        raise MerchantSettlementError('invalid_usdt_amount')
    if not quote['rate_available']:
        return {**quote, 'can_request': False}
    payout = amount if quote['balance_currency'] == 'USDT' else money(amount * quote['rate_rub'])
    fee = quote['fee_usdt'] if quote['balance_currency'] == 'USDT' else quote['fee_rub']
    total = money(payout + fee)
    return {**quote, 'amount_usdt': amount, 'payout_balance_amount': payout, 'fee_balance_amount': fee,
            'reserve_amount': total, 'total_usdt': usdt(amount + quote['fee_usdt']),
            'can_request': total <= quote['available']}


def _assert_idempotent_match(existing, *, merchant_id, requested_by_id, amount_usdt, trc20_address):
    if (existing.merchant_id != merchant_id or existing.requested_by_id != requested_by_id
        or usdt(existing.amount_usdt) != amount_usdt or existing.trc20_address != trc20_address):
        raise MerchantSettlementConflict('merchant_settlement_idempotency_mismatch')


async def _existing_idempotent(db, *, merchant_id, idempotency_key, for_update=False):
    query = select(MerchantSettlement).where(MerchantSettlement.merchant_id == merchant_id,
                                             MerchantSettlement.idempotency_key == idempotency_key)
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def _balance(db, merchant_id, currency):
    balance = await db.scalar(select(Balance).where(Balance.merchant_id == merchant_id,
        Balance.currency == currency).with_for_update().execution_options(populate_existing=True))
    if not balance:
        raise MerchantSettlementError('merchant_settlement_insufficient_available')
    return balance


async def _event(db, row, actor_id, action, previous, actor_ip=None, **details):
    await audit(db, 'merchant_settlement_' + action, 'merchant_settlement', actor_id, row.id, actor_ip,
        {'merchant_id': str(row.merchant_id), 'business_id': str(row.business_id) if row.business_id else None,
         'country': row.country, 'balance_id': str(row.balance_id) if row.balance_id else None,
         'balance_currency': row.balance_currency, 'amount_usdt': str(row.amount_usdt),
         'fee_usdt': str(row.fee_usdt), 'reserve_amount': str(row.reserve_amount),
         'previous_status': previous, 'new_status': row.status, 'tx_hash': row.tx_hash,
         'network_verified': False, **details})


async def create_merchant_settlement(db, *, merchant_id, requested_by_id, amount_usdt,
                                     trc20_address, idempotency_key, actor_ip=None, expected_reserve_amount=None):
    amount = usdt(amount_usdt)
    if amount <= 0:
        raise MerchantSettlementError('invalid_usdt_amount')
    address, idem = _validated_address(trc20_address), _idempotency_key(idempotency_key)
    merchant = await _requesting_merchant(db, merchant_id, requested_by_id)
    existing = await _existing_idempotent(db, merchant_id=merchant_id, idempotency_key=idem)
    if existing:
        _assert_idempotent_match(existing, merchant_id=merchant_id, requested_by_id=requested_by_id,
                                 amount_usdt=amount, trc20_address=address)
        return existing
    country = _country(merchant)
    currency, fee = SETTLEMENT_CURRENCIES[country], settlement_transfer_fee_usdt()
    quote = None
    quoted_at = datetime.now(UTC)
    if country == 'RU':
        from app.services.crossborder_money import CrossBorderError
        from app.services.parser_assignments import require_legacy_rapira_assignment
        try:
            await require_legacy_rapira_assignment(db, merchant_id)
        except CrossBorderError as exc:
            raise MerchantSettlementRateUnavailable() from exc
        # Provider I/O finishes before the first financial row lock.
        quote = await _live_quote()
        quoted_at = datetime.now(UTC)
        rub_rate = _validate_live_quote(quote, now=quoted_at)
        payout_balance, fee_balance = money(amount * rub_rate), money(fee * rub_rate)
    else:
        rub_rate, payout_balance, fee_balance = None, amount, fee
    total = money(payout_balance + fee_balance)
    if expected_reserve_amount is not None and money(expected_reserve_amount) != total:
        raise MerchantSettlementConflict('merchant_settlement_quote_changed')
    balance = await _balance(db, merchant_id, currency)
    existing = await _existing_idempotent(db, merchant_id=merchant_id, idempotency_key=idem, for_update=True)
    if existing:
        _assert_idempotent_match(existing, merchant_id=merchant_id, requested_by_id=requested_by_id,
                                 amount_usdt=amount, trc20_address=address)
        return existing
    if await db.scalar(select(MerchantSettlement.id).where(MerchantSettlement.merchant_id == merchant_id,
        MerchantSettlement.status.in_(SETTLEMENT_ACTIVE_STATUSES)).limit(1)):
        raise MerchantSettlementConflict('merchant_settlement_pending')
    if total > balance.available:
        raise MerchantSettlementError('merchant_settlement_insufficient_available')
    snapshot = {'source': 'usdt_balance', 'currency': 'USDT', 'fx_required': False}
    if quote:
        snapshot = {'symbol': quote.symbol, 'side': quote.side, 'source': quote.source,
            'provider_field': quote.provider_field, 'observation_id': quote.observation_id,
            'configuration_id': quote.configuration_id, 'configuration_version': quote.configuration_version,
            'expires_at': quote.expires_at.isoformat() if quote.expires_at else None,
            'provider_timestamp': _utc(quote.provider_timestamp).isoformat() if quote.provider_timestamp else None,
            'fetched_at': _utc(quote.fetched_at).isoformat(), 'freshness_basis': quote.freshness_basis}
    row = MerchantSettlement(merchant_id=merchant_id, requested_by_id=requested_by_id,
        contract_version=1, country=country, business_id=merchant.business_id, balance_id=balance.id,
        balance_currency=currency, amount_usdt=amount, fee_usdt=fee, total_usdt=usdt(amount + fee),
        payout_balance_amount=payout_balance, fee_balance_amount=fee_balance, reserve_amount=total,
        rate_rub=rub_rate, amount_rub=payout_balance if quote else None, fee_rub=fee_balance if quote else None,
        total_debit_rub=total if quote else None, trc20_address=address, network=SETTLEMENT_NETWORK,
        idempotency_key=idem, status=SETTLEMENT_STATUS_PENDING, network_verified=False,
        rate_symbol=quote.symbol if quote else None, rate_side=quote.side if quote else None,
        rate_source=quote.source if quote else 'usdt_balance', rate_provider_field=quote.provider_field if quote else None,
        provider_timestamp=quote.provider_timestamp if quote else None,
        fetched_at=_utc(quote.fetched_at) if quote else quoted_at,
        freshness_basis=quote.freshness_basis if quote else None,
        metadata_json={'quote': snapshot, 'settlement_contract': {'version': 1, 'country': country,
            'balance_currency': currency, 'network': SETTLEMENT_NETWORK, 'fee_on_top': True,
            'quoted_at': quoted_at.isoformat(), 'amount_usdt': str(amount), 'fee_usdt': str(fee),
            'reserve_amount': str(total)}})
    db.add(row)
    await db.flush()
    try:
        await hold(db, merchant_id, total, row.id, f'merchant-settlement:{row.id}:hold',
                   'merchant settlement reserve', currency=currency)
    except LedgerError as exc:
        raise MerchantSettlementError('merchant_settlement_insufficient_available') from exc
    await _event(db, row, requested_by_id, 'requested', None, actor_ip,
                 ledger_key=f'merchant-settlement:{row.id}:hold')
    await db.flush()
    return row


async def _locked_settlement_and_balance(db, settlement_id, *, actor_id, expected_country=None):
    await _admin(db, actor_id)
    identity = (await db.execute(select(MerchantSettlement.merchant_id, MerchantSettlement.country,
        MerchantSettlement.balance_currency).where(MerchantSettlement.id == settlement_id))).first()
    if not identity or (expected_country is not None and identity.country != expected_country):
        raise MerchantSettlementError('merchant_settlement_not_found')
    balance = await _balance(db, identity.merchant_id, identity.balance_currency)
    row = await db.scalar(select(MerchantSettlement).where(MerchantSettlement.id == settlement_id)
        .with_for_update().execution_options(populate_existing=True))
    merchant = await db.get(Merchant, identity.merchant_id)
    if (not row or not merchant or row.merchant_id != identity.merchant_id or row.country != identity.country
        or row.balance_currency != balance.currency or balance.merchant_id != row.merchant_id
        or (row.balance_id is not None and row.balance_id != balance.id) or row.network != SETTLEMENT_NETWORK):
        raise MerchantSettlementConflict('merchant_settlement_owner_changed')
    if row.contract_version == 1 and (row.country != merchant.working_country or row.business_id != merchant.business_id
        or row.requested_by_id != merchant.owner_id or SETTLEMENT_CURRENCIES.get(row.country) != row.balance_currency):
        raise MerchantSettlementConflict('merchant_settlement_owner_changed')
    if row.status in SETTLEMENT_ACTIVE_STATUSES:
        entry = await db.scalar(select(LedgerEntry).where(LedgerEntry.idempotency_key == f'merchant-settlement:{row.id}:hold'))
        if (not entry or entry.merchant_id != row.merchant_id or entry.operation_id != row.id
            or entry.currency != row.balance_currency or entry.entry_type != 'hold'
            or money(entry.amount) != money(row.reserve_amount) or balance.frozen < row.reserve_amount):
            raise MerchantSettlementConflict('merchant_settlement_invalid_frozen_balance')
        terminal_entry = await db.scalar(select(LedgerEntry.id).where(LedgerEntry.idempotency_key.in_(
            (f'merchant-settlement:{row.id}:debit-frozen',f'merchant-settlement:{row.id}:release'))).limit(1))
        fee_entry = await db.scalar(select(PlatformLedgerEntry.id).where(
            PlatformLedgerEntry.idempotency_key == f'merchant-settlement:{row.id}:fee'))
        if terminal_entry or fee_entry:
            raise MerchantSettlementConflict('merchant_settlement_invalid_frozen_balance')
    return row, balance


async def approve_merchant_settlement(db, *, settlement_id, actor_id, expected_country=None, actor_ip=None):
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    if row.status in ('approved','sent','reconciliation','completed'):
        return row
    if row.status != 'pending':
        raise MerchantSettlementConflict('merchant_settlement_already_rejected')
    previous = row.status
    row.status, row.approved_by_id, row.approved_at = 'approved', actor_id, datetime.now(UTC)
    await _event(db, row, actor_id, 'approved', previous, actor_ip)
    await db.flush()
    return row


async def register_merchant_settlement_sent(db, *, settlement_id, actor_id, tx_hash,
                                           sent_confirmed=False, expected_country=None, actor_ip=None):
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    txid = _tx_hash(tx_hash)
    if row.status in ('sent','reconciliation','completed'):
        if row.tx_hash != txid:
            raise MerchantSettlementConflict('merchant_settlement_tx_hash_mismatch')
        return row
    if row.status != 'approved':
        raise MerchantSettlementConflict('merchant_settlement_approval_required')
    if not sent_confirmed:
        raise MerchantSettlementError('merchant_settlement_dispatch_confirmation_required')
    from sqlalchemy import text
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                     {'key': 'settlement:TRC20:' + txid})
    if await db.scalar(select(MerchantSettlement.id).where(MerchantSettlement.network == SETTLEMENT_NETWORK,
            func.lower(MerchantSettlement.tx_hash) == txid, MerchantSettlement.id != row.id).limit(1)):
        raise MerchantSettlementConflict('merchant_settlement_tx_hash_duplicate')
    previous = row.status
    row.status, row.tx_hash, row.sent_by_id, row.sent_at = 'sent', txid, actor_id, datetime.now(UTC)
    await _event(db, row, actor_id, 'sent_registered', previous, actor_ip, payout_usdt=str(row.amount_usdt))
    try:
        await db.flush()
    except IntegrityError as exc:
        raise MerchantSettlementConflict('merchant_settlement_tx_hash_duplicate') from exc
    return row


async def complete_merchant_settlement(db, *, settlement_id, actor_id, tx_hash=None,
    execution_confirmed=False, execution_reference='', expected_country=None, actor_ip=None, now=None):
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    if row.status == 'completed':
        normalized = (tx_hash or row.tx_hash or '').strip().lower()
        if (row.tx_hash or '').lower() != normalized:
            raise MerchantSettlementConflict('merchant_settlement_already_completed')
        return row
    if row.status not in ('sent','reconciliation'):
        raise MerchantSettlementConflict('merchant_settlement_dispatch_required')
    if tx_hash is not None and _tx_hash(tx_hash) != row.tx_hash:
        raise MerchantSettlementConflict('merchant_settlement_tx_hash_mismatch')
    reference = (execution_reference or '').strip()
    if not execution_confirmed or not reference or len(reference) > 500:
        raise MerchantSettlementError('merchant_settlement_execution_evidence_required')
    previous = row.status
    try:
        await debit_frozen(db, row.merchant_id, row.reserve_amount, row.id,
            f'merchant-settlement:{row.id}:debit-frozen',
            f'TRC20 payout {row.amount_usdt} USDT + fee {row.fee_usdt} USDT', currency=row.balance_currency)
    except LedgerError as exc:
        raise MerchantSettlementConflict('merchant_settlement_invalid_frozen_balance') from exc
    # Income is in the reserve currency; RU does not mint a fictitious USDT asset.
    if row.fee_balance_amount > 0:
        db.add(PlatformLedgerEntry(merchant_id=row.merchant_id, operation_id=row.id,
            entry_type='settlement_fee_income', amount=row.fee_balance_amount, currency=row.balance_currency,
            idempotency_key=f'merchant-settlement:{row.id}:fee', description='Merchant settlement fee'))
    row.status, row.processed_by_id, row.processed_at = 'completed', actor_id, _utc(now or datetime.now(UTC))
    row.confirmation_kind, row.execution_reference, row.network_verified = 'administrator_confirmed', reference, False
    await _event(db, row, actor_id, 'completed', previous, actor_ip, execution_reference=reference,
                 ledger_key=f'merchant-settlement:{row.id}:debit-frozen', fee_ledger_key=f'merchant-settlement:{row.id}:fee')
    await db.flush()
    return row


async def _release(db, row, actor_id, reason, actor_ip=None, *, resolution=False, reference='', now=None):
    previous = row.status
    try:
        await release_hold(db, row.merchant_id, row.reserve_amount, row.id,
            f'merchant-settlement:{row.id}:release', 'settlement rejected before effective payout', currency=row.balance_currency)
    except LedgerError as exc:
        raise MerchantSettlementConflict('merchant_settlement_invalid_frozen_balance') from exc
    row.status, row.reject_reason, row.processed_by_id = 'rejected', reason, actor_id
    row.processed_at = _utc(now or datetime.now(UTC))
    if resolution:
        row.confirmation_kind, row.execution_reference = 'not_sent_verified', reference
    await _event(db, row, actor_id, 'not_sent_resolved' if resolution else 'rejected', previous, actor_ip,
                 reason=reason, execution_reference=reference, ledger_key=f'merchant-settlement:{row.id}:release')
    await db.flush()
    return row


async def reject_merchant_settlement(db, *, settlement_id, actor_id, reason,
                                     expected_country=None, actor_ip=None, now=None):
    reason = (reason or '').strip()
    if not reason or len(reason) > 500:
        raise MerchantSettlementError('merchant_settlement_reject_reason_required')
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    if row.status == 'rejected':
        return row
    if row.status not in ('pending','approved') or row.tx_hash is not None:
        raise MerchantSettlementConflict('merchant_settlement_reconciliation_required')
    return await _release(db, row, actor_id, reason, actor_ip, now=now)


async def flag_merchant_settlement_reconciliation(db, *, settlement_id, actor_id, reason,
                                                expected_country=None, actor_ip=None):
    reason = (reason or '').strip()
    if not reason or len(reason) > 500:
        raise MerchantSettlementError('merchant_settlement_reject_reason_required')
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    if row.status == 'reconciliation':
        return row
    if row.status != 'sent':
        raise MerchantSettlementConflict('merchant_settlement_dispatch_required')
    previous = row.status
    row.status, row.reconciliation_reason, row.reconciliation_by_id = 'reconciliation', reason, actor_id
    row.reconciliation_at = datetime.now(UTC)
    await _event(db, row, actor_id, 'reconciliation_required', previous, actor_ip, reason=reason)
    await db.flush()
    return row


async def resolve_merchant_settlement_not_sent(db, *, settlement_id, actor_id, reason,
    evidence_reference, not_sent_confirmed=False, expected_country=None, actor_ip=None):
    row, _ = await _locked_settlement_and_balance(db, settlement_id, actor_id=actor_id, expected_country=expected_country)
    if row.status == 'rejected' and row.confirmation_kind == 'not_sent_verified':
        return row
    reason, reference = (reason or '').strip(), (evidence_reference or '').strip()
    if row.status != 'reconciliation' or not not_sent_confirmed or not reason or not reference or max(len(reason),len(reference)) > 500:
        raise MerchantSettlementError('merchant_settlement_reconciliation_evidence_required')
    return await _release(db, row, actor_id, reason, actor_ip, resolution=True, reference=reference)


def public_settlement(row):
    names = ('id','merchant_id','business_id','country','balance_currency','amount_usdt','fee_usdt','total_usdt',
             'payout_balance_amount','fee_balance_amount','reserve_amount','rate_rub','amount_rub','fee_rub',
             'total_debit_rub','network','trc20_address','status','tx_hash','confirmation_kind','execution_reference',
             'reject_reason','reconciliation_reason','contract_version')
    result = {n: str(getattr(row,n)) if getattr(row,n) is not None else None for n in names}
    result['network_verified'] = False
    result['confirmation_label'] = ('Подтверждено администратором; сеть не проверялась' if row.status=='completed'
                                    else 'Зарегистрировано администратором; сеть не проверялась' if row.tx_hash else None)
    for name in ('created_at','approved_at','sent_at','processed_at','fetched_at','provider_timestamp'):
        value = getattr(row,name)
        result[name] = value.isoformat() if value else None
    result['rate_source'], result['rate_side'] = row.rate_source, row.rate_side
    return result


async def settlement_history(db, *, merchant_id=None, country=None, page=1):
    query = select(MerchantSettlement)
    if merchant_id is not None:
        query = query.where(MerchantSettlement.merchant_id == merchant_id)
    if country is not None:
        if country not in SETTLEMENT_CURRENCIES:
            raise MerchantSettlementError('merchant_settlement_country_required')
        query = query.where(MerchantSettlement.country == country)
    page = max(1, min(int(page),100000))
    return (await db.execute(query.order_by(MerchantSettlement.created_at.desc(),MerchantSettlement.id)
            .offset((page-1)*100).limit(100))).scalars().all()


async def settlement_audit(db, rows):
    ids = [str(row.id) for row in rows]
    events = (await db.execute(select(AuditLog).where(AuditLog.target_type=='merchant_settlement',
        AuditLog.target_id.in_(ids)).order_by(AuditLog.created_at,AuditLog.id))).scalars().all() if ids else []
    result = {}
    for event in events:
        result.setdefault(event.target_id, []).append(event)
    return result
