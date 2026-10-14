"""Read-only merchant presentation using stored settlement amounts."""
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import Date, String, cast, func, or_, select

from app.models import CrossBorderOperation, LedgerEntry, Requisite, User


def settlement_currency(merchant):
    return 'USDT' if merchant and merchant.working_country == 'KZ' else 'RUB'


async def kz_merchant_view(db, merchant, *, filters=None, trader_id='', page=1):
    """Aggregate the entire cabinet; paginate only its operational list."""
    if not merchant or merchant.working_country != 'KZ':
        return {}
    scope = (CrossBorderOperation.merchant_id == merchant.id,
             CrossBorderOperation.direction == 'kz-ge')
    groups = (await db.execute(select(
        CrossBorderOperation.status, CrossBorderOperation.trader_id,
        func.count().label('count'),
        func.coalesce(func.sum(CrossBorderOperation.base_usdt), 0).label('turnover'),
        func.coalesce(func.sum(CrossBorderOperation.merchant_usdt), 0).label('payable'),
    ).where(*scope).group_by(CrossBorderOperation.status, CrossBorderOperation.trader_id))).mappings().all()
    trader_ids = {g['trader_id'] for g in groups}
    traders = (await db.execute(select(User).where(User.id.in_(trader_ids)))).scalars().all() if trader_ids else []
    by_trader = {t.id: {'email': t.email, 'turnover': Decimal(0), 'successful_count': 0,
        'failed_count': 0, 'active_count': 0, 'total_count': 0, 'appeals': 0} for t in traders}
    stats = {'currency': 'USDT', 'total_turnover': Decimal(0), 'merchant_payable': Decimal(0),
             'merchant_fee': Decimal(0), 'successful_count': 0, 'failed_count': 0,
             'active_count': 0, 'total_count': 0, 'appeals_count': 0, 'traders_count': len(trader_ids)}
    for group in groups:
        bucket = ('successful_count' if group['status'] == 'paid' else
                  'active_count' if group['status'] == 'accepted' else 'failed_count')
        stats[bucket] += group['count']
        stats['total_count'] += group['count']
        trader = by_trader.get(group['trader_id'])
        if trader:
            trader[bucket] += group['count']
            trader['total_count'] += group['count']
        if group['status'] == 'paid':
            # Both values are frozen when the operation is accepted; no live-rate conversion.
            stats['total_turnover'] += group['turnover']
            stats['merchant_payable'] += group['payable']
            stats['merchant_fee'] += group['turnover'] - group['payable']
            if trader:
                trader['turnover'] += group['turnover']
    for item in (stats, *by_trader.values()):
        item['conversion'] = (Decimal(item['successful_count']) / item['total_count'] * 100
                              if item['total_count'] else Decimal(0))

    filters = filters or {}
    query = select(CrossBorderOperation, Requisite.bank_name, Requisite.owner_name, Requisite.last4,
                   User.email).outerjoin(Requisite, Requisite.id == CrossBorderOperation.requisite_id
                   ).outerjoin(User, User.id == CrossBorderOperation.trader_id).where(*scope)
    if filters.get('query'):
        needle = '%' + filters['query'] + '%'
        query = query.where(or_(CrossBorderOperation.external_id.ilike(needle),
                               cast(CrossBorderOperation.id, String).ilike(needle)))
    try:
        if filters.get('amount'):
            amount = Decimal(str(filters['amount']).replace(',', '.'))
            query = query.where(CrossBorderOperation.amount_kzt == amount) if amount.is_finite() else query.where(False)
        if filters.get('date'):
            query = query.where(cast(CrossBorderOperation.created_at, Date) == date.fromisoformat(filters['date']))
        if trader_id:
            query = query.where(CrossBorderOperation.trader_id == UUID(trader_id))
    except (ValueError, InvalidOperation):
        query = query.where(False)
    if filters.get('requisite'):
        needle = '%' + filters['requisite'] + '%'
        query = query.where(or_(Requisite.bank_name.ilike(needle), Requisite.owner_name.ilike(needle),
                               Requisite.last4.ilike(needle)))
    try:
        page = max(1, min(int(page), 100000))
    except (TypeError, ValueError):
        page = 1
    rows = (await db.execute(query.order_by(CrossBorderOperation.created_at.desc(), CrossBorderOperation.id)
                            .offset((page - 1) * 200).limit(201))).all()
    operations = []
    for operation, bank, owner, last4, email in rows[:200]:
        operations.append({'id': str(operation.id), 'external_id': operation.external_id,
            'created_at': operation.created_at, 'status': operation.status, 'amount_kzt': operation.amount_kzt,
            'base_usdt': operation.base_usdt, 'merchant_usdt': operation.merchant_usdt,
            'merchant_fee_usdt': operation.base_usdt - operation.merchant_usdt,
            'expected_gel': operation.expected_gel, 'actual_gel': operation.actual_gel,
            'bank': bank or 'Архивный банк', 'beneficiary': f'{owner or "—"} · ****{last4 or ""}',
            'trader_email': email or 'Архивный трейдер'})
    ledger = (await db.execute(select(LedgerEntry).where(LedgerEntry.merchant_id == merchant.id,
        LedgerEntry.currency == 'USDT').order_by(LedgerEntry.created_at.desc(), LedgerEntry.id).limit(200))).scalars().all()
    return {'currency': 'USDT', 'statistics': stats, 'operations': operations, 'ledger': ledger,
            'traders': traders, 'trader_rows': sorted(by_trader.values(), key=lambda t: t['turnover'], reverse=True),
            'page': page, 'has_more': len(rows) > 200}
