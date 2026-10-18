"""TJ web forms reuse staff sessions, CSRF middleware and the cabinet services."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tj import TariffIn
from app.core.tajikistan import admission_status
from app.db.session import get_db
from app.models import Balance, Deposit, LedgerEntry, MerchantSettlement, OperationFeeSnapshot, Requisite, TariffPlan, TraderWallet, TraderWalletLedgerEntry, User
from app.services.country_tariffs import public_plan
from app.services.crossborder_money import CrossBorderError
from app.services.tj_cabinets import personal_plan, save_personal_plan

router = APIRouter()


@router.post('/cabinet/tj/deposits/{deposit_id}/refund')
async def refund_local_deposit(deposit_id: str, request: Request, reason: str=Form(...),
        reference: str=Form(...),db: AsyncSession=Depends(get_db)):
    from uuid import UUID
    from fastapi.responses import RedirectResponse
    from app.web.routes import _fee_admin_user, PREVIEW_ROLE_SESSION_KEY, staff_2fa_setup_required
    from app.services.deposit_lifecycle import refund_deposit
    from app.services.webhook_enqueue import enqueue_webhook_delivery
    actor=await _fee_admin_user(request,db)
    if not actor or staff_2fa_setup_required(actor) or request.session.get(PREVIEW_ROLE_SESSION_KEY):
        raise HTTPException(403,'Нужны права Admin/SuperAdmin')
    try:
        result=await refund_deposit(db,UUID(deposit_id),actor=actor,reason=reason,reference=reference)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409,str(exc)) from exc
    if result.webhook_event_id:
        enqueue_webhook_delivery(result.webhook_event_id)
    return RedirectResponse('/staff/cabinet#sec1',status_code=303)


@router.post('/cabinet/tj/tariffs')
async def save_tariff(request: Request, db: AsyncSession=Depends(get_db)):
    from app.web.routes import _fee_admin_user, PREVIEW_ROLE_SESSION_KEY, staff_2fa_setup_required
    actor = await _fee_admin_user(request, db)
    if not actor or staff_2fa_setup_required(actor) or request.session.get(PREVIEW_ROLE_SESSION_KEY):
        raise HTTPException(403, 'Нужны права Admin/SuperAdmin и действующая staff-сессия')
    try:
        data = TariffIn.model_validate(await request.json())
        plan = await save_personal_plan(db, actor=actor, **data.model_dump())
        await db.commit()
        return public_plan(plan)
    except (ValidationError, ValueError) as exc:
        await db.rollback()
        return JSONResponse({'detail': str(exc) if isinstance(exc, CrossBorderError) else 'Проверьте диапазоны, дату и процент комиссии.'}, status_code=422)


async def cabinet_context(db, *, merchant=None, trader=None, request=None):
    subject = merchant or trader
    if not subject or subject.working_country != 'TJ':
        return {}
    model = Balance if merchant else TraderWallet
    owner = model.merchant_id if merchant else model.trader_id
    wallets = (await db.execute(select(model).where(owner == subject.id))).scalars().all()
    values = {row.currency: {'available': row.available if merchant else row.balance-row.hold,
                            'hold': row.frozen if merchant else row.hold} for row in wallets if row.currency in ('TJS','USDT')}
    party = 'merchant' if merchant else 'trader'
    plans = (await db.execute(select(TariffPlan).where(TariffPlan.country == 'TJ', TariffPlan.party == party,
        TariffPlan.entity_id == subject.id)
        .order_by(TariffPlan.created_at.desc()))).scalars().all()
    query = select(func.count(),func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.currency == 'TJS', Deposit.status == 'paid')
    query = query.where(Deposit.merchant_id == subject.id) if merchant else query.join(Requisite,Requisite.id == Deposit.requisites_id).where(Requisite.trader_id == subject.id,Requisite.country == 'TJ')
    count,turnover = (await db.execute(query)).one()
    base = select(Deposit).where(Deposit.currency == 'TJS')
    base = base.where(Deposit.merchant_id == subject.id) if merchant else base.join(
        Requisite, Requisite.id == Deposit.requisites_id).where(Requisite.trader_id == subject.id, Requisite.country == 'TJ')
    filters = request.query_params if request else {}
    period = filters.get('tj_period', 'all')
    if period in ('1', '7', '30', '90'):
        base = base.where(Deposit.created_at >= datetime.now(timezone.utc) - timedelta(days=int(period)))
    for field, end in [('tj_from', False), ('tj_to', True)]:
        if filters.get(field):
            try:
                value = datetime.combine(datetime.strptime(filters[field], '%Y-%m-%d').date(), time.min, timezone.utc)
            except ValueError as exc:
                raise HTTPException(422, 'Дата должна иметь формат ГГГГ-ММ-ДД') from exc
            base = base.where(Deposit.created_at < value + timedelta(days=1)) if end else base.where(Deposit.created_at >= value)
    scoped = base.subquery()
    counts = dict((await db.execute(select(scoped.c.status, func.count()).group_by(scoped.c.status))).all())
    total = sum(counts.values())
    success = counts.get('paid', 0)
    failed = sum(counts.get(s, 0) for s in ('failed', 'cancelled', 'expired', 'refunded'))
    completed = success + failed
    financial = OperationFeeSnapshot.financial_context
    net_key = 'merchant_payable_usdt' if merchant else 'debit_usdt'
    fee_key = 'merchant_fee_usdt' if merchant else 'trader_fee_usdt'
    paid_totals = (await db.execute(select(func.coalesce(func.sum(Deposit.amount), 0),
        func.coalesce(func.sum(cast(financial[net_key].as_string(), Numeric(38, 12))), 0),
        func.coalesce(func.sum(cast(financial[fee_key].as_string(), Numeric(38, 12))), 0))
        .join(OperationFeeSnapshot, OperationFeeSnapshot.deposit_id == Deposit.id)
        .where(Deposit.id.in_(select(scoped.c.id)), Deposit.status == 'paid'))).one()
    statistics = {'total': total, 'successful': success, 'failed': failed,
        'active': total - completed, 'completed': completed,
        'conversion': Decimal(success) * 100 / Decimal(completed) if completed else Decimal(0),
        'turnover_tjs': paid_totals[0], 'settled_usdt': paid_totals[1], 'fee_usdt': paid_totals[2]}
    status = filters.get('tj_status', '')
    if status in ('created', 'pending', 'paid', 'failed', 'cancelled', 'expired', 'refunded', 'appeal_opened'):
        base = base.where(Deposit.status == status)
    search = filters.get('tj_query', '').strip()[:120]
    if search:
        from sqlalchemy import String, or_
        base = base.where(or_(cast(Deposit.id, String).contains(search, autoescape=True),
            Deposit.external_id.contains(search, autoescape=True)))
    filtered_count = await db.scalar(select(func.count()).select_from(base.subquery()))
    try:
        page = max(1, min(int(filters.get('tj_page', '1')), 1000000))
    except (ValueError, TypeError):
        page = 1
    pages = max(1, (filtered_count + 24) // 25)
    page = min(page, pages)
    recent = list(await db.scalars(base.order_by(Deposit.created_at.desc(), Deposit.id.desc()).offset((page - 1) * 25).limit(25)))
    def page_url(number):
        query = {k: v for k, v in filters.items() if k.startswith('tj_') or k == 'section'}
        query['tj_page'] = str(number)
        return '?' + urlencode(query)
    pagination = {'page': page, 'pages': pages, 'count': filtered_count,
        'previous': page_url(page - 1) if page > 1 else None,
        'next': page_url(page + 1) if page < pages else None}
    own_finance = {}
    snapshots = (await db.execute(select(OperationFeeSnapshot).where(
        OperationFeeSnapshot.deposit_id.in_([op.id for op in recent])))).scalars().all() if recent else []
    from app.core.security import reveal_text
    for fixed in snapshots:
        context = fixed.financial_context or {}
        bank = context.get('requisite') or {}
        external_details = None
        if fixed.executor_type == 'aggregator':
            from app.services.aggregator_execution import public_requisites
            deposit = next(op for op in recent if op.id == fixed.deposit_id)
            external_details = (await public_requisites(db, deposit)).get('requisites') or {}
        own_finance[str(fixed.deposit_id)] = {
            'base_usdt':context.get('base_usdt'),
            'net_usdt':context.get('merchant_payable_usdt' if merchant else 'debit_usdt'),
            'fee_tjs':context.get('merchant_fee_tjs' if merchant else 'trader_fee_tjs'),
            'fee_usdt':context.get('merchant_fee_usdt' if merchant else 'trader_fee_usdt'),
            'reserve_usdt':context.get('reserve_usdt') if trader else None,
            'trader_id':str(fixed.executor_id),'bank_id':bank.get('bank_id'),
            'bank_name':bank.get('bank_name'),'requisite':reveal_text(bank['value_encrypted']) if bank.get('value_encrypted') else ''}
        if external_details:
            own_finance[str(fixed.deposit_id)].update(bank_id=external_details.get('bank_id'),
                bank_name=external_details.get('bank_name'),requisite=external_details.get('card_number') or external_details.get('bank_account'))
    qa_enabled = False
    owner = await db.get(User,merchant.owner_id) if merchant else trader
    if owner and owner.role in ('merchant','trader'):
        from app.services.local_deposit_policy import require_execution
        try:
            await require_execution(db)
            qa_enabled = True
        except ValueError:
            pass
    assigned = await personal_plan(db, party=party, entity_id=subject.id)
    current_tiers = assigned.tiers if assigned else []
    admission = admission_status()
    if qa_enabled:
        admission = {**admission,'reason':'Изолированная QA: синтетические котировки и платежи. Реальный приём TJ закрыт.'}
    if merchant:
        journal = list(await db.scalars(select(LedgerEntry).where(LedgerEntry.merchant_id == subject.id,
            LedgerEntry.currency.in_(('TJS', 'USDT'))).order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(100)))
        settlements = list(await db.scalars(select(MerchantSettlement).where(MerchantSettlement.merchant_id == subject.id,
            MerchantSettlement.country == 'TJ').order_by(MerchantSettlement.created_at.desc()).limit(100)))
        from app.services.rolling import rolling_overview, list_rolling_transfers
        rolling = await rolling_overview(db, subject.id)
        rolling['transfers'] = await list_rolling_transfers(db, subject.id)
    else:
        journal = list(await db.scalars(select(TraderWalletLedgerEntry).where(TraderWalletLedgerEntry.trader_id == subject.id,
            TraderWalletLedgerEntry.currency.in_(('TJS', 'USDT'))).order_by(TraderWalletLedgerEntry.created_at.desc(), TraderWalletLedgerEntry.id.desc()).limit(100)))
        settlements, rolling = [], {}
    grouped_settlements = dict((await db.execute(select(MerchantSettlement.status,
        func.coalesce(func.sum(MerchantSettlement.reserve_amount), 0)).where(
        MerchantSettlement.merchant_id == subject.id, MerchantSettlement.country == 'TJ',
        MerchantSettlement.balance_currency == 'USDT').group_by(MerchantSettlement.status))).all()) if merchant else {}
    settlement_totals = {state: grouped_settlements.get(state, Decimal(0))
        for state in ('pending', 'approved', 'sent', 'reconciliation', 'completed')}
    return {'admission':admission,'wallets':values,'party':party,'cabinet_id':str(subject.id),
            'tariffs':[public_plan(p) for p in plans], 'current_tiers':current_tiers, 'tariff_ready':bool(assigned),
            'successful_count':count,'turnover_tjs':turnover, 'operations':recent,
            'qa_financial':qa_enabled,'operation_finance':own_finance, 'statistics': statistics,
            'filters': filters, 'pagination': pagination, 'journal': journal,
            'settlements': settlements, 'settlement_totals': settlement_totals, 'rolling': rolling}
