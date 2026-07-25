import asyncio
from datetime import datetime, timedelta, timezone

from celery import Celery
from sqlalchemy import select

from app.core.config import settings
from app.core.enums import DepositStatus
from app.db.session import AsyncSessionLocal, engine
from app.models import AggregatorCallbackLog, Deposit, WebhookEvent
from app.services.appeals import approve_expired_appeals
from app.services.aggregators import deliver_callback_log, sync_payment_from_deposit
from app.services.antiscam import check_payment_result_for_risk, run_periodic_antiscam_scan
from app.services.requisites import release_trader_deposit_hold
from app.services.webhooks import deliver_event
from app.services.webhooks import queue_webhook
from app.services.webhook_payloads import build_deposit_webhook_payload

celery_app = Celery('processing_platform', broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)


DEPOSIT_PROCESSING_TTL = timedelta(minutes=15)


def _run_async_task(coro_factory):
    async def wrapper():
        try:
            return await coro_factory()
        finally:
            await engine.dispose()
    return asyncio.run(wrapper())


def _deposit_payload(dep: Deposit) -> dict:
    return build_deposit_webhook_payload(dep)


@celery_app.task(name='deliver_webhook')
def deliver_webhook_task(event_id: str):
    async def run():
        async with AsyncSessionLocal() as db:
            await deliver_event(db, event_id)
            await db.commit()
    return _run_async_task(run)


@celery_app.task(name='deliver_aggregator_callback')
def deliver_aggregator_callback_task(callback_log_id: str):
    async def run():
        async with AsyncSessionLocal() as db:
            await deliver_callback_log(db, callback_log_id)
            await db.commit()
    return _run_async_task(run)


@celery_app.task(name='deliver_due_webhooks')
def deliver_due_webhooks_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            events = (await db.execute(
                select(WebhookEvent)
                .where(
                    WebhookEvent.status.in_(['queued', 'failed']),
                    WebhookEvent.attempts < settings.PLATFORM_WEBHOOK_RETRY_LIMIT,
                    (WebhookEvent.next_retry_at.is_(None) | (WebhookEvent.next_retry_at <= now)),
                )
                .order_by(WebhookEvent.created_at.asc())
                .limit(100)
            )).scalars().all()
            for event in events:
                await deliver_event(db, event.id)
            await db.commit()
            return len(events)
    return _run_async_task(run)


@celery_app.task(name='deliver_due_aggregator_callbacks')
def deliver_due_aggregator_callbacks_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            logs = (await db.execute(
                select(AggregatorCallbackLog)
                .where(
                    AggregatorCallbackLog.status.in_(['pending', 'failed']),
                    AggregatorCallbackLog.attempt < 5,
                    (AggregatorCallbackLog.next_retry_at.is_(None) | (AggregatorCallbackLog.next_retry_at <= now)),
                )
                .order_by(AggregatorCallbackLog.created_at.asc())
                .limit(100)
            )).scalars().all()
            for log in logs:
                await deliver_callback_log(db, log.id)
            await db.commit()
            return len(logs)
    return _run_async_task(run)


@celery_app.task(name='expire_stale_deposits')
def expire_stale_deposits_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            cutoff = datetime.now(timezone.utc) - DEPOSIT_PROCESSING_TTL
            rows = (await db.execute(
                select(Deposit)
                .where(
                    Deposit.status.in_([DepositStatus.created.value, DepositStatus.pending.value]),
                    Deposit.created_at <= cutoff,
                )
                .with_for_update()
            )).scalars().all()
            event_ids = []
            for dep in rows:
                dep.status = DepositStatus.failed.value
                payload = _deposit_payload(dep)
                await release_trader_deposit_hold(db, dep)
                await check_payment_result_for_risk(db, dep)
                event = await queue_webhook(db, dep.merchant_id, 'deposit.failed', payload)
                event_ids.append(event.id)
                await sync_payment_from_deposit(db, dep)
            await db.commit()
            for event_id in event_ids:
                await deliver_event(db, event_id)
            if event_ids:
                await db.commit()
            return len(rows)
    return _run_async_task(run)


@celery_app.task(name='approve_expired_appeals')
def approve_expired_appeals_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            events = await approve_expired_appeals(db)
            await db.commit()
            for event in events:
                await deliver_event(db, event.id)
            if events:
                await db.commit()
            return len(events)
    return _run_async_task(run)


@celery_app.task(name='run_antiscam_scan')
def run_antiscam_scan_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            changed = await run_periodic_antiscam_scan(db)
            await db.commit()
            return changed
    return _run_async_task(run)


celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.timezone = 'UTC'
celery_app.conf.beat_schedule = {
    'expire-stale-deposits-every-minute': {
        'task': 'expire_stale_deposits',
        'schedule': 60.0,
    },
    'approve-expired-appeals-every-minute': {
        'task': 'approve_expired_appeals',
        'schedule': 60.0,
    },
    'deliver-due-webhooks-every-minute': {
        'task': 'deliver_due_webhooks',
        'schedule': 60.0,
    },
    'deliver-due-aggregator-callbacks-every-minute': {
        'task': 'deliver_due_aggregator_callbacks',
        'schedule': 60.0,
    },
    'run-antiscam-scan-every-minute': {
        'task': 'run_antiscam_scan',
        'schedule': 60.0,
    },
}
