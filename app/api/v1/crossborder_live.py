"""Country tariffs, parser controls and currency-safe crossborder operations."""
import hashlib
import hmac
import time
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import merchant_auth, require_roles
from app.core.config import settings
from app.core.enums import Role
from app.db.session import get_db
from app.models import BusinessAccount, CrossBorderOperation, CrossBorderQuote, ParserAssignment, Merchant, ParserConfiguration, RateObservation, Requisite, TariffPlan, User
from app.core.security import decrypt_secret
from app.services import country_tariffs, crossborder_operations as operations, merchant_countries, rate_registry, parser_assignments
from app.services.audit import audit
from app.services.crossborder_money import CrossBorderError

router = APIRouter(prefix='/crossborder', tags=['crossborder-live'])
participant = require_roles(Role.admin, Role.superadmin, Role.merchant, Role.trader)
administrator = require_roles(Role.admin, Role.superadmin)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class QuoteIn(StrictInput):
    direction: Literal['kz-ge'] = 'kz-ge'
    merchant_id: UUID
    trader_id: UUID
    requisite_id: UUID
    amount_kzt: Decimal = Field(gt=0, le=Decimal('999999999999.99'), decimal_places=2)


class AcceptIn(StrictInput):
    quote_id: UUID
    external_id: str = Field(min_length=1, max_length=128)


class MerchantQuoteIn(StrictInput):
    direction: Literal['kz-ge'] = 'kz-ge'
    merchant_id: UUID | None = None  # Compatibility: identity is always taken from the HMAC key.
    trader_id: UUID | None = None
    requisite_id: UUID | None = None
    amount_kzt: Decimal = Field(gt=0, le=Decimal('999999999999.99'), decimal_places=2)


class MerchantOperationIn(StrictInput):
    external_id: str = Field(min_length=1, max_length=128)
    quote_id: UUID | None = None
    amount_kzt: Decimal | None = Field(default=None, gt=0, le=Decimal('999999999999.99'), decimal_places=2)
    trader_id: UUID | None = None
    requisite_id: UUID | None = None

    @model_validator(mode='after')
    def mode(self):
        if (self.quote_id is None) == (self.amount_kzt is None):
            raise ValueError('Передайте amount_kzt или сохранённую quote_id')
        if self.quote_id and (self.trader_id or self.requisite_id):
            raise ValueError('Маршрут уже зафиксирован в котировке')
        return self


class AssignmentIn(StrictInput):
    owner_type: Literal['merchant', 'trader']
    owner_id: UUID
    parser_id: str
    purpose: Literal['financial', 'informational']
    is_active: bool = True


async def merchant_v2(request: Request, merchant=Depends(merchant_auth)):
    if getattr(request.state, 'merchant_hmac_version', None) != 'v2':
        raise HTTPException(401, 'KZ → GE требует Merchant HMAC v2')
    return merchant


class ConfirmIn(StrictInput):
    actual_gel: Decimal = Field(gt=0, le=Decimal('999999999999.99'), decimal_places=2)
    received_at: datetime
    source: Literal['trader_manual', 'admin_manual', 'bank_webhook']
    reference: str = Field(min_length=1, max_length=200)
    event_key: str = Field(min_length=1, max_length=100)
    bank_credit_confirmed: bool = False


class ReasonIn(StrictInput):
    reason: str = Field(min_length=1, max_length=500)
    event_key: str = Field(min_length=1, max_length=100)


class TierIn(StrictInput):
    min: Decimal = Field(gt=0, decimal_places=8)
    max: Decimal | None = Field(default=None, gt=0, decimal_places=8)
    percent: Decimal = Field(ge=0, le=100, decimal_places=4)


class TariffIn(StrictInput):
    payment_method: Literal['kz-ge'] = 'kz-ge'
    party: Literal['merchant', 'trader']
    entity_id: UUID | None = None
    tiers: list[TierIn] = Field(min_length=1, max_length=100)
    effective_from: datetime
    is_active: bool = True
    reference: dict | None = None
    approve_reference: bool = False


class ProposalIn(StrictInput):
    party: Literal['merchant', 'trader']
    local_per_usdt: Decimal = Field(gt=0)
    reference_date: str
    local_tiers: list[TierIn] = Field(min_length=1, max_length=100)


class ParserIn(StrictInput):
    refresh_seconds: int = Field(ge=15, le=120)
    ttl_seconds: int = Field(ge=15, le=120)
    is_active: bool


class CountryIn(StrictInput):
    country: Literal['RU', 'KZ', 'GE']
    parser_id: str
    business_id: UUID | None = None


class MerchantParserIn(StrictInput):
    parser_id: str


class CabinetSettingsIn(StrictInput):
    currency: str
    parser_id: str
    tariff_plan_id: UUID
    methods: list[str] = Field(min_length=1, max_length=4)


class RuTariffIn(StrictInput):
    entity_id: UUID | None = None
    tiers: list[TierIn] = Field(min_length=1, max_length=100)


@router.get('/merchant-catalog')
async def merchant_catalog(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    from app.services.merchant_cabinets import catalog
    return await catalog(db)


@router.post('/merchants/{merchant_id}/settings')
async def cabinet_settings(merchant_id: UUID, data: CabinetSettingsIn,
                           db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    from app.services.merchant_cabinets import configure
    async def apply():
        merchant = await operations.merchant_access(db, actor, merchant_id)
        return await configure(db, actor=actor, merchant=merchant, **data.model_dump())
    return await commit_result(db, apply(), lambda m: {'merchant_id': str(m.id), 'country': m.working_country})


@router.post('/tariffs/ru')
async def ru_tariff(data: RuTariffIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    from app.services.merchant_cabinets import create_ru_plan
    return await commit_result(db, create_ru_plan(db, actor=actor, **data.model_dump()), country_tariffs.public_plan)


async def commit_result(db, action, serializer=lambda value: value):
    try:
        value = await action
        await db.commit()
        return serializer(value)
    except CrossBorderError as exc:
        await db.rollback()
        waiting = str(exc).startswith('awaiting_rate:')
        raise HTTPException(409, {'code': 'awaiting_rate' if waiting else exc.code,
                                  'reason': str(exc).partition(': ')[2] if waiting else str(exc)}) from exc


@router.get('/operations')
async def list_operations(db: AsyncSession = Depends(get_db), actor=Depends(participant),
                          limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows = await operations.visible_operations(db, actor, limit=limit, offset=offset)
    return [operations.public_operation(row) for row in rows]


@router.get('/statistics')
async def statistics(db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    return await operations.statistics(db, actor)


@router.get('/operations/{operation_id}')
async def get_operation(operation_id: UUID, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    try:
        return operations.public_operation(await operations.operation_access(db, actor, operation_id))
    except CrossBorderError as exc:
        raise HTTPException(404, 'Операция не найдена') from exc


@router.get('/operations/{operation_id}/requisite')
async def operation_requisite(operation_id: UUID, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    try:
        operation = await operations.operation_access(db, actor, operation_id)
    except CrossBorderError as exc:
        raise HTTPException(404, 'Операция не найдена') from exc
    requisite = await db.get(Requisite, operation.requisite_id)
    from app.core.ge_bank_requisites import operation_requisite_payload
    return JSONResponse(operation_requisite_payload(requisite, decrypt_secret(requisite.value_encrypted)),
                        headers={'Cache-Control': 'no-store'})


@router.post('/quotes')
async def quote(data: QuoteIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return await commit_result(db, operations.create_quote(db, actor=actor, **data.model_dump(exclude={'direction'})), operations.public_quote)


@router.get('/quotes/{quote_id}')
async def saved_quote(quote_id: UUID, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    row = await db.get(CrossBorderQuote, quote_id)
    if not row:
        raise HTTPException(404, 'Котировка не найдена')
    try:
        await operations.merchant_access(db, actor, row.merchant_id)
    except CrossBorderError as exc:
        raise HTTPException(404, 'Котировка не найдена') from exc
    return operations.public_quote(row)


@router.post('/operations')
async def accept(data: AcceptIn, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    raise HTTPException(403, 'Новые заявки доступны только через Merchant HMAC v2 API')


@router.post('/operations/{operation_id}/confirm')
async def confirm(operation_id: UUID, data: ConfirmIn, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    if data.source == 'bank_webhook':
        raise HTTPException(422, 'Банковский источник доступен только через подписанный webhook')
    return await commit_result(db, operations.confirm(db, actor=actor, operation_id=operation_id, **data.model_dump()), operations.public_operation)


@router.post('/operations/{operation_id}/cancel')
async def cancel(operation_id: UUID, data: ReasonIn, db: AsyncSession = Depends(get_db), actor=Depends(participant)):
    return await commit_result(db, operations.cancel(db, actor=actor, operation_id=operation_id, **data.model_dump()), operations.public_operation)


@router.post('/operations/{operation_id}/refund')
async def refund(operation_id: UUID, data: ReasonIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return await commit_result(db, operations.refund(db, actor=actor, operation_id=operation_id, **data.model_dump()), operations.public_operation)


@router.get('/tariffs')
async def tariffs(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    rows = (await db.execute(select(TariffPlan).order_by(TariffPlan.created_at.desc()).limit(500))).scalars().all()
    return [country_tariffs.public_plan(row) for row in rows]


@router.post('/tariffs')
async def create_tariff(data: TariffIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return await commit_result(db, country_tariffs.create_plan(db, actor=actor, **data.model_dump()), country_tariffs.public_plan)


@router.post('/tariffs/reference-proposal')
async def propose_tariff(data: ProposalIn, actor=Depends(administrator)):
    try:
        return country_tariffs.reference_proposal(**data.model_dump())
    except CrossBorderError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/assignments')
async def assignments(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return [parser_assignments.public_assignment(row) for row in await parser_assignments.current(db)]


@router.get('/assignments/history')
async def assignment_history(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    rows = (await db.execute(select(ParserAssignment).order_by(ParserAssignment.created_at.desc()).limit(500))).scalars().all()
    return [parser_assignments.public_assignment(row) for row in rows]


@router.post('/assignments')
async def assign_cabinet_parser(data: AssignmentIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return await commit_result(db, parser_assignments.assign(db, actor=actor, **data.model_dump()), parser_assignments.public_assignment)


@router.get('/parsers')
async def parsers(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    results = [await rate_registry.parser_state(db, parser_id) for parser_id in rate_registry.PARSER_POLICY if parser_id != 'xe']
    await db.commit()
    return results


@router.post('/parsers/{parser_id}')
async def configure_parser(parser_id: str, data: ParserIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    return await commit_result(db, rate_registry.configure(db, actor=actor, parser_id=parser_id, **data.model_dump()), rate_registry.public_config)


@router.post('/parsers/{parser_id}/test')
async def test_parser(parser_id: str, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    state = await rate_registry.parser_state(db, parser_id, refresh=True)
    await audit(db, 'parser_tested', 'parser', actor.id, parser_id,
                details={'available': state['available'], 'status': state['status']})
    await db.commit()
    return state


@router.get('/parsers/{parser_id}/history')
async def parser_history(parser_id: str, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    rows = (await db.execute(select(RateObservation).join(ParserConfiguration).where(ParserConfiguration.parser_id == parser_id)
        .order_by(RateObservation.fetched_at.desc()).limit(100))).scalars().all()
    configs = (await db.execute(select(ParserConfiguration).where(ParserConfiguration.parser_id == parser_id)
        .order_by(ParserConfiguration.version.desc()))).scalars().all()
    return {'configurations': [rate_registry.public_config(row) for row in configs],
            'observations': [{**row.snapshot, 'id': str(row.id)} for row in rows]}


@router.get('/businesses')
async def businesses(db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    rows = (await db.execute(select(BusinessAccount).order_by(BusinessAccount.name))).scalars().all()
    return [{'id': str(row.id), 'name': row.name} for row in rows]


@router.post('/merchants/{merchant_id}/country')
async def assign_country(merchant_id: UUID, data: CountryIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_id).with_for_update())).scalar_one_or_none()
    if not merchant:
        raise HTTPException(404, 'Мерчант не найден')
    return await commit_result(db, merchant_countries.assign_country(db, actor=actor, merchant=merchant, **data.model_dump()),
                               lambda row: {'id': str(row.id), 'country': row.working_country, 'parser_id': row.rate_parser_id})


@router.post('/merchants/{merchant_id}/parser')
async def assign_parser(merchant_id: UUID, data: MerchantParserIn, db: AsyncSession = Depends(get_db), actor=Depends(administrator)):
    merchant = (await db.execute(select(Merchant).where(Merchant.id == merchant_id).with_for_update())).scalar_one_or_none()
    if not merchant:
        raise HTTPException(404, 'Мерчант не найден')
    return await commit_result(db, merchant_countries.change_parser(db, actor=actor, merchant=merchant, **data.model_dump()),
                               lambda row: {'id': str(row.id), 'country': row.working_country, 'parser_id': row.rate_parser_id})


async def routing(db, merchant, trader_id=None, requisite_id=None):
    candidates = await merchant_routing_catalog(db, merchant)
    for candidate in candidates:
        rid, tid = UUID(candidate['requisite_id']), UUID(candidate['trader_id'])
        if (requisite_id and requisite_id != rid) or (trader_id and trader_id != tid):
            continue
        requisite = await db.get(Requisite, rid)
        count = len((await db.execute(select(CrossBorderOperation.id).where(
            CrossBorderOperation.requisite_id == rid, CrossBorderOperation.status == 'accepted'))).all())
        if count < max(1, requisite.simultaneous_limit):
            return tid, rid
    raise CrossBorderError('Нет доступного маршрута для этого кабинета')


@router.post('/merchant/quotes')
async def merchant_quote(data: MerchantQuoteIn, db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2)):
    async def action():
        if data.merchant_id and data.merchant_id != merchant.id:
            raise CrossBorderError('Кабинет не найден')
        trader_id, requisite_id = await routing(db, merchant, data.trader_id, data.requisite_id)
        return await operations.create_quote(db, actor=await db.get(User, merchant.owner_id),
            merchant_id=merchant.id, trader_id=trader_id, requisite_id=requisite_id, amount_kzt=data.amount_kzt)
    return await commit_result(db, action(), operations.public_quote)


@router.post('/merchant/operations')
async def merchant_accept(data: MerchantOperationIn, db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2)):
    async def action():
        actor = await db.get(User, merchant.owner_id)
        if data.quote_id:
            quote = await db.get(CrossBorderQuote, data.quote_id)
            if not quote or quote.merchant_id != merchant.id:
                raise CrossBorderError('Котировка не найдена')
            return await operations.accept_quote(db, actor=actor, quote_id=data.quote_id, external_id=data.external_id)
        external_id = data.external_id.strip()
        if not external_id or any(ord(c) < 32 or ord(c) == 127 for c in external_id):
            raise CrossBorderError('Некорректный номер заявки')
        await country_tariffs.scope_lock(db, f'crossborder-external:{merchant.id}:{external_id}')
        identity = {'amount_kzt': str(operations.local_amount(data.amount_kzt)),
                    'trader_id': str(data.trader_id) if data.trader_id else None,
                    'requisite_id': str(data.requisite_id) if data.requisite_id else None}
        existing = await db.scalar(select(CrossBorderOperation).where(
            CrossBorderOperation.merchant_id == merchant.id, CrossBorderOperation.external_id == external_id))
        if existing:
            if existing.snapshot.get('merchant_request') != identity:
                raise CrossBorderError('Номер заявки уже использован с другими параметрами')
            return existing
        candidates = await merchant_routing_catalog(db, merchant)
        last_error = None
        for candidate in candidates:
            trader_id, requisite_id = UUID(candidate['trader_id']), UUID(candidate['requisite_id'])
            if (data.requisite_id and data.requisite_id != requisite_id) or (data.trader_id and data.trader_id != trader_id):
                continue
            try:
                # Each failed candidate rolls back its preview and reserves nothing.
                async with db.begin_nested():
                    quote = await operations.create_quote(db, actor=actor, merchant_id=merchant.id, trader_id=trader_id,
                        requisite_id=requisite_id, amount_kzt=data.amount_kzt, merchant_request=identity)
                    operation = await operations.accept_quote(db, actor=actor, quote_id=quote.id, external_id=external_id)
                return operation
            except CrossBorderError as exc:
                last_error = exc
                if data.requisite_id:
                    raise
        raise last_error or CrossBorderError('Нет доступного грузинского банковского реквизита')
    return await commit_result(db, action(), operations.public_operation)


@router.get('/merchant/operations')
async def merchant_operations(db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2),
                              limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows = (await db.execute(select(CrossBorderOperation).where(CrossBorderOperation.merchant_id == merchant.id)
        .order_by(CrossBorderOperation.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return [operations.public_operation(row) for row in rows]


@router.get('/merchant/operations/{operation_id}')
async def merchant_operation(operation_id: UUID, db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2)):
    row = await db.get(CrossBorderOperation, operation_id)
    if not row or row.merchant_id != merchant.id:
        raise HTTPException(404, 'Операция не найдена')
    return operations.public_operation(row)


@router.get('/merchant/operations/{operation_id}/requisite')
async def merchant_requisite(operation_id: UUID, db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2)):
    await merchant_operation(operation_id, db, merchant)
    return await operation_requisite(operation_id, db, await db.get(User, merchant.owner_id))


@router.get('/merchant/requisites')
async def merchant_routing_catalog(db: AsyncSession = Depends(get_db), merchant=Depends(merchant_v2)):
    if merchant.working_country != 'KZ':
        raise HTTPException(409, 'Маршрут доступен казахстанскому кабинету')
    pairs = (await db.execute(select(Requisite, User).join(User, Requisite.trader_id == User.id).where(
        Requisite.country == 'GE', Requisite.currency == 'GEL', Requisite.method.in_(('c2c', 'bank_account')),
        Requisite.enabled.is_(True), Requisite.is_archived.is_(False), Requisite.status == 'active',
        Requisite.traffic_status.in_(('active', 'reinstated_full')), User.working_country == 'GE',
        User.trader_traffic_status.in_(('active', 'reinstated_full')), User.role == 'trader',
        User.is_active.is_(True), User.is_locked.is_(False), User.is_archived.is_(False)))).all()
    from app.core.payment_methods import payment_method_label
    return [{'requisite_id': str(r.id), 'trader_id': str(u.id), 'bank': r.bank_name, 'last4': r.last4,
             'method': r.method, 'method_label': payment_method_label(r.method, 'GE'), 'currency': 'GEL'} for r, u in pairs if not u.trader_assigned_merchants
            or str(merchant.id) in map(str, u.trader_assigned_merchants)]


@router.post('/webhook/{operation_id}')
async def bank_webhook(operation_id: UUID, request: Request, data: ConfirmIn, db: AsyncSession = Depends(get_db)):
    secret = settings.CROSSBORDER_WEBHOOK_SECRET
    if not secret or not settings.CROSSBORDER_WEBHOOK_ACTOR_ID:
        raise HTTPException(503, 'Банковская интеграция не настроена')
    raw_timestamp = request.headers.get('X-Timestamp', '')
    if not raw_timestamp.isdigit() or abs(int(time.time()) - int(raw_timestamp)) > 300:
        raise HTTPException(401, 'invalid timestamp')
    body = await request.body()
    canonical = raw_timestamp.encode() + b'\n' + str(operation_id).encode() + b'\n' + body
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, request.headers.get('X-Signature', '')):
        raise HTTPException(401, 'invalid signature')
    actor = await db.get(User, UUID(settings.CROSSBORDER_WEBHOOK_ACTOR_ID))
    if not operations.active(actor) or actor.role not in operations.STAFF or data.source != 'bank_webhook':
        raise HTTPException(403, 'invalid bank integration identity')
    return await commit_result(db, operations.confirm(db, actor=actor, operation_id=operation_id, **data.model_dump()), operations.public_operation)
