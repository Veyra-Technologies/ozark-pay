from sqlalchemy import select, or_, func
from app.models import User, Merchant, Deposit, Requisite, Appeal, AuditLog
from app.web.view_models import build_audit_view

TABS = {
    'merchants': {'main', 'finance', 'tariffs', 'rolling', 'integration', 'history'},
    'traders': {'main', 'requisites', 'operations', 'finance', 'tariffs', 'activity'},
}


async def add_profile_context(db, request, context, kind, target):
    tab = request.query_params.get('tab', 'main')
    tab = tab if tab in TABS[kind] else 'main'
    try:
        page = max(1, min(int(request.query_params.get('page', 1)), 100000))
    except ValueError:
        page = 1
    offset = (page - 1) * 20
    context.update(profile_kind=kind, profile_user=target, profile_tab=tab,
                   profile_page=page, profile_has_next=False, profile_activity=[])
    if kind == 'traders':
        context['profile_working_account'] = (await db.get(User, target.trader_account_id)
            if target.role == 'operator' and target.trader_account_id and target.trader_account_id != target.id else target)
        if context['profile_working_account'].working_country == 'GE' and tab == 'tariffs':
            from app.services.country_tariffs import display_plan, resolve_plan
            from app.services.crossborder_money import CrossBorderError
            try:
                context['profile_ge_tariff'] = await display_plan(db, await resolve_plan(db, 'trader', context['profile_working_account'].id))
            except CrossBorderError as exc:
                context['profile_ge_tariff_error'] = str(exc)
        req_ids = select(Requisite.id).where(Requisite.trader_id == target.id)
        statement = select(Deposit).where(Deposit.requisites_id.in_(req_ids))
        page_rows = (await db.execute(statement.order_by(Deposit.created_at.desc(), Deposit.id).offset(offset).limit(21))).scalars().all()
        detail = context['trader_details'].setdefault(str(target.id), {})
        detail['deposits'] = page_rows[:20]
        dep_ids = statement.with_only_columns(Deposit.id)
        appeal_rows = (await db.execute(select(Appeal).where(Appeal.operation_id.in_(dep_ids)).order_by(Appeal.created_at.desc(), Appeal.id).offset(offset).limit(21))).scalars().all()
        detail['appeals'] = appeal_rows[:20]
        context['profile_operators'] = (await db.execute(select(User).where(User.trader_account_id == target.id, User.id != target.id, User.is_archived.is_(False)).order_by(User.email))).scalars().all()
        total, active = (await db.execute(select(func.count(), func.count().filter(Deposit.status.in_(['created','pending','appeal_opened']))).where(Deposit.requisites_id.in_(req_ids)))).one()
        context['trader_stats'].setdefault(str(target.id), {}).update(total_count=total, active_requests=active)
        context['profile_has_next'] = len(page_rows) > 20 or len(appeal_rows) > 20
        # The detail map for later pages must be built from those exact rows.
        from app.services.requisites import deposit_trader_profit_amount, deposit_trader_settlement_amount
        from app.core.requisite_providers import requisite_provider_display, requisite_provider_label
        requisites = {r.id: r for r in detail.get('requisites', [])}
        for deposit in page_rows[:20]:
            req = requisites.get(deposit.requisites_id)
            context['deposit_details'].setdefault(str(deposit.id), {}).update(
                trader_profit=deposit_trader_profit_amount(deposit, target),
                trader_settlement=deposit_trader_settlement_amount(deposit, target),
                provider_label=requisite_provider_label(req.method) if req else 'Банк',
                provider_name=requisite_provider_display(req.method, req.bank_code, req.operator_code, req.bank_name) if req else '')
        targets = [str(target.id)]
    else:
        merchant_id = context['merchant_credentials'][str(target.id)]['merchant_id']
        from uuid import UUID
        from app.models import TariffPlan
        from app.services.merchant_cabinets import catalog
        from app.services.country_tariffs import resolve_plan, display_plan
        from app.services.crossborder_money import CrossBorderError
        merchant = await db.get(Merchant, UUID(merchant_id))
        context['profile_merchant'] = merchant
        context['merchant_catalog'] = await catalog(db, merchant.id)
        context['profile_merchant_tariff'] = None
        if merchant.tariff_plan_id:
            context['profile_merchant_tariff'] = await display_plan(db, await db.get(TariffPlan, merchant.tariff_plan_id))
        elif merchant.working_country == 'KZ':
            try:
                context['profile_merchant_tariff'] = await display_plan(db, await resolve_plan(db, 'merchant', merchant.id))
            except CrossBorderError:
                pass
        targets = [str(target.id), merchant_id]
    if tab in ('activity', 'history'):
        rows = (await db.execute(select(AuditLog).where(or_(AuditLog.actor_id == target.id, AuditLog.target_id.in_(targets))).order_by(AuditLog.created_at.desc(), AuditLog.id).offset(offset).limit(21))).scalars().all() if context['user'].role == 'superadmin' else []
        actor_ids = {row.actor_id for row in rows if row.actor_id}
        labels = dict((await db.execute(select(User.id, User.email).where(User.id.in_(actor_ids)))).all()) if actor_ids else {}
        context['profile_activity'] = [build_audit_view(row, labels.get(row.actor_id, 'Система')) for row in rows[:20]]
        context['profile_has_next'] = len(rows) > 20
