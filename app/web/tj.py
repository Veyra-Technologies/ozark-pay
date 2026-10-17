"""TJ web forms reuse staff sessions, CSRF middleware and the cabinet services."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tj import TariffIn
from app.core.tajikistan import admission_status
from app.db.session import get_db
from app.models import Balance, Deposit, OperationFeeSnapshot, Requisite, TariffPlan, TraderWallet, User
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


async def cabinet_context(db, *, merchant=None, trader=None):
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
    recent = []
    if merchant:
        recent = (await db.execute(select(Deposit).where(Deposit.merchant_id == subject.id,
            Deposit.currency == 'TJS').order_by(Deposit.created_at.desc()).limit(50))).scalars().all()
    else:
        recent = (await db.execute(select(Deposit).join(Requisite,Requisite.id == Deposit.requisites_id)
            .where(Requisite.trader_id == subject.id,Requisite.country == 'TJ',Deposit.currency == 'TJS')
            .order_by(Deposit.created_at.desc()).limit(50))).scalars().all()
    own_finance = {}
    snapshots = (await db.execute(select(OperationFeeSnapshot).where(
        OperationFeeSnapshot.deposit_id.in_([op.id for op in recent])))).scalars().all() if recent else []
    from app.core.security import reveal_text
    for fixed in snapshots:
        context = fixed.financial_context or {}
        bank = context.get('requisite') or {}
        own_finance[str(fixed.deposit_id)] = {
            'base_usdt':context.get('base_usdt'),
            'net_usdt':context.get('merchant_payable_usdt' if merchant else 'debit_usdt'),
            'fee_tjs':context.get('merchant_fee_tjs' if merchant else 'trader_fee_tjs'),
            'fee_usdt':context.get('merchant_fee_usdt' if merchant else 'trader_fee_usdt'),
            'reserve_usdt':context.get('reserve_usdt') if trader else None,
            'trader_id':str(fixed.executor_id),'bank_id':bank.get('bank_id'),
            'bank_name':bank.get('bank_name'),'requisite':reveal_text(bank['value_encrypted']) if bank.get('value_encrypted') else ''}
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
    return {'admission':admission,'wallets':values,'party':party,'cabinet_id':str(subject.id),
            'tariffs':[public_plan(p) for p in plans], 'current_tiers':current_tiers, 'tariff_ready':bool(assigned),
            'successful_count':count,'turnover_tjs':turnover, 'operations':recent,
            'qa_financial':qa_enabled,'operation_finance':own_finance}
