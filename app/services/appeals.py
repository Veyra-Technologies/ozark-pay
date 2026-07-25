from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AppealStatus, DepositStatus, Role
from app.models import Appeal, AppealMessage, Deposit, Requisite, User
from app.services.antiscam import check_payment_result_for_risk
from app.services.fees import settle_deposit_credit
from app.services.ledger import LedgerError, trader_debit_available, trader_debit_frozen, trader_release_hold
from app.services.requisites import deposit_trader_hold_amount, deposit_trader_settlement_amount
from app.services.webhooks import queue_webhook
from app.services.webhook_payloads import build_deposit_webhook_payload


APPEAL_REVIEW_TTL = timedelta(minutes=90)
APPEAL_ACTIVE_STATUSES = {AppealStatus.opened.value, AppealStatus.in_review.value}
TRADER_REJECTION_REASONS = {
    'wrong_requisite': 'Неверные реквизиты',
    'wrong_amount': 'Неверная сумма',
    'no_payment': 'Отсутствие платежа',
}


def money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(value or '0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def metadata(appeal: Appeal) -> dict:
    return dict(appeal.metadata_json or {})


def set_metadata(appeal: Appeal, **values) -> dict:
    meta = metadata(appeal)
    meta.update(values)
    appeal.metadata_json = meta
    return meta


def default_deadline() -> datetime:
    return utcnow() + APPEAL_REVIEW_TTL


def appeal_deadline(appeal: Appeal) -> datetime:
    meta = metadata(appeal)
    raw = meta.get('deadline_at')
    if raw:
        try:
            return aware(datetime.fromisoformat(str(raw)))
        except ValueError:
            pass
    return aware(appeal.created_at) + APPEAL_REVIEW_TTL


def appeal_remaining_seconds(appeal: Appeal) -> int:
    if appeal.status not in APPEAL_ACTIVE_STATUSES:
        return 0
    if metadata(appeal).get('trader_decision') == 'rejected':
        return 0
    return max(0, int((appeal_deadline(appeal) - utcnow()).total_seconds()))


def deposit_payload(dep: Deposit, appeal: Appeal | None = None) -> dict:
    extra = {'appeal_id': str(appeal.id)} if appeal else None
    return build_deposit_webhook_payload(dep, extra=extra)


async def resolve_deposit_trader(db: AsyncSession, dep: Deposit, trader_id: str | UUID | None = None) -> tuple[Requisite | None, User | None]:
    req = None
    if dep.requisites_id:
        req = (await db.execute(select(Requisite).where(Requisite.id == dep.requisites_id).with_for_update())).scalar_one_or_none()
    selected_trader_id = trader_id or (req.trader_id if req else None)
    if not selected_trader_id:
        return req, None
    trader = (await db.execute(
        select(User)
        .where(User.id == UUID(str(selected_trader_id)), User.role.in_([Role.operator.value, Role.trader.value]))
        .with_for_update()
    )).scalar_one_or_none()
    return req, trader


async def settle_trader_for_approved_appeal(db: AsyncSession, dep: Deposit, trader_id: str | UUID | None = None) -> User | None:
    req, trader = await resolve_deposit_trader(db, dep, trader_id)
    if not trader:
        return None

    meta = dict(dep.metadata_json or {})
    if meta.get('trader_settled') is True or meta.get('trader_hold_status') == 'settled':
        return trader

    hold_amount = deposit_trader_hold_amount(dep, trader)
    settlement_amount = deposit_trader_settlement_amount(dep, trader)
    hold_status = meta.get('trader_hold_status')

    if hold_status == 'active':
        try:
            if settlement_amount > 0:
                await trader_debit_frozen(
                    db,
                    trader,
                    settlement_amount,
                    dep.id,
                    f'trader-appeal-settle:{dep.id}',
                    'appeal approved deposit settlement debit',
                )
            extra_hold = money(hold_amount - settlement_amount)
            if extra_hold > 0:
                await trader_release_hold(
                    db,
                    trader,
                    extra_hold,
                    dep.id,
                    f'trader-appeal-profit-release:{dep.id}',
                    'appeal approved trader commission release',
                )
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
    elif hold_status not in {'released', None, ''}:
        raise ValueError(f'trader hold status {hold_status} cannot be settled by appeal')
    elif settlement_amount > 0:
        try:
            await trader_debit_available(
                db,
                trader,
                settlement_amount,
                dep.id,
                f'trader-appeal-available-debit:{dep.id}',
                'appeal approved deposit settlement debit',
            )
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
    meta.update({
        'trader_hold_status': 'settled',
        'trader_settled': True,
        'trader_settled_at': utcnow().isoformat(),
        'trader_settlement_source': 'appeal',
        'trader_settlement_amount': str(settlement_amount),
    })
    dep.metadata_json = meta
    return trader


async def approve_deposit_appeal(db: AsyncSession, appeal: Appeal, *, actor_id=None, automatic: bool = False) -> object | None:
    if appeal.operation_type != 'deposit':
        raise ValueError('only deposit appeals can be approved from cabinet')
    dep = (await db.execute(select(Deposit).where(Deposit.id == appeal.operation_id).with_for_update())).scalar_one_or_none()
    if not dep:
        raise ValueError('deposit not found')

    meta = metadata(appeal)
    if meta.get('finance_applied') is True:
        raise ValueError('appeal finance has already been applied')
    trader_id = meta.get('trader_id')
    await settle_trader_for_approved_appeal(db, dep, trader_id)
    settlement = await settle_deposit_credit(
        db,
        merchant_id=dep.merchant_id,
        method=dep.method,
        amount=dep.amount,
        operation_id=dep.id,
        description='appeal approved deposit confirmation',
    )
    dep.status = DepositStatus.paid.value
    await check_payment_result_for_risk(db, dep)
    appeal.status = AppealStatus.approved.value
    appeal.decision = 'approved_auto' if automatic else 'approved'
    set_metadata(
        appeal,
        finance_applied=True,
        approved_at=utcnow().isoformat(),
        approved_by=str(actor_id) if actor_id else None,
        automatic_approval=automatic,
        settlement=settlement,
    )
    db.add(AppealMessage(
        appeal_id=appeal.id,
        author_id=actor_id,
        message='Апелляция автоматически одобрена по истечению таймера.' if automatic else 'Апелляция одобрена.',
    ))
    return await queue_webhook(db, dep.merchant_id, 'deposit.paid', deposit_payload(dep, appeal))


async def reject_deposit_appeal(db: AsyncSession, appeal: Appeal, *, actor_id, decision: str = 'rejected') -> object | None:
    if appeal.operation_type != 'deposit':
        raise ValueError('only deposit appeals can be rejected from cabinet')
    dep = (await db.execute(select(Deposit).where(Deposit.id == appeal.operation_id).with_for_update())).scalar_one_or_none()
    if not dep:
        raise ValueError('deposit not found')
    meta = metadata(appeal)
    previous_status = meta.get('previous_deposit_status') or DepositStatus.failed.value
    if dep.status == DepositStatus.appeal_opened.value:
        dep.status = previous_status if previous_status != DepositStatus.appeal_opened.value else DepositStatus.failed.value
        await check_payment_result_for_risk(db, dep)
    appeal.status = AppealStatus.rejected.value
    appeal.decision = decision[:64]
    set_metadata(appeal, rejected_at=utcnow().isoformat(), rejected_by=str(actor_id), finance_applied=False)
    db.add(AppealMessage(appeal_id=appeal.id, author_id=actor_id, message='Апелляция отклонена.'))
    return await queue_webhook(db, dep.merchant_id, 'deposit.failed', deposit_payload(dep, appeal))


async def approve_expired_appeals(db: AsyncSession) -> list:
    rows = (await db.execute(
        select(Appeal)
        .where(Appeal.status == AppealStatus.opened.value)
        .order_by(Appeal.created_at.asc())
        .limit(200)
        .with_for_update()
    )).scalars().all()
    events = []
    now = utcnow()
    for appeal in rows:
        meta = metadata(appeal)
        if meta.get('trader_decision'):
            continue
        if appeal_deadline(appeal) > now:
            continue
        try:
            event = await approve_deposit_appeal(db, appeal, automatic=True)
        except ValueError as exc:
            set_metadata(appeal, auto_approve_error=str(exc), auto_approve_failed_at=now.isoformat())
            continue
        if event:
            events.append(event)
    return events
