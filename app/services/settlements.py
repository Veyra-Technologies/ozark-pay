import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Balance, MerchantSettlement
from app.services.ledger import LedgerError, get_or_create_balance, hold
from app.services.rapira import get_rapira_rub_usdt_quote


SETTLEMENT_STATUS_PENDING = 'pending'
SETTLEMENT_STATUS_COMPLETED = 'completed'
SETTLEMENT_STATUS_REJECTED = 'rejected'
TRC20_RE = re.compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')
TWOPLACES = Decimal('0.01')
FOURPLACES = Decimal('0.0001')


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def usdt(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(TWOPLACES, rounding=ROUND_DOWN)


def rate(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.0000').quantize(FOURPLACES, rounding=ROUND_HALF_UP)


async def settlement_rate_rub() -> tuple[Decimal, str]:
    quote = await get_rapira_rub_usdt_quote()
    source = f'{quote.source} stale' if quote.stale else quote.source
    return rate(quote.rate_rub), source


def settlement_transfer_fee_usdt() -> Decimal:
    configured = usdt(settings.SETTLEMENT_TRANSFER_FEE_USDT)
    return configured if configured >= 0 else Decimal('5.00')


def validate_trc20_address(address: str) -> str:
    clean = (address or '').strip()
    if not TRC20_RE.fullmatch(clean):
        raise ValueError('Введите корректный TRC20 адрес')
    return clean


async def pending_settlement_total(db: AsyncSession, merchant_id: UUID) -> Decimal:
    total = (await db.execute(
        select(func.coalesce(func.sum(MerchantSettlement.total_debit_rub), 0)).where(
            MerchantSettlement.merchant_id == merchant_id,
            MerchantSettlement.status == SETTLEMENT_STATUS_PENDING,
        )
    )).scalar_one()
    return money(total)


async def merchant_settlement_quote(db: AsyncSession, merchant_id: UUID) -> dict:
    balance = (await db.execute(
        select(Balance).where(Balance.merchant_id == merchant_id, Balance.currency == 'RUB')
    )).scalar_one_or_none()
    available = money(balance.available if balance else Decimal('0.00'))
    pending = await pending_settlement_total(db, merchant_id)
    rate_quote = await get_rapira_rub_usdt_quote()
    current_rate = rate(rate_quote.rate_rub)
    rate_source = f'{rate_quote.source} stale' if rate_quote.stale else rate_quote.source
    fee_usdt = settlement_transfer_fee_usdt()
    fee_rub = money(fee_usdt * current_rate)
    free_rub = max(Decimal('0.00'), available)
    max_request_usdt = Decimal('0.00')
    if current_rate > 0 and free_rub > fee_rub:
        max_request_usdt = usdt((free_rub - fee_rub) / current_rate)
    return {
        'rate_rub': current_rate,
        'fee_usdt': fee_usdt,
        'fee_rub': fee_rub,
        'available_rub': free_rub,
        'pending_rub': pending,
        'max_request_usdt': max_request_usdt,
        'rate_source': rate_source,
        'rate_updated_at': rate_quote.updated_at,
        'rate_stale': rate_quote.stale,
    }


async def create_merchant_settlement(db: AsyncSession, *, merchant_id: UUID, requested_by_id: UUID, amount_usdt: Decimal, trc20_address: str) -> MerchantSettlement:
    amount_usdt = usdt(amount_usdt)
    if amount_usdt <= 0:
        raise ValueError('Сумма settle должна быть больше нуля')
    trc20_address = validate_trc20_address(trc20_address)
    # Fetch the external quote before acquiring any database row lock.
    rate_quote = await get_rapira_rub_usdt_quote()
    rate_rub = rate(rate_quote.rate_rub)
    rate_source = (
        f'{rate_quote.source} stale'
        if rate_quote.stale
        else rate_quote.source
    )
    fee_usdt = settlement_transfer_fee_usdt()
    amount_rub = money(amount_usdt * rate_rub)
    fee_rub = money(fee_usdt * rate_rub)
    total_debit_rub = money(amount_rub + fee_rub)
    # The balance lock serializes requests for this merchant. The database
    # partial unique index remains the final concurrency guard.
    balance = await get_or_create_balance(db, merchant_id)
    pending = (await db.execute(
        select(MerchantSettlement.id).where(
            MerchantSettlement.merchant_id == merchant_id,
            MerchantSettlement.status == SETTLEMENT_STATUS_PENDING,
        )
    )).scalar_one_or_none()
    if pending:
        raise ValueError('У мерчанта уже есть активный запрос settle')
    if total_debit_rub > money(balance.available):
        raise ValueError('Недостаточно доступного баланса для settle с учётом комиссии сети')
    settlement = MerchantSettlement(
        merchant_id=merchant_id,
        requested_by_id=requested_by_id,
        amount_usdt=amount_usdt,
        fee_usdt=fee_usdt,
        rate_rub=rate_rub,
        amount_rub=amount_rub,
        fee_rub=fee_rub,
        total_debit_rub=total_debit_rub,
        trc20_address=trc20_address,
        status=SETTLEMENT_STATUS_PENDING,
        metadata_json={'rate_source': rate_source},
    )
    db.add(settlement)
    await db.flush()
    try:
        await hold(db, merchant_id, total_debit_rub, settlement.id, f'merchant-settlement:{settlement.id}:hold', 'settlement request hold')
    except LedgerError as exc:
        raise ValueError(str(exc)) from exc
    return settlement
