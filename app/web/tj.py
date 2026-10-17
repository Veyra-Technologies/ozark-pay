"""TJ web forms reuse staff sessions, CSRF middleware and the cabinet services."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tj import TariffIn
from app.core.tajikistan import admission_status
from app.db.session import get_db
from app.models import Balance, Deposit, Requisite, TariffPlan, TraderWallet
from app.services.country_tariffs import public_plan
from app.services.crossborder_money import CrossBorderError
from app.services.tj_cabinets import personal_plan, save_personal_plan

router = APIRouter()


@router.post('/cabinet/tj/tariffs')
async def save_tariff(request: Request, db: AsyncSession=Depends(get_db)):
    from app.web.routes import _fee_admin_user, PREVIEW_ROLE_SESSION_KEY
    actor = await _fee_admin_user(request, db)
    if not actor or request.session.get(PREVIEW_ROLE_SESSION_KEY):
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
    assigned = await personal_plan(db, party=party, entity_id=subject.id)
    current_tiers = assigned.tiers if assigned else []
    return {'admission':admission_status(),'wallets':values,'party':party,'cabinet_id':str(subject.id),
            'tariffs':[public_plan(p) for p in plans], 'current_tiers':current_tiers, 'tariff_ready':bool(assigned),
            'successful_count':count,'turnover_tjs':turnover, 'operations':recent}
