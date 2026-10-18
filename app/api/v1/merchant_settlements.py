from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from app.api.deps import merchant_auth, require_roles
from app.core.client_ip import client_ip
from app.core.enums import Role
from app.core.settlement_policy import SETTLEMENT_ERROR_MESSAGES
from app.db.session import get_db
from app.models import Merchant, MerchantSettlement, User
from app.services import settlements as service
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=['merchant-settlements'])
DBSession = Annotated[AsyncSession, Depends(get_db)]
MerchantIdentity = Annotated[Merchant, Depends(merchant_auth)]
StaffIdentity = Annotated[User, Depends(require_roles(Role.superadmin, Role.admin))]


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount_usdt: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    trc20_address: str = Field(min_length=34, max_length=34)
    network: Literal['TRC20'] = 'TRC20'
    expected_reserve_amount: Decimal | None = Field(default=None, gt=0, max_digits=24, decimal_places=2, allow_inf_nan=False)


class SettlementAction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tx_hash: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=500)
    execution_reference: str | None = Field(default=None, max_length=500)
    sent_confirmed: bool = False
    execution_confirmed: bool = False
    not_sent_confirmed: bool = False


def error(exc):
    status = (503 if isinstance(exc, service.MerchantSettlementRateUnavailable) or exc.code == 'tj_settlement_production_disabled' else
              404 if exc.code == 'merchant_settlement_not_found' else
              403 if exc.code == 'permission_denied' else
              409 if isinstance(exc, service.MerchantSettlementConflict) or exc.code.endswith('insufficient_available') else 422)
    return HTTPException(status, {'code': exc.code, 'message': SETTLEMENT_ERROR_MESSAGES.get(exc.code, exc.code)})


@router.get('/merchant/settlements/quote')
async def merchant_quote(merchant: MerchantIdentity, db: DBSession, amount_usdt: Decimal | None = None):
    try:
        return (await service.settlement_preview(db, merchant.id, amount_usdt) if amount_usdt is not None
                else await service.merchant_settlement_quote(db, merchant.id, allow_unavailable=True))
    except service.MerchantSettlementError as exc:
        raise error(exc) from exc


@router.post('/merchant/settlements')
async def merchant_request(payload: SettlementRequest, request: Request, merchant: MerchantIdentity, db: DBSession):
    try:
        row = await service.create_merchant_settlement(db, merchant_id=merchant.id, requested_by_id=merchant.owner_id,
            amount_usdt=payload.amount_usdt, trc20_address=payload.trc20_address,
            idempotency_key=request.state.idempotency_key, actor_ip=client_ip(request),
            expected_reserve_amount=payload.expected_reserve_amount)
        await db.commit()
    except service.MerchantSettlementError as exc:
        await db.rollback()
        raise error(exc) from exc
    return service.public_settlement(row)


@router.get('/merchant/settlements')
async def merchant_history(merchant: MerchantIdentity, db: DBSession, page: int = 1):
    rows = await service.settlement_history(db, merchant_id=merchant.id, page=page)
    return {'items': [service.public_settlement(row) for row in rows]}


@router.get('/merchant/settlements/{settlement_id}')
async def merchant_detail(settlement_id: UUID, merchant: MerchantIdentity, db: DBSession):
    row = await db.scalar(select(MerchantSettlement).where(MerchantSettlement.id == settlement_id,
                                                          MerchantSettlement.merchant_id == merchant.id))
    if not row:
        raise HTTPException(404, 'Settlement not found')
    return service.public_settlement(row)


@router.get('/admin/settlements/{country}')
async def admin_queue(country: Literal['RU','KZ','TJ'], actor: StaffIdentity, db: DBSession, page: int = 1):
    rows = await service.settlement_history(db, country=country, page=page)
    return {'country': country, 'items': [service.public_settlement(row) for row in rows]}


@router.post('/admin/settlements/{country}/{settlement_id}/{action}')
async def admin_action(country: Literal['RU','KZ','TJ'], settlement_id: UUID,
    action: Literal['approve','send','complete','reject','reconcile','resolve-not-sent'],
    payload: SettlementAction, request: Request,
    actor: StaffIdentity, db: DBSession):
    params = {'settlement_id': settlement_id, 'actor_id': actor.id,
              'expected_country': country, 'actor_ip': client_ip(request)}
    allowed = {'approve': set(), 'send': {'tx_hash','sent_confirmed'},
               'complete': {'tx_hash','execution_confirmed','execution_reference'},
               'reject': {'reason'}, 'reconcile': {'reason'},
               'resolve-not-sent': {'reason','execution_reference','not_sent_confirmed'}}
    if payload.model_fields_set - allowed[action]:
        raise HTTPException(422, 'Unexpected fields for settlement action')
    try:
        if action == 'approve':
            row = await service.approve_merchant_settlement(db, **params)
        elif action == 'send':
            row = await service.register_merchant_settlement_sent(db, **params, tx_hash=payload.tx_hash,
                                                                  sent_confirmed=payload.sent_confirmed)
        elif action == 'complete':
            row = await service.complete_merchant_settlement(db, **params, tx_hash=payload.tx_hash,
                execution_confirmed=payload.execution_confirmed, execution_reference=payload.execution_reference)
        elif action == 'reject':
            row = await service.reject_merchant_settlement(db, **params, reason=payload.reason)
        elif action == 'reconcile':
            row = await service.flag_merchant_settlement_reconciliation(db, **params, reason=payload.reason)
        else:
            row = await service.resolve_merchant_settlement_not_sent(db, **params, reason=payload.reason,
                evidence_reference=payload.execution_reference, not_sent_confirmed=payload.not_sent_confirmed)
        await db.commit()
    except service.MerchantSettlementError as exc:
        await db.rollback()
        raise error(exc) from exc
    return service.public_settlement(row)
