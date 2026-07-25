import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.api.deps import merchant_auth
from app.models import Merchant, Deposit, Payout, Balance, ApiRequestLog, WebhookEvent, Requisite
from app.schemas.common import PaymentCreate, PayoutCreate
from app.core.enums import DepositStatus, PaymentMethod, PayoutStatus, RiskDecision
from app.core.payment_methods import normalize_payment_method, payment_method_label
from app.core.requisite_providers import requisite_provider_payload
from app.core.security import reveal_text
from app.core.client_ip import client_ip
from app.services.ledger import hold, LedgerError
from app.services.payouts import release_payout_hold
from app.services.risk import assess_operation, record_risk_event
from app.services.antiscam import check_payment_result_for_risk
from app.services.requisites import release_trader_deposit_hold, select_deposit_requisite
from app.services.audit import audit
from app.services.fee_tiers import (
    FeeConfigurationError,
    NegativePlatformMarginError,
    create_operation_fee_snapshot,
    resolve_fee_rule,
)
from app.services.webhook_enqueue import enqueue_webhook_delivery
router=APIRouter(prefix='/merchant', tags=['merchant-api'])
DEPOSIT_TTL_SECONDS = 15 * 60
DEPOSIT_TTL = timedelta(seconds=DEPOSIT_TTL_SECONDS)

def _enqueue_webhook(event_id) -> bool:
    return enqueue_webhook_delivery(event_id)

async def log_req(db, merchant, request, status=200):
    db.add(ApiRequestLog(merchant_id=merchant.id, method=request.method, path=request.url.path, ip=client_ip(request), status_code=status, request_id=request.headers.get('X-Request-ID','')))

IDEMPOTENCY_CONFLICT = 'idempotency_conflict'

MERCHANT_ERROR_MESSAGES = {
    'idempotency_conflict': 'Same idempotency key or external_id was used with different payment fields.',
    'metadata_callback_url_not_supported': 'metadata.callback_url is not supported; configure merchant webhook_url in integration settings.',
    'not_found': 'Operation was not found.',
    'risk_denied': 'Operation was rejected by risk control.',
    'manual_review_required': 'Operation requires manual risk review.',
    'no_active_requisites': 'No active requisites are available for the requested payment method.',
    'requisites_busy_or_limited': 'All matching requisites are busy, over limits, or assigned traders have insufficient available balance.',
    'paid_deposit_cannot_be_cancelled': 'Paid deposit cannot be cancelled.',
    'payout_status_not_cancellable': 'Payout status does not allow cancellation.',
    'ledger_error': 'Balance operation failed.',
    'fee_rule_missing': 'No active fee rule matches this operation.',
    'fee_rule_ambiguous': 'Multiple fee rules match this operation.',
    'negative_platform_margin': 'The configured executor fee exceeds the merchant fee.',
    'fee_configuration_error': 'Fee configuration blocks this operation.',
    'webhook_not_found': 'Webhook event was not found.',
}


def merchant_error(status_code: int, code: str, message: str | None = None, details: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code,
        {
            'code': code,
            'message': message or MERCHANT_ERROR_MESSAGES.get(code, code),
            'details': details or {},
        },
    )


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _aware(dt).isoformat().replace('+00:00', 'Z')


def deposit_expires_at(dep: Deposit) -> datetime:
    created_at = getattr(dep, 'created_at', None) or datetime.now(timezone.utc)
    return _aware(created_at) + DEPOSIT_TTL


def deposit_ttl_seconds() -> int:
    return DEPOSIT_TTL_SECONDS


def deposit_ttl_fields(dep: Deposit) -> dict:
    return {
        'expires_at': _iso_z(deposit_expires_at(dep)),
        'ttl_seconds': deposit_ttl_seconds(),
    }


def _requisite_error_code(message: str) -> str:
    lower = message.lower()
    if 'нет актив' in lower or 'no active' in lower or 'not supported' in lower:
        return 'no_active_requisites'
    return 'requisites_busy_or_limited'


def _same_deposit(dep: Deposit, data: PaymentCreate, idempotency_key: str) -> bool:
    return (
        dep.external_id == data.external_id
        and dep.idempotency_key == idempotency_key
        and dep.amount == data.amount
        and dep.currency == data.currency
        and dep.method == data.method.value
    )


def _same_payout(payout: Payout, data: PayoutCreate, idempotency_key: str) -> bool:
    return (
        payout.external_id == data.external_id
        and payout.idempotency_key == idempotency_key
        and payout.amount == data.amount
        and payout.currency == data.currency
        and payout.method == data.method.value
        and payout.destination == data.destination
    )


async def _find_existing_deposit(db: AsyncSession, merchant: Merchant, idempotency_key: str, external_id: str) -> Deposit | None:
    by_idempotency = (await db.execute(
        select(Deposit).where(Deposit.merchant_id == merchant.id, Deposit.idempotency_key == idempotency_key)
    )).scalar_one_or_none()
    by_external = (await db.execute(
        select(Deposit).where(Deposit.merchant_id == merchant.id, Deposit.external_id == external_id)
    )).scalar_one_or_none()
    if by_idempotency and by_external and by_idempotency.id != by_external.id:
        raise merchant_error(409, IDEMPOTENCY_CONFLICT)
    return by_idempotency or by_external


async def _find_existing_payout(db: AsyncSession, merchant: Merchant, idempotency_key: str, external_id: str) -> Payout | None:
    by_idempotency = (await db.execute(
        select(Payout).where(Payout.merchant_id == merchant.id, Payout.idempotency_key == idempotency_key)
    )).scalar_one_or_none()
    by_external = (await db.execute(
        select(Payout).where(Payout.merchant_id == merchant.id, Payout.external_id == external_id)
    )).scalar_one_or_none()
    if by_idempotency and by_external and by_idempotency.id != by_external.id:
        raise merchant_error(409, IDEMPOTENCY_CONFLICT)
    return by_idempotency or by_external


async def _idempotent_deposit_response(db: AsyncSession, dep: Deposit) -> dict:
    return {
        'id': dep.id,
        'external_id': dep.external_id,
        'status': dep.status,
        'amount': dep.amount,
        'method': dep.method,
        **deposit_ttl_fields(dep),
        'idempotent': True,
        'payment_details': await _payment_details_for_deposit(db, dep),
    }


def _idempotent_payout_response(payout: Payout) -> dict:
    return {
        'id': payout.id,
        'external_id': payout.external_id,
        'status': payout.status,
        'amount': payout.amount,
        'idempotent': True,
    }


async def _existing_deposit_or_conflict(db: AsyncSession, merchant: Merchant, data: PaymentCreate, idempotency_key: str) -> Deposit | None:
    existing = await _find_existing_deposit(db, merchant, idempotency_key, data.external_id)
    if existing and not _same_deposit(existing, data, idempotency_key):
        raise merchant_error(409, IDEMPOTENCY_CONFLICT)
    return existing


async def _existing_payout_or_conflict(db: AsyncSession, merchant: Merchant, data: PayoutCreate, idempotency_key: str) -> Payout | None:
    existing = await _find_existing_payout(db, merchant, idempotency_key, data.external_id)
    if existing and not _same_payout(existing, data, idempotency_key):
        raise merchant_error(409, IDEMPOTENCY_CONFLICT)
    return existing

async def _payment_details_for_deposit(db: AsyncSession, dep: Deposit) -> dict | None:
    if not dep.requisites_id:
        return None
    req = (await db.execute(select(Requisite).where(Requisite.id == dep.requisites_id))).scalar_one_or_none()
    if not req:
        return None
    method = normalize_payment_method(req.method, canonical_mobile=True) or req.method
    provider = requisite_provider_payload(method, req.bank_code, req.operator_code, req.bank_name)
    return {
        'receiver_name': req.full_name or req.owner_name,
        'requisite': reveal_text(req.value_encrypted),
        'bank': provider.get('bank', ''),
        'exact_amount': str(dep.amount),
        'currency': dep.currency,
        'method': method,
        'payment_method': method,
        'method_label': payment_method_label(method),
        **provider,
    }

@router.post('/deposits')
async def create_deposit(data: PaymentCreate, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth), idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    idem = (idempotency_key or data.external_id).strip()[:128]
    callback_url = (data.metadata or {}).get('callback_url')
    if callback_url:
        await log_req(db, merchant, request, 422)
        await db.commit()
        raise merchant_error(422, 'metadata_callback_url_not_supported')
    existing = await _existing_deposit_or_conflict(db, merchant, data, idem)
    if existing:
        await log_req(db,merchant,request); await db.commit()
        return await _idempotent_deposit_response(db, existing)
    ip = client_ip(request)
    risk = await assess_operation(db, merchant_id=merchant.id, operation_type='deposit', amount=data.amount, method=data.method.value, ip=ip)
    if risk.decision != RiskDecision.allow:
        await record_risk_event(db, merchant_id=merchant.id, operation_type='deposit', operation_id=None, result=risk)
        await log_req(db,merchant,request,403); await db.commit()
        if risk.decision == RiskDecision.deny:
            raise merchant_error(403, 'risk_denied', risk.reason or MERCHANT_ERROR_MESSAGES['risk_denied'])
        raise merchant_error(403, 'manual_review_required')
    try:
        merchant_rate_rule = await resolve_fee_rule(
            db,
            entity_type='merchant',
            entity_id=merchant.id,
            fee_side='merchant_fee',
            payment_method=data.method.value,
            currency=data.currency,
            amount=data.amount,
        )
    except FeeConfigurationError as exc:
        code = getattr(exc, 'code', 'fee_configuration_error')
        await audit(
            db,
            'deposit_fee_configuration_blocked',
            'merchant',
            target_id=merchant.id,
            ip=ip,
            details={'code': code, 'method': data.method.value, 'currency': data.currency},
        )
        await log_req(db, merchant, request, 409)
        await db.commit()
        raise merchant_error(409, code) from exc
    try:
        selected_requisite = await select_deposit_requisite(
            db,
            merchant_id=merchant.id,
            method=data.method.value,
            amount=data.amount,
            merchant_rate_rule=merchant_rate_rule,
        )
    except FeeConfigurationError as exc:
        code = getattr(exc, 'code', 'fee_configuration_error')
        await audit(
            db,
            'deposit_fee_configuration_blocked',
            'merchant',
            target_id=merchant.id,
            ip=ip,
            details={'code': code, 'method': data.method.value, 'currency': data.currency},
        )
        await log_req(db, merchant, request, 409)
        await db.commit()
        raise merchant_error(409, code) from exc
    except ValueError as exc:
        await log_req(db,merchant,request,409); await db.commit()
        message = str(exc)
        raise merchant_error(409, _requisite_error_code(message), message)
    dep=Deposit(merchant_id=merchant.id, external_id=data.external_id, idempotency_key=idem, amount=data.amount, currency=data.currency, method=data.method.value, status=DepositStatus.pending.value, requisites_id=selected_requisite.requisite.id, client_ip=ip, metadata_json={**(data.metadata or {}), **selected_requisite.settlement_metadata(), 'risk_score': risk.score, 'risk_reason': risk.reason, 'integration_mode': getattr(request.state, 'merchant_api_key_mode', 'unknown')})
    db.add(dep)
    try:
        await db.flush()
        await create_operation_fee_snapshot(
            db,
            deposit=dep,
            merchant_rule=merchant_rate_rule,
            executor_rule=selected_requisite.executor_rate_rule,
            executor_type=selected_requisite.executor_type,
            executor_id=selected_requisite.executor_id,
        )
    except IntegrityError as exc:
        await db.rollback()
        existing = await _existing_deposit_or_conflict(db, merchant, data, idem)
        if existing:
            await log_req(db,merchant,request); await db.commit()
            return await _idempotent_deposit_response(db, existing)
        await log_req(db,merchant,request,409); await db.commit()
        raise merchant_error(409, IDEMPOTENCY_CONFLICT) from exc
    await record_risk_event(db, merchant_id=merchant.id, operation_type='deposit', operation_id=dep.id, result=risk)
    await log_req(db,merchant,request)
    await db.commit(); await db.refresh(dep)
    return {'id':dep.id,'external_id':dep.external_id,'status':dep.status,'amount':dep.amount,'method':dep.method,**deposit_ttl_fields(dep),'risk_decision':risk.decision.value,'payment_details':selected_requisite.payment_details()}

@router.get('/deposits/{external_id}')
async def deposit_status(external_id: str, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    dep=(await db.execute(select(Deposit).where(Deposit.merchant_id==merchant.id, Deposit.external_id==external_id))).scalar_one_or_none()
    if not dep: raise merchant_error(404, 'not_found')
    await log_req(db,merchant,request); await db.commit()
    return {'id':dep.id,'external_id':dep.external_id,'status':dep.status,'amount':dep.amount,**deposit_ttl_fields(dep),'payment_details':await _payment_details_for_deposit(db, dep)}

@router.post('/deposits/{external_id}/cancel')
async def cancel_deposit(external_id: str, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    dep=(await db.execute(select(Deposit).where(Deposit.merchant_id==merchant.id, Deposit.external_id==external_id).with_for_update())).scalar_one_or_none()
    if not dep:
        await log_req(db,merchant,request,404); await db.commit()
        raise merchant_error(404, 'not_found')
    if dep.status == DepositStatus.paid.value:
        await log_req(db,merchant,request,409); await db.commit()
        raise merchant_error(409, 'paid_deposit_cannot_be_cancelled')
    if dep.status not in [DepositStatus.failed.value, DepositStatus.expired.value, DepositStatus.cancelled.value]:
        await release_trader_deposit_hold(db, dep)
        dep.status = DepositStatus.cancelled.value
        await check_payment_result_for_risk(db, dep)
    await log_req(db,merchant,request)
    await db.commit()
    return {'id':dep.id,'external_id':dep.external_id,'status':dep.status,'amount':dep.amount}

@router.post('/payouts')
async def create_payout(data: PayoutCreate, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth), idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    idem = (idempotency_key or data.external_id).strip()[:128]
    existing = await _existing_payout_or_conflict(db, merchant, data, idem)
    if existing:
        await log_req(db,merchant,request); await db.commit()
        return _idempotent_payout_response(existing)
    risk = await assess_operation(db, merchant_id=merchant.id, operation_type='payout', amount=data.amount, method=data.method.value, ip=client_ip(request), destination=data.destination)
    if risk.decision != RiskDecision.allow:
        await record_risk_event(db, merchant_id=merchant.id, operation_type='payout', operation_id=None, result=risk)
        await log_req(db,merchant,request,403); await db.commit()
        if risk.decision == RiskDecision.deny:
            raise merchant_error(403, 'risk_denied', risk.reason or MERCHANT_ERROR_MESSAGES['risk_denied'])
        raise merchant_error(403, 'manual_review_required')
    p=Payout(merchant_id=merchant.id, external_id=data.external_id, idempotency_key=idem, amount=data.amount, currency=data.currency, method=data.method.value, status=PayoutStatus.pending.value, destination=data.destination, metadata_json={**(data.metadata or {}), 'risk_score': risk.score, 'risk_reason': risk.reason, 'integration_mode': getattr(request.state, 'merchant_api_key_mode', 'unknown')})
    db.add(p)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        existing = await _existing_payout_or_conflict(db, merchant, data, idem)
        if existing:
            await log_req(db,merchant,request); await db.commit()
            return _idempotent_payout_response(existing)
        await log_req(db,merchant,request,409); await db.commit()
        raise merchant_error(409, IDEMPOTENCY_CONFLICT) from exc
    try: await hold(db, merchant.id, data.amount, p.id, f'payout-hold:{merchant.id}:{data.external_id}', 'payout hold')
    except LedgerError as e:
        await db.rollback()
        await log_req(db,merchant,request,409); await db.commit()
        raise merchant_error(409, 'ledger_error', str(e))
    await record_risk_event(db, merchant_id=merchant.id, operation_type='payout', operation_id=p.id, result=risk)
    await log_req(db,merchant,request); await db.commit(); await db.refresh(p)
    return {'id':p.id,'external_id':p.external_id,'status':p.status,'amount':p.amount,'risk_decision':risk.decision.value}

@router.get('/payouts/{external_id}')
async def payout_status(external_id: str, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    p=(await db.execute(select(Payout).where(Payout.merchant_id==merchant.id, Payout.external_id==external_id))).scalar_one_or_none()
    if not p: raise merchant_error(404, 'not_found')
    await log_req(db,merchant,request); await db.commit()
    return {'id':p.id,'external_id':p.external_id,'status':p.status,'amount':p.amount}


@router.post('/payouts/{external_id}/cancel')
async def cancel_payout(external_id: str, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    p=(await db.execute(select(Payout).where(Payout.merchant_id==merchant.id, Payout.external_id==external_id).with_for_update())).scalar_one_or_none()
    if not p:
        await log_req(db,merchant,request,404); await db.commit()
        raise merchant_error(404, 'not_found')
    try:
        await release_payout_hold(db, p, PayoutStatus.cancelled, 'payout cancelled by merchant')
    except ValueError as exc:
        await log_req(db,merchant,request,409); await db.commit()
        raise merchant_error(409, 'payout_status_not_cancellable', str(exc))
    await log_req(db,merchant,request)
    await db.commit()
    return {'id':p.id,'external_id':p.external_id,'status':p.status,'amount':p.amount}

@router.get('/balance')
async def balance(request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    b=(await db.execute(select(Balance).where(Balance.merchant_id==merchant.id))).scalar_one_or_none()
    await log_req(db,merchant,request); await db.commit()
    return {'available': b.available if b else 0, 'frozen': b.frozen if b else 0, 'currency': b.currency if b else 'RUB'}

@router.get('/statistics')
async def statistics(request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    dep_success=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==merchant.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    dep_failed=(await db.execute(select(func.count()).select_from(Deposit).where(Deposit.merchant_id==merchant.id, Deposit.status.in_([DepositStatus.failed.value,DepositStatus.cancelled.value,DepositStatus.expired.value])))).scalar_one()
    pay_success=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==merchant.id, Payout.status==PayoutStatus.completed.value))).scalar_one()
    pay_failed=(await db.execute(select(func.count()).select_from(Payout).where(Payout.merchant_id==merchant.id, Payout.status.in_([PayoutStatus.failed.value,PayoutStatus.cancelled.value,PayoutStatus.rejected.value])))).scalar_one()
    turnover=(await db.execute(select(func.coalesce(func.sum(Deposit.amount),0)).where(Deposit.merchant_id==merchant.id, Deposit.status==DepositStatus.paid.value))).scalar_one()
    await log_req(db,merchant,request); await db.commit()
    return {'turnover':turnover,'successful_deposits':dep_success,'failed_deposits':dep_failed,'successful_payouts':pay_success,'failed_payouts':pay_failed,'conversion': float(dep_success / max(dep_success+dep_failed,1))}

def _deposit_operation_out(dep: Deposit) -> dict:
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


def _payout_operation_out(payout: Payout) -> dict:
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


@router.get('/operations')
async def operations(
    request: Request,
    db: AsyncSession=Depends(get_db),
    merchant: Merchant=Depends(merchant_auth),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    amount: Decimal | None = Query(default=None, ge=Decimal('0.01')),
    operation_id: str | None = Query(default=None, max_length=128),
):
    dep_filters = [Deposit.merchant_id == merchant.id]
    pay_filters = [Payout.merchant_id == merchant.id]
    if date_from:
        dep_filters.append(Deposit.created_at >= date_from)
        pay_filters.append(Payout.created_at >= date_from)
    if date_to:
        dep_filters.append(Deposit.created_at <= date_to)
        pay_filters.append(Payout.created_at <= date_to)
    if amount is not None:
        normalized_amount = Decimal(amount).quantize(Decimal('0.01'))
        dep_filters.append(Deposit.amount == normalized_amount)
        pay_filters.append(Payout.amount == normalized_amount)
    if operation_id and operation_id.strip():
        needle = operation_id.strip()
        parsed_uuid = None
        try:
            parsed_uuid = uuid.UUID(needle)
        except ValueError:
            parsed_uuid = None
        dep_operation_filter = Deposit.external_id.ilike(f'%{needle}%')
        pay_operation_filter = Payout.external_id.ilike(f'%{needle}%')
        if parsed_uuid:
            dep_operation_filter = or_(Deposit.id == parsed_uuid, dep_operation_filter)
            pay_operation_filter = or_(Payout.id == parsed_uuid, pay_operation_filter)
        dep_filters.append(dep_operation_filter)
        pay_filters.append(pay_operation_filter)

    dep_total=(await db.execute(select(func.count()).select_from(Deposit).where(*dep_filters))).scalar_one()
    pay_total=(await db.execute(select(func.count()).select_from(Payout).where(*pay_filters))).scalar_one()
    deps=(await db.execute(select(Deposit).where(*dep_filters).order_by(Deposit.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    pays=(await db.execute(select(Payout).where(*pay_filters).order_by(Payout.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    await log_req(db,merchant,request); await db.commit()
    return {
        'limit': limit,
        'offset': offset,
        'deposits_total': dep_total,
        'payouts_total': pay_total,
        'deposits':[_deposit_operation_out(dep) for dep in deps],
        'payouts':[_payout_operation_out(payout) for payout in pays],
    }

@router.post('/webhooks/{event_id}/retry')
async def retry_webhook(event_id: str, request: Request, db: AsyncSession=Depends(get_db), merchant: Merchant=Depends(merchant_auth)):
    ev=(await db.execute(select(WebhookEvent).where(WebhookEvent.id==event_id, WebhookEvent.merchant_id==merchant.id))).scalar_one_or_none()
    if not ev:
        await log_req(db,merchant,request,404); await db.commit()
        raise merchant_error(404, 'webhook_not_found')
    ev.status='queued'
    ev.attempts = 0
    ev.next_retry_at = None
    ev.last_error = None
    await log_req(db,merchant,request)
    await db.commit()
    webhook_enqueued = _enqueue_webhook(ev.id)
    return {'ok':True,'status':'queued','event_id':ev.id,'webhook_enqueued':webhook_enqueued}
