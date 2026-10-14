import asyncio
import logging
from datetime import datetime, timezone

from celery import Celery
from sqlalchemy import and_, or_, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models import AggregatorCallbackLog
from app.services.appeals import approve_expired_appeals
from app.services.aggregators import deliver_callback_log
from app.services.antiscam import run_periodic_antiscam_scan
from app.services.deposit_lifecycle import expire_due_deposits
from app.services.webhooks import deliver_event, due_webhook_event_ids

celery_app = Celery('processing_platform', broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)
logger = logging.getLogger('app.workers')


def _run_async_task(coro_factory):
    async def wrapper():
        try:
            return await coro_factory()
        finally:
            await engine.dispose()
    return asyncio.run(wrapper())


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
            event_ids = await due_webhook_event_ids(db, limit=100)
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        return sum(
            1 for event_id in event_ids
            if enqueue_webhook_delivery(event_id)
        )
    return _run_async_task(run)


@celery_app.task(name='deliver_due_aggregator_callbacks')
def deliver_due_aggregator_callbacks_task() -> int:
    async def run() -> int:
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            callback_ids = (await db.execute(
                select(AggregatorCallbackLog.id)
                .where(
                    or_(
                        AggregatorCallbackLog.status.in_(['pending', 'failed']),
                        and_(
                            AggregatorCallbackLog.status == 'delivering',
                            AggregatorCallbackLog.next_retry_at <= now,
                        ),
                    ),
                    AggregatorCallbackLog.attempt < 5,
                    (AggregatorCallbackLog.next_retry_at.is_(None) | (AggregatorCallbackLog.next_retry_at <= now)),
                )
                .order_by(AggregatorCallbackLog.created_at.asc())
                .limit(100)
            )).scalars().all()
        from app.services.aggregator_enqueue import (
            enqueue_aggregator_callback_delivery,
        )

        return sum(
            1 for callback_id in callback_ids
            if enqueue_aggregator_callback_delivery(callback_id)
        )
    return _run_async_task(run)


@celery_app.task(name='expire_stale_deposits')
def expire_stale_deposits_task() -> int:
    async def run() -> int:
        from app.services.aggregator_enqueue import enqueue_aggregator_callback_delivery
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        async with AsyncSessionLocal() as db:
            results = await expire_due_deposits(db)
            await db.commit()
            for result in results:
                if result.webhook_event_id:
                    enqueue_webhook_delivery(result.webhook_event_id)
                for callback_id in result.aggregator_callback_log_ids:
                    enqueue_aggregator_callback_delivery(callback_id)
            return len(results)
    return _run_async_task(run)


@celery_app.task(name='approve_expired_appeals')
def approve_expired_appeals_task() -> int:
    async def run() -> int:
        from app.services.webhook_enqueue import enqueue_webhook_delivery

        async with AsyncSessionLocal() as db:
            events = await approve_expired_appeals(db)
            await db.commit()
            for event in events:
                enqueue_webhook_delivery(event.id)
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


@celery_app.task(name='reconcile_external_processor_payments')
def reconcile_external_processor_payments_task() -> int:
    async def run() -> int:
        from app.services.external_processors import due_reconciliation_payment_ids, reconcile_payment

        async with AsyncSessionLocal() as db:
            payment_ids = await due_reconciliation_payment_ids(db, limit=100)
        reconciled = 0
        for payment_id in payment_ids:
            async with AsyncSessionLocal() as db:
                try:
                    await reconcile_payment(db, payment_id, retry_create_if_missing=True)
                    reconciled += 1
                except Exception:
                    await db.rollback()
                    # The row remains due and will be retried by the next beat run.
                    logger.exception('external_processor_reconciliation_failed', extra={'payment_id': str(payment_id)})
        return reconciled
    return _run_async_task(run)


celery_app.conf.broker_connection_retry_on_startup = True
@celery_app.task(name='refresh_xe_rates')
def refresh_xe_rates_task():
    async def run():
        from app.services.xe import get_state
        state = await get_state()
        return {'available': state['available'], 'code': state['code']}
    return _run_async_task(run)


@celery_app.task(name='refresh_binance_p2p_rate')
def refresh_binance_p2p_rate_task():
    async def run():
        from app.services.binance_p2p import PARSER_ID, GEL_PARSER_ID
        from app.services.rate_registry import parser_state
        async with AsyncSessionLocal() as db:
            states = {}
            for parser_id in (PARSER_ID, GEL_PARSER_ID):
                states[parser_id] = await parser_state(db, parser_id, refresh=True)
                await db.commit()
            return {p: {'available': s['available'], 'status': s['status']} for p, s in states.items()}
    return _run_async_task(run)


@celery_app.task(name='refresh_rapira_rate')
def refresh_rapira_rate_task():
    async def run():
        from app.services.rate_registry import parser_state
        async with AsyncSessionLocal() as db:
            state = await parser_state(db, 'rapira', refresh=True)
            await db.commit()
            return {'available': state['available'], 'status': state['status']}
    return _run_async_task(run)

celery_app.conf.timezone = 'UTC'
celery_app.conf.beat_schedule = {
    'refresh-xe-rates': {'task': 'refresh_xe_rates', 'schedule': 60.0},
    'refresh-binance-p2p-rate': {
        'task': 'refresh_binance_p2p_rate',
        'schedule': 15.0,
    },
    'refresh-rapira-rate': {'task': 'refresh_rapira_rate', 'schedule': 15.0},
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
    'reconcile-external-processors-every-minute': {
        'task': 'reconcile_external_processor_payments',
        'schedule': 60.0,
    },
}
