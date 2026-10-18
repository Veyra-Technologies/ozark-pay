"""Staff integration profile and scoped diagnostics; no credential readback."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import decrypt_secret
import json
from app.models import (AggregatorAccount, AggregatorExecution, AggregatorIntegration,
    AggregatorProviderEvent, Appeal, AuditLog, Deposit, OperationFeeSnapshot)
from app.services import aggregator_execution as service

router = APIRouter()


async def staff(request, db):
    from app.web.routes import _fee_admin_user, PREVIEW_ROLE_SESSION_KEY
    actor = await _fee_admin_user(request, db)
    if not actor or not request.session.get('twofa_verified') or request.session.get(PREVIEW_ROLE_SESSION_KEY):
        raise HTTPException(403, 'Нужен действующий вход Admin/SuperAdmin с 2FA')
    return actor


async def subject(db, identity):
    account = await db.get(AggregatorAccount, identity)
    if not account or account.working_country != 'TJ' or account.is_archived:
        raise HTTPException(404, 'Кабинет Aggregator TJ не найден')
    return account


@router.get('/cabinet/aggregators/{aggregator_id}/integration')
async def profile(aggregator_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await staff(request, db)
    account = await subject(db, aggregator_id)
    context = await service.own_context(db, account)
    staff_operations = []
    records = (await db.execute(select(Deposit, AggregatorExecution, OperationFeeSnapshot).join(
        AggregatorExecution, AggregatorExecution.deposit_id == Deposit.id).join(
        OperationFeeSnapshot, OperationFeeSnapshot.deposit_id == Deposit.id).where(
        AggregatorExecution.aggregator_id == account.id).order_by(Deposit.created_at.desc()).limit(100))).all()
    event_rows = list(await db.scalars(select(AggregatorProviderEvent).where(
        AggregatorProviderEvent.execution_id.in_([row[1].id for row in records])).order_by(AggregatorProviderEvent.created_at))) if records else []
    appeal_rows = list(await db.scalars(select(Appeal).where(Appeal.operation_type == 'deposit',
        Appeal.operation_id.in_([row[0].id for row in records])))) if records else []
    for deposit, provider, fixed in records:
        events = [row for row in event_rows if row.execution_id == provider.id]
        appeals = [row for row in appeal_rows if row.operation_id == deposit.id]
        details = json.loads(decrypt_secret(provider.requisites_encrypted)) if provider.requisites_encrypted else {}
        value = details.get('card_number') or details.get('bank_account') or ''
        staff_operations.append({'deposit':deposit, 'execution':provider, 'snapshot':fixed,
            'events':events, 'appeals':appeals, 'bank_name':details.get('bank_name'),
            'masked_requisite':'•••• '+value[-4:] if value else 'Ожидается ответ'})
    changes = list(await db.scalars(select(AuditLog).where(AuditLog.target_id == str(account.id))
        .order_by(AuditLog.created_at.desc()).limit(30)))
    from app.web.routes import templates
    return templates.TemplateResponse(request=request, name='external_aggregator.html', context={
        'request':request,'actor':actor,'external':context,'changes':changes,
        'message':request.query_params.get('message',''), 'credential_fields':service.CREDENTIAL_FIELDS,
        'staff_operations':staff_operations})


@router.post('/cabinet/aggregators/{aggregator_id}/integration')
async def save_profile(aggregator_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await staff(request, db)
    account = await subject(db, aggregator_id)
    form = await request.form()
    try:
        service.require_fresh_otp(actor, form.get('otp'))
        action = form.get('action')
        integration = await db.scalar(select(AggregatorIntegration).where(
            AggregatorIntegration.aggregator_id == account.id).with_for_update())
        if action == 'save':
            values = dict(form)
            values['merchant_ids'] = str(form.get('merchant_ids') or '').replace(',', ' ').split()
            values['allowed_ips'] = str(form.get('allowed_ips') or '').replace(',', ' ').split()
            await service.save_integration(db, actor=actor, account=account, form=values, otp=form.get('otp'))
            message = 'Сохранена новая версия. Маршрутизация выключена до проверки.'
        elif action == 'tariff':
            tiers = [{'min':low,'max':high or None,'percent':rate} for low,high,rate in zip(
                form.getlist('tier_min'),form.getlist('tier_max'),form.getlist('tier_percent'),strict=True) if low or high or rate]
            await service.save_executor_tariff(db,actor=actor,account=account,tiers=tiers,
                effective_from=datetime.fromisoformat(str(form.get('effective_from') or '').replace('Z','+00:00')),otp=form.get('otp'))
            message = 'Индивидуальный тариф исполнителя сохранён.'
        elif not integration:
            raise ValueError('Сначала сохраните API-интеграцию')
        elif action == 'verify':
            verified = await service.verify_integration(db,integration=integration)
            message = 'Проверено в изолированной QA.' if verified else 'Проверка не поддерживается: документированный адаптер отсутствует или не подтвердил подключение.'
        elif action == 'pause':
            integration.status = 'suspended'
            integration.routing_enabled = False
            message = 'Подключение приостановлено. Неопределённые обязательства сохранены для ручной сверки.'
        elif action == 'enable_qa':
            version = await service.integration_version(db,integration)
            await service.adapter_for(db,version)
            if integration.status != 'connected':
                raise ValueError('Требуется проверенное QA-подключение')
            plan = await service.local.personal_plan(db, party='aggregator', entity_id=account.id)
            collateral = await service.wallet(db, account.id)
            if not plan or collateral.balance <= collateral.hold or not version.merchant_ids or account.status != 'active':
                raise ValueError('Нужны действующий тариф, обеспечение и разрешённые мерчанты')
            integration.routing_enabled = True
            message = 'Разрешён только изолированный QA-маршрут. Реальные платежи закрыты.'
        else:
            raise ValueError('Неизвестное действие')
        from app.services.audit import audit
        await audit(db,'aggregator_integration_staff_action','aggregator',actor.id,account.id,
                    details={'action':str(action)[:32]})
        await db.commit()
    except (ValueError, KeyError, OSError, TimeoutError):
        await db.rollback()
        # Errors cannot reflect secret values submitted in this form.
        raise HTTPException(422,'Изменение отклонено. Проверьте 2FA, страну, диапазоны, адрес и незавершённые обязательства.') from None
    from urllib.parse import urlencode
    return RedirectResponse(f'/staff/cabinet/aggregators/{account.id}/integration?'+urlencode({'message':message}),status_code=303)
