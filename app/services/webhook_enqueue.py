import logging

from app.core.metrics import metrics_registry
from app.workers.celery_app import deliver_webhook_task

logger = logging.getLogger('app.webhooks')


def enqueue_webhook_delivery(event_id) -> bool:
    event_id_text = str(event_id)
    try:
        deliver_webhook_task.delay(event_id_text)
    except Exception as exc:
        metrics_registry.increment('processing_platform_webhook_enqueue_failures_total', {'reason': exc.__class__.__name__})
        logger.exception(
            'webhook_enqueue_failed',
            extra={
                'event_id': event_id_text,
                'reason': str(exc)[:500],
            },
        )
        return False
    metrics_registry.increment('processing_platform_webhook_enqueue_total', {'status': 'accepted'})
    logger.info('webhook_delivery_enqueued', extra={'event_id': event_id_text})
    return True