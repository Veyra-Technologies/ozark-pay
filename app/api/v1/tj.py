"""TJ cabinet preparation within existing admin authorization and financial models."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core.enums import Role
from app.core.tajikistan import admission_status
from app.db.session import get_db
from app.models import Merchant, TariffPlan, User
from app.services import tj_cabinets
from app.services.country_tariffs import public_plan
from app.services.crossborder_money import CrossBorderError

router = APIRouter(prefix='/tj', tags=['tajikistan-local'])
administrator = require_roles(Role.admin, Role.superadmin)
participant = require_roles(Role.admin, Role.superadmin, Role.merchant, Role.trader, Role.aggregator)


class TariffIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    party: Literal['merchant', 'trader']
    entity_id: UUID | None = None
    tiers: list[dict] = Field(min_length=1, max_length=100)
    effective_from: datetime


class AssignIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: UUID


class RefundIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=4, max_length=400)
    reference: str = Field(min_length=4, max_length=200)


@router.post('/deposits/{deposit_id}/refund')
async def refund_local_deposit(deposit_id: UUID, data: RefundIn,
        actor=Depends(administrator), db: AsyncSession=Depends(get_db)):
    from app.services.deposit_lifecycle import refund_deposit
    from app.services.webhook_enqueue import enqueue_webhook_delivery
    try:
        result = await refund_deposit(db,deposit_id,actor=actor,**data.model_dump())
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409,str(exc)) from exc
    if result.webhook_event_id:
        enqueue_webhook_delivery(result.webhook_event_id)
    return {'id':str(result.deposit_id),'status':result.status,'changed':result.changed}


@router.get('/status')
async def status(actor=Depends(participant), db: AsyncSession=Depends(get_db)):
    subject_id = None
    party = 'merchant'
    if actor.role in ('merchant', 'aggregator'):
        merchant = await db.scalar(select(Merchant).where(Merchant.owner_id == actor.id))
        if not merchant or merchant.working_country != 'TJ':
            raise HTTPException(403, 'Недоступно для страны кабинета')
        subject_id = merchant.id
    elif actor.role == 'trader' and actor.working_country != 'TJ':
        raise HTTPException(403, 'Недоступно для страны кабинета')
    elif actor.role == 'trader':
        subject_id, party = actor.id, 'trader'
    if subject_id:
        plan = await tj_cabinets.personal_plan(db, party=party, entity_id=subject_id)
        return {**admission_status(), 'tariff_ready':bool(plan), 'cabinet_id':str(subject_id)}
    return admission_status()


@router.post('/tariffs')
async def create_tariff(data: TariffIn, actor=Depends(administrator), db: AsyncSession=Depends(get_db)):
    try:
        plan = await tj_cabinets.create_plan(db, actor=actor, **data.model_dump())
        await db.commit()
        return public_plan(plan)
    except CrossBorderError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.get('/tariffs')
async def tariffs(actor=Depends(participant), db: AsyncSession=Depends(get_db)):
    query = select(TariffPlan).where(TariffPlan.country == 'TJ')
    if actor.role in ('merchant', 'aggregator'):
        merchant = await db.scalar(select(Merchant).where(Merchant.owner_id == actor.id))
        if not merchant or merchant.working_country != 'TJ':
            raise HTTPException(403, 'Недоступно для страны кабинета')
        query = query.where(TariffPlan.party == 'merchant', TariffPlan.entity_id == merchant.id)
    elif actor.role == 'trader':
        if actor.working_country != 'TJ':
            raise HTTPException(403, 'Недоступно для страны кабинета')
        query = query.where(TariffPlan.party == 'trader', TariffPlan.entity_id == actor.id)
    return [public_plan(p) for p in (await db.execute(query.order_by(TariffPlan.created_at.desc()))).scalars()]


@router.post('/traders/{trader_id}/tariff')
async def assign_tariff(trader_id: UUID, data: AssignIn, actor=Depends(administrator), db: AsyncSession=Depends(get_db)):
    trader = await db.get(User, trader_id)
    if not trader:
        raise HTTPException(404, 'Кабинет не найден')
    try:
        plan = await tj_cabinets.assign_trader_plan(db, actor=actor, trader=trader, plan_id=data.plan_id)
        await db.commit()
        return public_plan(plan)
    except CrossBorderError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
