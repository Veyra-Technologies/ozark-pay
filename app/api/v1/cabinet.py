from app.services.trader_work import trader_account_id
from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile
from uuid import UUID
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.api.deps import current_user, require_roles
from app.core.enums import DepositStatus, PayoutStatus, Role
from app.models import User, Merchant, Balance, Deposit, Payout, Requisite, Appeal, AppealMessage
from app.schemas.common import SmsIn, AppealIn
from app.services.sms import ingest_sms
router=APIRouter(prefix='/cabinet', tags=['cabinet'])

STAFF_ROLES = {Role.superadmin.value, Role.admin.value, Role.support.value}
TRADER_ROLES = {Role.operator.value, Role.trader.value}

async def my_merchant(db, user):
    m=(await db.execute(select(Merchant).where(Merchant.owner_id==user.id))).scalar_one_or_none()
    if not m: raise HTTPException(404,'merchant not found')
    return m

def _deposit_out(dep: Deposit) -> dict:
    return {
        'id': dep.id,
        'external_id': dep.external_id,
        'amount': dep.amount,
        'currency': dep.currency,
        'method': dep.method,
        'status': dep.status,
        'created_at': dep.created_at,
        'updated_at': dep.updated_at,
    }

def _payout_out(payout: Payout) -> dict:
    return {
        'id': payout.id,
        'external_id': payout.external_id,
        'amount': payout.amount,
        'currency': payout.currency,
        'method': payout.method,
        'status': payout.status,
        'created_at': payout.created_at,
        'updated_at': payout.updated_at,
    }

async def _operation_visible_to_user(db: AsyncSession, user: User, operation_type: str, operation_id):
    operation_type = operation_type.strip().lower()
    if operation_type not in {'deposit', 'payout'}:
        raise HTTPException(422, 'operation_type must be deposit or payout')

    model = Deposit if operation_type == 'deposit' else Payout
    operation = (await db.execute(select(model).where(model.id == operation_id))).scalar_one_or_none()
    if not operation:
        raise HTTPException(404, 'operation not found')

    if user.role in STAFF_ROLES:
        return operation

    if user.role == Role.merchant.value:
        merchant = await my_merchant(db, user)
        if operation.merchant_id != merchant.id:
            raise HTTPException(404, 'operation not found')
        return operation

    if user.role in TRADER_ROLES and operation_type == 'deposit':
        if not operation.requisites_id:
            raise HTTPException(404, 'operation not found')
        req = (await db.execute(select(Requisite).where(Requisite.id == operation.requisites_id))).scalar_one_or_none()
        if not req or not trader_account_id(user) or req.trader_id != trader_account_id(user):
            raise HTTPException(404, 'operation not found')
        return operation

    raise HTTPException(403, 'not enough permissions')

@router.get('/wallet')
async def wallet(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    currency = 'USDT' if m.working_country == 'KZ' else 'RUB'
    rows=(await db.execute(select(Balance).where(Balance.merchant_id==m.id))).scalars().all()
    b=next((row for row in rows if row.currency == currency), None)
    return {'merchant':m.name,'country':m.working_country,'currency':currency,
            'available':b.available if b else 0,'frozen':b.frozen if b else 0,'holds':b.frozen if b else 0,
            'balances':[{'currency':row.currency,'available':row.available,'frozen':row.frozen} for row in rows],
            'history_endpoint':'/api/v1/crossborder/operations' if currency == 'USDT' else '/api/v1/cabinet/operations'}


@router.get('/working-profile')
async def working_profile(db: AsyncSession=Depends(get_db), user: User=Depends(require_roles(Role.operator, Role.trader))):
    from app.services.trader_work import accepts_currency
    account_id = trader_account_id(user)
    account = await db.get(User, account_id) if account_id else None
    ge_route = bool(account and account.working_country == 'GE' and account.is_active
                    and not account.is_locked and not account.is_archived)
    return {'trader_id': account_id, 'working_country': account.working_country if account else None,
            'working_currency': account.working_currency if account else None,
            'working_rate_source': account.working_rate_source if account else None,
            'payment_methods': ['sbp', 'c2c', 'mobile_commerce'] if accepts_currency(account) else [],
            'settlement_currency': 'RUB', 'legacy_balance_currency': 'RUB',
            'crossborder_settlement_currency': 'USDT', 'crossborder_collateral_currency': 'USDT',
            'crossborder_bank_route_ready': ge_route,
            'crossborder_directions': ['kz-ge'] if ge_route else [],
            'crossborder_confirmation': 'manual_or_signed_webhook' if ge_route else None,
            'country_editable': False}


@router.get('/working-profile/quote')
async def working_quote(db: AsyncSession=Depends(get_db), user: User=Depends(require_roles(Role.operator, Role.trader))):
    from app.services.work_rates import profile_quote
    account_id = trader_account_id(user)
    return await profile_quote(await db.get(User, account_id) if account_id else None, db=db)

@router.get('/operations')
async def operations(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    deps=(await db.execute(select(Deposit).where(Deposit.merchant_id==m.id).order_by(Deposit.created_at.desc()).limit(100))).scalars().all()
    pays=(await db.execute(select(Payout).where(Payout.merchant_id==m.id).order_by(Payout.created_at.desc()).limit(100))).scalars().all()
    return {'deposits':[_deposit_out(dep) for dep in deps],'payouts':[_payout_out(payout) for payout in pays]}

@router.get('/statistics')
async def statistics(db: AsyncSession=Depends(get_db), user: User=Depends(current_user)):
    m=await my_merchant(db,user)
    if m.working_country == 'KZ':
        from app.services.crossborder_operations import statistics as crossborder_statistics
        return await crossborder_statistics(db, user)
    turnover=(await db.execute(select(func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.merchant_id==m.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    successful_deposits=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==m.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    failed_deposits=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==m.id, Deposit.status.in_([DepositStatus.failed.value,DepositStatus.cancelled.value,DepositStatus.expired.value])))).scalar_one()
    successful_payouts=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==m.id, Payout.status==PayoutStatus.completed.value))).scalar_one()
    failed_payouts=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==m.id, Payout.status.in_([PayoutStatus.failed.value,PayoutStatus.cancelled.value,PayoutStatus.rejected.value])))).scalar_one()
    return {'turnover':turnover,'successful_deposits':successful_deposits,'failed_deposits':failed_deposits,'successful_payouts':successful_payouts,'failed_payouts':failed_payouts}

@router.post('/sms')
async def sms(data: SmsIn, db: AsyncSession=Depends(get_db), user: User=Depends(require_roles(Role.superadmin,Role.admin,Role.support,Role.operator,Role.trader))):
    s=await ingest_sms(db,data.provider_message_id,data.sender,data.body); await db.commit(); await db.refresh(s); return s

@router.post('/appeals')
async def create_appeal(
    operation_id: UUID = Form(...), operation_type: str = Form('deposit'),
    trader_id: UUID = Form(...), amount: str = Form(...), requisite: str = Form(...),
    recipient_bank: str = Form(...), operation_date: str = Form(...),
    message: str = Form(''), receipt_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_roles(Role.merchant)),
):
    from app.services.appeal_evidence import create_deposit_appeal, save_upload
    if operation_type != 'deposit':
        raise HTTPException(422, 'Поддерживаются апелляции по пополнениям с чеком')
    try:
        receipt = await save_upload(receipt_file)
        appeal = await create_deposit_appeal(db, user=user, deposit_id=operation_id,
            trader_id=trader_id, amount=amount, requisite=requisite,
            recipient_bank=recipient_bank, operation_date=operation_date,
            receipt=receipt, message=message)
        await db.commit()
        return {'id': appeal.id, 'status': appeal.status, 'evidence_validated': appeal.evidence_validated}
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
