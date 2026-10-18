from typing import Annotated
from uuid import UUID

from app.core.client_ip import client_ip
from app.core.session import realm_cabinet_path, realm_for_role, request_auth_realm
from app.core.settlement_policy import SETTLEMENT_CURRENCIES
from app.db.session import get_db
from app.models import Merchant, MerchantSettlement
from app.services import settlements as service
from app.web.ui_messages import cabinet_redirect_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix='/cabinet/settlements')
DBSession = Annotated[AsyncSession, Depends(get_db)]


async def actor(request, db, *, admin=False):
    from app.web.routes import (
        _session_identity_changed,
        get_current_web_user,
        staff_2fa_setup_required,
    )
    if await _session_identity_changed(request):
        raise HTTPException(409, 'Сессия изменилась. Обновите страницу.')
    user = await get_current_web_user(request, db)
    if not user:
        raise HTTPException(401, 'Требуется вход')
    if admin:
        if user.role not in ('admin','superadmin'):
            raise HTTPException(403, 'Недостаточно прав')
        if staff_2fa_setup_required(user) or not request.scope.get('session',{}).get('twofa_verified'):
            raise HTTPException(403, 'Требуется подтверждённая 2FA-сессия')
    elif user.role != 'merchant':
        raise HTTPException(403, 'Заявку создаёт только сам мерчант')
    request.state.settlement_actor_role = user.role
    return user


def redirect(request, user, *, code=None, country=None):
    role = getattr(request.state, 'settlement_actor_role', None) or user.role
    base = realm_cabinet_path(request_auth_realm(request) or realm_for_role(role))
    section = 'settle-' + country.lower() if country else getattr(request.state, 'settlement_section', 'sec4')
    return RedirectResponse(cabinet_redirect_url(base, section=section, success=not code,
                            extra={'ui_error':code} if code else None), status_code=303)


def check_form(form, allowed):
    if set(form) - set(allowed) - {'csrf_token','session_view'}:
        raise HTTPException(422, 'Недопустимые поля: страна, кабинет и кошелёк определяются сервером')


@router.get('/quote')
async def quote(request: Request, db: DBSession, amount_usdt: str | None = None):
    user = await actor(request, db)
    merchant = await db.scalar(select(Merchant).where(Merchant.owner_id==user.id, Merchant.is_archived.is_(False)))
    if not merchant:
        raise HTTPException(404, 'Кабинет не найден')
    try:
        result = (await service.settlement_preview(db, merchant.id, amount_usdt) if amount_usdt is not None
                  else await service.merchant_settlement_quote(db, merchant.id, allow_unavailable=True))
        from decimal import Decimal

        from fastapi.encoders import jsonable_encoder
        return JSONResponse(jsonable_encoder(result, custom_encoder={Decimal:str}), headers={'Cache-Control':'no-store'})
    except service.MerchantSettlementError as exc:
        return JSONResponse({'code':exc.code,'can_request':False},status_code=422)


@router.post('/request')
async def create(request: Request, db: DBSession):
    user = await actor(request, db)
    merchant = await db.scalar(select(Merchant).where(Merchant.owner_id==user.id, Merchant.is_archived.is_(False)))
    if not merchant:
        raise HTTPException(404, 'Кабинет не найден')
    if merchant.working_country == 'TJ':
        request.state.settlement_section = 'sec10'
    form = await request.form()
    check_form(form, ('amount_usdt','trc20_address','idempotency_key','request_confirmed','expected_reserve_amount'))
    if 'request_confirmed' in form and str(form['request_confirmed']).lower() not in ('true','on','1'):
        raise HTTPException(422, 'Подтвердите создание заявки')
    try:
        await service.create_merchant_settlement(db, merchant_id=merchant.id, requested_by_id=user.id,
            amount_usdt=form.get('amount_usdt'), trc20_address=form.get('trc20_address'),
            idempotency_key=form.get('idempotency_key'), actor_ip=client_ip(request),
            expected_reserve_amount=form.get('expected_reserve_amount') or None)
        await db.commit()
    except service.MerchantSettlementRateUnavailable as exc:
        await db.rollback()
        return JSONResponse({'detail':exc.code,'code':exc.code},status_code=503)
    except service.MerchantSettlementError as exc:
        await db.rollback()
        return redirect(request,user,code=exc.code)
    return redirect(request,user)


@router.post('/{country}/{settlement_id}/{action}')
async def mutate(request: Request, country: str, settlement_id: str, action: str,
                 db: DBSession):
    user = await actor(request, db, admin=True)
    if country not in SETTLEMENT_CURRENCIES:
        raise HTTPException(404, 'Очередь не найдена')
    try:
        row_id = UUID(settlement_id)
    except ValueError:
        raise HTTPException(404,'Заявка не найдена')
    form = await request.form()
    params = {'settlement_id':row_id,'actor_id':user.id,'expected_country':country,'actor_ip':client_ip(request)}
    fields = {'approve':(), 'send':('tx_hash','sent_confirmed'),
              'complete':('execution_confirmed','execution_reference'), 'reject':('reject_reason',),
              'reconcile':('reason',), 'resolve-not-sent':('reason','execution_reference','not_sent_confirmed')}
    if action not in fields:
        raise HTTPException(404,'Действие не найдено')
    check_form(form,fields[action])
    truth = lambda name: str(form.get(name,'')).lower() in ('true','on','1')
    try:
        if action=='approve':
            await service.approve_merchant_settlement(db,**params)
        elif action=='send':
            await service.register_merchant_settlement_sent(db,**params,tx_hash=form.get('tx_hash'),sent_confirmed=truth('sent_confirmed'))
        elif action=='complete':
            await service.complete_merchant_settlement(db,**params,execution_confirmed=truth('execution_confirmed'),
                execution_reference=form.get('execution_reference'))
        elif action=='reject':
            await service.reject_merchant_settlement(db,**params,reason=form.get('reject_reason'))
        elif action=='reconcile':
            await service.flag_merchant_settlement_reconciliation(db,**params,reason=form.get('reason'))
        else:
            await service.resolve_merchant_settlement_not_sent(db,**params,reason=form.get('reason'),
                evidence_reference=form.get('execution_reference'),not_sent_confirmed=truth('not_sent_confirmed'))
        await db.commit()
    except service.MerchantSettlementError as exc:
        await db.rollback()
        return redirect(request,user,country=country,code=exc.code)
    return redirect(request,user,country=country)


@router.post('/{settlement_id}/approve')
async def legacy_approve(request: Request, settlement_id: str, db: DBSession):
    user = await actor(request, db, admin=True)
    try:
        row = await db.get(MerchantSettlement, UUID(settlement_id))
    except ValueError:
        row = None
    if not row:
        raise HTTPException(404,'Заявка не найдена')
    country = row.country
    form = await request.form()
    # The old address remains, but approval no longer declares a performed transfer.
    check_form(form,())
    try:
        await service.approve_merchant_settlement(db,settlement_id=row.id,actor_id=user.id,
                                                 expected_country=row.country,actor_ip=client_ip(request))
        await db.commit()
    except service.MerchantSettlementError as exc:
        await db.rollback()
        return redirect(request,user,country=country,code=exc.code)
    return redirect(request,user,country=country)


@router.post('/{settlement_id}/reject')
async def legacy_reject(request: Request, settlement_id: str, db: DBSession):
    user = await actor(request, db, admin=True)
    try:
        row = await db.get(MerchantSettlement, UUID(settlement_id))
    except ValueError:
        row = None
    if not row:
        raise HTTPException(404,'Заявка не найдена')
    country = row.country
    form = await request.form()
    check_form(form,('reject_reason',))
    try:
        await service.reject_merchant_settlement(db,settlement_id=row.id,actor_id=user.id,reason=form.get('reject_reason'),
                                                expected_country=row.country,actor_ip=client_ip(request))
        await db.commit()
    except service.MerchantSettlementError as exc:
        await db.rollback()
        return redirect(request,user,country=country,code=exc.code)
    return redirect(request,user,country=country)
