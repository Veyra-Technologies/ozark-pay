"""Existing realm sessions and CSRF middleware protect every cabinet mutation."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select, or_, cast, String, func
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import crossborder_live as api
from app.core.security import decrypt_secret
from app.db.session import get_db
from app.models import AuditLog, Balance, BusinessAccount, Merchant, Requisite, TariffPlan, User, CrossBorderOperation, CrossBorderEvent
from app.services import crossborder_operations as operations, rate_registry, parser_assignments
from app.services.country_tariffs import public_plan
from app.services.crossborder_money import CrossBorderError

router = APIRouter(prefix='/cabinet/crossborder/live')


async def live_context(db, actor, role, filters=None):
    if role not in ('admin', 'superadmin', 'merchant', 'trader'):
        return {}
    # Role preview must not inherit the underlying staff account's broad data scope.
    if role != actor.role:
        return {'preview': True, 'operations': [], 'merchants': [], 'requisites': [], 'businesses': [],
                'tariffs': [], 'parsers': [], 'balances': [], 'audit': []}
    staff = role in operations.STAFF
    merchant_query = select(Merchant).where(Merchant.is_archived.is_(False))
    if not staff:
        merchant_query = merchant_query.where(Merchant.owner_id == actor.id)
    merchants = (await db.execute(merchant_query.order_by(Merchant.name))).scalars().all()
    # Public routing catalog contains the masked card, bank and beneficiary only.
    rq = select(Requisite).join(User, User.id == Requisite.trader_id).where(
        Requisite.country == 'GE', Requisite.currency == 'GEL', Requisite.method.in_(('c2c', 'bank_account')),
        Requisite.enabled.is_(True), Requisite.is_archived.is_(False), Requisite.status == 'active',
        Requisite.traffic_status.in_(('active', 'reinstated_full')),
        User.working_country == 'GE', User.role == 'trader', User.is_active.is_(True),
        User.is_locked.is_(False), User.is_archived.is_(False),
        User.trader_traffic_status.in_(('active', 'reinstated_full')))
    if role == 'trader':
        rq = rq.where(Requisite.trader_id == actor.id)
    requisites = (await db.execute(rq)).scalars().all()
    if role == 'merchant':
        own_ids = {str(m.id) for m in merchants if m.working_country == 'KZ'}
        traders = {row.id: row for row in (await db.execute(select(User).where(
            User.id.in_([r.trader_id for r in requisites])))).scalars().all()} if requisites else {}
        requisites = [r for r in requisites if own_ids and (not traders[r.trader_id].trader_assigned_merchants
            or own_ids.intersection(map(str, traders[r.trader_id].trader_assigned_merchants)))]
    rows = await operations.visible_operations(db, actor)
    if role == 'trader' and filters is not None:
        query = operations.scoped_operations(select(CrossBorderOperation), actor)
        query = query.where(CrossBorderOperation.status == 'accepted' if filters.get('view', 'active') == 'active'
                            else CrossBorderOperation.status != 'accepted')
        if filters.get('query'):
            needle = '%' + filters['query'] + '%'
            query = query.where(or_(CrossBorderOperation.external_id.ilike(needle), cast(CrossBorderOperation.id, String).ilike(needle)))
        if filters.get('amount'):
            from decimal import Decimal
            query = query.where(CrossBorderOperation.amount_kzt == Decimal(str(filters['amount'])))
        if filters.get('date'):
            from sqlalchemy import Date
            from datetime import date
            query = query.where(cast(CrossBorderOperation.created_at, Date) == date.fromisoformat(str(filters['date'])))
        status = filters.get('status')
        if status:
            query = query.where(CrossBorderOperation.status == ('accepted' if status in ('created','pending') else status))
        for key in ('bank_code', 'requisite', 'payment_method'):
            if filters.get(key):
                query = query.join(Requisite, Requisite.id == CrossBorderOperation.requisite_id)
                if filters.get('bank_code'):
                    query = query.where(cast(Requisite.bank_id, String) == filters['bank_code'])
                if filters.get('requisite'):
                    query = query.where(or_(Requisite.owner_name.ilike('%'+filters['requisite']+'%'), Requisite.last4 == filters['requisite']))
                if filters.get('payment_method'):
                    query = query.where(CrossBorderOperation.direction == filters['payment_method'])
                break
        try:
            page = max(1, min(int(filters.get('operation_page', 1)), 100000))
        except (TypeError, ValueError):
            page = 1
        rows = (await db.execute(query.order_by(CrossBorderOperation.created_at.desc(), CrossBorderOperation.id)
            .offset((page-1)*200).limit(201))).scalars().all()
    balances = (await db.execute(select(Balance).where(Balance.merchant_id.in_([m.id for m in merchants]),
                                                      Balance.currency == 'USDT'))).scalars().all() if merchants else []
    context = {'preview': False, 'merchants': merchants, 'statistics': await operations.statistics(db, actor),
        'requisites': [{'id': str(r.id), 'trader_id': str(r.trader_id), 'label': f'{r.bank_name or "GE bank"} · {r.owner_name} · ****{r.last4 or ""}'} for r in requisites],
        'operations': [operations.public_operation(row) for row in rows], 'balances': balances,
        'businesses': [], 'tariffs': [], 'parsers': [], 'audit': []}
    context['operation_page'] = page if role == 'trader' and filters is not None else 1
    context['has_more_operations'] = len(rows) > 200
    context['operations'] = context['operations'][:200]
    context['active_count'] = sum(s['count'] for s in context['statistics']['statuses'] if s['status'] == 'accepted')
    context['history_count'] = sum(s['count'] for s in context['statistics']['statuses'] if s['status'] != 'accepted')
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    paid_query = operations.scoped_operations(select(func.count(), func.coalesce(func.sum(CrossBorderOperation.actual_gel), 0)), actor)
    paid_count, paid_gel = (await db.execute(paid_query.where(CrossBorderOperation.status == 'paid', CrossBorderOperation.confirmed_at >= today))).one()
    context['paid_today_count'], context['paid_today_gel'] = paid_count, paid_gel
    context['urgent_count'] = await db.scalar(operations.scoped_operations(select(func.count()).select_from(CrossBorderOperation), actor)
        .where(CrossBorderOperation.status == 'accepted', CrossBorderOperation.expires_at <= datetime.now(timezone.utc) + timedelta(minutes=5)))
    assigned = await parser_assignments.current(db)
    from app.services.binance_p2p import GEL_PARSER_ID
    active_traders = {r.trader_id for r in assigned if r.is_active and r.parser_id == GEL_PARSER_ID}
    if not staff:
        own_merchants = {m.id for m in merchants}
        assigned = [r for r in assigned if r.merchant_id in own_merchants or r.trader_id == actor.id]
    context['assignments'] = [parser_assignments.public_assignment(r) for r in assigned] if staff else []
    states = {p: await rate_registry.parser_state(db, p, refresh=False, persist=False) for p in {r.parser_id for r in assigned}} if staff else {}
    for item in context['assignments']:
        state = states[item['parser_id']]
        usable = item['is_active'] and state['available'] and state['configuration'].get('id') == item['configuration_id']
        item.update(rate=state['quote']['rate'] if usable else None,
            rate_unit=state['configuration']['parameters'].get('unit'),
            fetched_at=state['quote']['fetched_at'] if state['quote'] else None,
            rate_status=state['status'] if item['is_active'] else 'disabled')
    context['traders'] = (await db.execute(select(User).where(User.role == 'trader',
        User.working_country == 'GE', User.is_active.is_(True), User.is_locked.is_(False), User.is_archived.is_(False)).order_by(User.email))).scalars().all() if staff else []
    from decimal import Decimal, ROUND_HALF_UP
    gel = await rate_registry.parser_state(db, GEL_PARSER_ID, refresh=False, persist=False)
    op_ids = [UUID(op['id']) for op in context['operations']]
    req_ids = {UUID(op['requisite_id']) for op in context['operations']}
    req_map = {r.id: r for r in (await db.execute(select(Requisite).where(Requisite.id.in_(req_ids)))).scalars().all()} if req_ids else {}
    event_map = {}
    if op_ids:
        events = (await db.execute(select(CrossBorderEvent).where(CrossBorderEvent.operation_id.in_(op_ids))
            .order_by(CrossBorderEvent.created_at))).scalars().all()
        for event in events:
            event_map.setdefault(str(event.operation_id), []).append({'action': event.action, 'at': event.created_at.isoformat()})
    for op in context['operations']:
        requisite = req_map.get(UUID(op['requisite_id']))
        op['requisite_label'] = f'{requisite.bank_name or "GE bank"} · {requisite.owner_name} · ****{requisite.last4 or ""}' if requisite else 'Архивный реквизит'
        op['bank_label'] = requisite.bank_name if requisite else 'Архивный банк'
        from app.core.payment_methods import payment_method_label
        op['requisite_method_label'] = payment_method_label(requisite.method, 'GE') if requisite else 'Реквизит'
        op['beneficiary_label'] = f'{requisite.owner_name} · ****{requisite.last4 or ""}' if requisite else 'Архивный реквизит'
        op['remaining_seconds'] = max(0, int((datetime.fromisoformat(op['expires_at']) - datetime.now(timezone.utc)).total_seconds()))
        op['events'] = event_map.get(op['id'], [])
        available = UUID(op['trader_id']) in active_traders and gel['available']
        op['current_expected_gel'] = str((Decimal(op['base_usdt']) * Decimal(gel['quote']['rate']))
            .quantize(Decimal('.01'), rounding=ROUND_HALF_UP)) if available else None
        op['current_gel_at'] = gel['quote']['fetched_at'] if available else None
    if staff:
        from app.services.merchant_cabinets import catalog
        context['merchant_catalog'] = await catalog(db)
        context['businesses'] = (await db.execute(select(BusinessAccount).order_by(BusinessAccount.name))).scalars().all()
        context['tariffs'] = [public_plan(row) for row in (await db.execute(select(TariffPlan).where(TariffPlan.direction == 'kz-ge')
            .order_by(TariffPlan.created_at.desc()).limit(200))).scalars().all()]
        context['parsers'] = [await rate_registry.parser_state(db, parser_id, refresh=False, persist=False)
                             for parser_id in rate_registry.PARSER_POLICY if parser_id != 'xe']
        context['audit'] = (await db.execute(select(AuditLog).where(AuditLog.action.in_(
            ['parser_assignment_changed', 'merchant_country_assigned', 'merchant_parser_changed', 'parser_configuration_version_created', 'country_tariff_version_created']))
            .order_by(AuditLog.created_at.desc()).limit(100))).scalars().all()
    return context


async def web_actor(request, db, *, admin=False):
    from app.web.routes import get_current_web_user, staff_2fa_setup_required, PREVIEW_ROLE_SESSION_KEY, _session_identity_changed
    if await _session_identity_changed(request):
        raise HTTPException(409, 'Сессия изменилась; обновите страницу')
    actor = await get_current_web_user(request, db)
    allowed = operations.STAFF if admin else (*operations.STAFF, 'merchant', 'trader')
    if (not actor or actor.role not in allowed or staff_2fa_setup_required(actor)
            or request.session.get(PREVIEW_ROLE_SESSION_KEY)):
        raise HTTPException(403, 'Недостаточно прав')
    return actor


@router.get('/operations/{operation_id}/requisite')
async def payment_requisite(operation_id: UUID, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    response.headers['Cache-Control'] = 'no-store'
    actor = await web_actor(request, db)
    try:
        operation = await operations.operation_access(db, actor, operation_id)
    except CrossBorderError as exc:
        raise HTTPException(404, 'Операция не найдена') from exc
    requisite = await db.get(Requisite, operation.requisite_id)
    from app.core.ge_bank_requisites import operation_requisite_payload
    return operation_requisite_payload(requisite, decrypt_secret(requisite.value_encrypted))


@router.post('/{action}')
async def command(action: str, request: Request, db: AsyncSession = Depends(get_db)):
    admin_actions = {'tariff', 'ru-tariff', 'merchant-settings', 'proposal', 'parser', 'test-parser', 'parser-history', 'country', 'merchant-parser', 'assignment', 'assignment-history', 'refund'}
    actor = await web_actor(request, db, admin=action in admin_actions)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('Некорректная форма')
        if action in ('quote', 'accept'):
            raise HTTPException(403, 'Создание заявок доступно только через Merchant HMAC v2 API')
        if action == 'assignment':
            return await api.assign_cabinet_parser(api.AssignmentIn.model_validate(payload), db, actor)
        if action == 'merchant-settings':
            merchant_id = UUID(payload.pop('merchant_id'))
            return await api.cabinet_settings(merchant_id, api.CabinetSettingsIn.model_validate(payload), db, actor)
        if action == 'ru-tariff':
            return await api.ru_tariff(api.RuTariffIn.model_validate(payload), db, actor)
        if action == 'assignment-history':
            return await api.assignment_history(db, actor)
        if action in ('confirm', 'cancel', 'refund'):
            operation_id = UUID(payload.pop('operation_id'))
            model = api.ConfirmIn if action == 'confirm' else api.ReasonIn
            return await getattr(api, action)(operation_id, model.model_validate(payload), db, actor)
        if action == 'tariff':
            return await api.create_tariff(api.TariffIn.model_validate(payload), db, actor)
        if action == 'proposal':
            return await api.propose_tariff(api.ProposalIn.model_validate(payload), actor)
        if action == 'parser':
            parser_id = payload.pop('parser_id')
            return await api.configure_parser(parser_id, api.ParserIn.model_validate(payload), db, actor)
        if action == 'test-parser':
            return await api.test_parser(payload['parser_id'], db, actor)
        if action == 'parser-history':
            return await api.parser_history(payload['parser_id'], db, actor)
        if action in ('country', 'merchant-parser'):
            merchant_id = UUID(payload.pop('merchant_id'))
            if action == 'country':
                return await api.assign_country(merchant_id, api.CountryIn.model_validate(payload), db, actor)
            return await api.assign_parser(merchant_id, api.MerchantParserIn.model_validate(payload), db, actor)
        raise HTTPException(404, 'Неизвестное действие')
    except ValidationError as exc:
        await db.rollback()
        raise HTTPException(422, 'Проверьте обязательные поля, валюты, числа и время формы') from exc
    except (ValueError, KeyError, TypeError) as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
