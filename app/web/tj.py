"""TJ web forms reuse staff sessions, CSRF middleware and the cabinet services."""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tj import TariffIn
from app.core.tajikistan import admission_status
from app.db.session import get_db
from app.models import Balance, Deposit, FeeRule, Merchant, Requisite, TariffPlan, TraderWallet
from app.services.country_tariffs import public_plan
from app.services.crossborder_money import CrossBorderError
from app.services.tj_cabinets import create_plan, assign_trader_plan

router = APIRouter()


@router.post('/cabinet/tj/tariffs')
async def save_tariff(request: Request, db: AsyncSession=Depends(get_db)):
    from app.web.routes import _fee_admin_user, PREVIEW_ROLE_SESSION_KEY
    actor = await _fee_admin_user(request, db)
    if not actor or request.session.get(PREVIEW_ROLE_SESSION_KEY):
        raise HTTPException(403, 'Нужны права Admin/SuperAdmin и действующая staff-сессия')
    try:
        data = TariffIn.model_validate(await request.json())
        plan = await create_plan(db, actor=actor, **data.model_dump())
        if data.entity_id and data.effective_from <= datetime.now(timezone.utc):
            if data.party == 'merchant':
                from app.services.merchant_cabinets import configure
                merchant = await db.get(Merchant, data.entity_id)
                await configure(db, actor=actor, merchant=merchant, currency='TJS',
                    parser_id=merchant.rate_parser_id, tariff_plan_id=plan.id, methods=['c2c'])
            else:
                from app.models import User
                await assign_trader_plan(db, actor=actor, trader=await db.get(User, data.entity_id), plan_id=plan.id)
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
                            'hold': row.frozen if merchant else row.hold} for row in wallets}
    party = 'merchant' if merchant else 'trader'
    plans = (await db.execute(select(TariffPlan).where(TariffPlan.country == 'TJ', TariffPlan.party == party,
        (TariffPlan.entity_id == subject.id) | TariffPlan.entity_id.is_(None))
        .order_by(TariffPlan.created_at.desc()))).scalars().all()
    query = select(func.count(),func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.currency == 'TJS', Deposit.status == 'paid')
    query = query.where(Deposit.merchant_id == subject.id) if merchant else query.join(Requisite,Requisite.id == Deposit.requisites_id).where(Requisite.trader_id == subject.id,Requisite.country == 'TJ')
    count,turnover = (await db.execute(query)).one()
    current_tiers = []
    if merchant:
        assigned = next((p for p in plans if p.id == merchant.tariff_plan_id), None)
        if assigned:
            current_tiers = assigned.tiers
    else:
        now = datetime.now(timezone.utc)
        rules = (await db.execute(select(FeeRule).where(FeeRule.entity_type == 'trader',
            FeeRule.entity_id == subject.id, FeeRule.currency == 'TJS', FeeRule.payment_method == 'c2c',
            FeeRule.is_active.is_(True), FeeRule.effective_from <= now,
            FeeRule.effective_to.is_(None) | (FeeRule.effective_to > now))
            .order_by(FeeRule.min_amount))).scalars().all()
        current_tiers = [{'min':str(r.min_amount),'max':str(r.max_amount) if r.max_amount is not None else None,
                          'percent':str(r.rate_percent)} for r in rules]
    return {'admission':admission_status(),'wallets':values,'party':party,'cabinet_id':str(subject.id),
            'tariffs':[public_plan(p) for p in plans], 'current_tiers':current_tiers,
            'successful_count':count,'turnover_tjs':turnover}
