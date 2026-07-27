from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DepositStatus
from app.core.metrics import metrics_registry
from app.models import Deposit
from app.services.aggregators import sync_payment_from_deposit
from app.services.antiscam import check_payment_result_for_risk
from app.services.audit import audit
from app.services.requisites import release_trader_deposit_hold
from app.services.rolling import release_pending_allocation
from app.services.deposit_ttl import (
    aware,
    deposit_deadline,
    deposit_is_expired,
    deposit_remaining_seconds,
    new_deposit_expires_at,
    utcnow,
)
from app.services.webhook_payloads import build_deposit_webhook_payload
from app.services.webhooks import queue_webhook


logger = logging.getLogger('app.finance')


ACTIVE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
    DepositStatus.appeal_opened.value,
}
TTL_EXPIRABLE_DEPOSIT_STATUSES = {
    DepositStatus.created.value,
    DepositStatus.pending.value,
}
UNSUCCESSFUL_DEPOSIT_STATUSES = {
    DepositStatus.failed.value,
    DepositStatus.expired.value,
    DepositStatus.cancelled.value,
}


@dataclass(frozen=True)
class DepositFinalizationResult:
    deposit_id: UUID
    changed: bool
    status: str
    reason: str
    webhook_event_id: UUID | None = None
    aggregator_callback_log_ids: tuple[UUID, ...] = ()


async def finalize_unsuccessful_deposit(
    db: AsyncSession,
    deposit: Deposit,
    *,
    reason: str,
    target_status: str = DepositStatus.failed.value,
    actor_id: UUID | None = None,
    actor_role: str = 'system',
    actor_ip: str | None = None,
    request_id: str | None = None,
    audit_action: str = 'deposit_finalized_unsuccessfully',
) -> DepositFinalizationResult:
    """Finalize a locked deposit once and persist all release/outbox rows."""
    if target_status not in UNSUCCESSFUL_DEPOSIT_STATUSES:
        raise ValueError('target status must be an unsuccessful final deposit status')
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('deposit finalization reason is required')
    old_status = deposit.status
    if old_status not in ACTIVE_DEPOSIT_STATUSES:
        return DepositFinalizationResult(
            deposit_id=deposit.id,
            changed=False,
            status=old_status,
            reason=reason,
        )

    deposit.status = target_status
    finalized_at = utcnow()
    metadata = dict(deposit.metadata_json or {})
    metadata.update({
        'failure_reason': reason,
        'finalized_at': finalized_at.isoformat(),
        'finalized_by_role': actor_role,
    })
    if request_id:
        metadata['finalization_request_id'] = request_id
    deposit.metadata_json = metadata

    # Global lock order: Deposit -> Requisite/Trader -> Rolling account/allocation.
    await release_trader_deposit_hold(db, deposit)
    await release_pending_allocation(db, deposit, reason=reason)
    await check_payment_result_for_risk(db, deposit)
    # PostgreSQL server_onupdate expires updated_at after autoflush. Refresh it
    # explicitly so the synchronous webhook snapshot builder never triggers
    # implicit async I/O (MissingGreenlet).
    await db.flush()
    await db.refresh(deposit, attribute_names=['updated_at'])
    event_type = (
        'deposit.cancelled'
        if target_status == DepositStatus.cancelled.value
        else 'deposit.failed'
    )
    event = await queue_webhook(
        db,
        deposit.merchant_id,
        event_type,
        build_deposit_webhook_payload(
            deposit,
            event_type,
            {'failure_reason': reason},
        ),
    )
    aggregator_logs = await sync_payment_from_deposit(db, deposit)
    await audit(
        db,
        audit_action,
        'deposit',
        actor_id,
        deposit.id,
        actor_ip,
        {
            'actor_role': actor_role,
            'deposit_id': str(deposit.id),
            'reason': reason,
            'old_status': old_status,
            'new_status': target_status,
            'timestamp': finalized_at.isoformat(),
            'request_id': request_id,
        },
    )
    return DepositFinalizationResult(
        deposit_id=deposit.id,
        changed=True,
        status=deposit.status,
        reason=reason,
        webhook_event_id=event.id,
        aggregator_callback_log_ids=tuple(row.id for row in aggregator_logs),
    )


async def expire_due_deposits(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> list[DepositFinalizationResult]:
    current = aware(now) if now else utcnow()
    rows = (await db.execute(
        select(Deposit)
        .where(
            Deposit.status.in_(TTL_EXPIRABLE_DEPOSIT_STATUSES),
            Deposit.expires_at <= current,
        )
        .order_by(Deposit.expires_at.asc(), Deposit.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    results = []
    for deposit in rows:
        deposit_id = deposit.id
        try:
            async with db.begin_nested():
                result = await finalize_unsuccessful_deposit(
                    db,
                    deposit,
                    reason='trader_timeout',
                    target_status=DepositStatus.failed.value,
                    actor_role='system',
                    audit_action='deposit_expired_by_ttl',
                )
        except ValueError as exc:
            metrics_registry.increment(
                'processing_platform_deposit_expiry_errors_total',
                {'reason': type(exc).__name__},
            )
            logger.error(
                'deposit_expiry_financial_invariant_failed',
                extra={
                    'operation_id': str(deposit_id),
                    'reason': str(exc)[:500],
                },
                exc_info=True,
            )
            continue
        if result.changed:
            results.append(result)
    return results
