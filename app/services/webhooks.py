import asyncio
import ipaddress
import json
import logging
import socket
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.branding import get_branding
from app.core.metrics import metrics_registry
from app.core.security import decrypt_secret, sign_hmac
from app.core.validators import validate_public_webhook_url
from app.models import ApiKey, Merchant, WebhookDeliveryAttempt, WebhookEvent

logger = logging.getLogger('app.webhooks')


def _webhook_user_agent() -> str:
    brand_id = ''.join(ch for ch in get_branding().brand_id if ch.isalnum() or ch in {'-', '_'}).strip('-_')
    return f'{brand_id or "platform"}-Webhook/1.0'


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return any((
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_reserved,
        ip.is_multicast,
        ip.is_unspecified,
    ))


async def _resolve_webhook_ip(host: str, port: int) -> str:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    candidates: list[str] = []
    for info in infos:
        try:
            parsed = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if settings.is_production and _is_blocked_ip(parsed):
            raise ValueError('webhook_url must not resolve to private or reserved IP ranges')
        candidates.append(str(parsed))
    if not candidates:
        raise ValueError('webhook_url host could not be resolved')
    return candidates[0]


async def _post_validated_webhook(url: str, body: bytes, headers: dict[str, str], timeout_seconds: float = 10.0) -> int:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('webhook_url is invalid')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    host = parsed.hostname.strip().lower().rstrip('.')
    connect_host = host
    if settings.is_production:
        connect_host = await _resolve_webhook_ip(host, port)
    ssl_context = ssl.create_default_context() if parsed.scheme == 'https' else None
    server_hostname = host if parsed.scheme == 'https' else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(connect_host, port, ssl=ssl_context, server_hostname=server_hostname),
        timeout=timeout_seconds,
    )
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        default_port = (parsed.scheme == 'https' and port == 443) or (parsed.scheme == 'http' and port == 80)
        host_header = host if default_port else f'{host}:{port}'
        request_headers = {
            'Host': host_header,
            'User-Agent': _webhook_user_agent(),
            'Content-Length': str(len(body)),
            'Connection': 'close',
            **headers,
        }
        header_blob = ''.join(f'{name}: {value}\r\n' for name, value in request_headers.items())
        writer.write(f'POST {path} HTTP/1.1\r\n{header_blob}\r\n'.encode('ascii') + body)
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
        if not status_line:
            raise ValueError('webhook response is empty')
        parts = status_line.decode('iso-8859-1', errors='replace').split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError('webhook response status is invalid')
        return int(parts[1])
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def queue_webhook(db: AsyncSession, merchant_id, event_type: str, payload: dict):
    ev = WebhookEvent(merchant_id=merchant_id, event_type=event_type, payload=payload)
    db.add(ev); await db.flush()
    metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': event_type, 'status': 'queued'})
    logger.info('webhook_queued', extra={'event_id': str(ev.id), 'merchant_id': str(merchant_id), 'event_type': event_type})
    return ev


async def deliver_event(db: AsyncSession, event_id):
    res = await db.execute(
        select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update()
    )
    ev = res.scalar_one()
    now = datetime.now(timezone.utc)
    if ev.status in {'delivered', 'dead_letter', 'configuration_error', 'blocked', 'skipped'}:
        metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': ev.event_type, 'status': ev.status})
        return ev
    if ev.status == 'delivering' and (
        not ev.next_retry_at or ev.next_retry_at > now
    ):
        metrics_registry.increment(
            'processing_platform_webhook_events_total',
            {'event_type': ev.event_type, 'status': 'lease_active'},
        )
        return ev
    if ev.status == 'failed' and ev.next_retry_at and ev.next_retry_at > now:
        metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': ev.event_type, 'status': 'retry_wait'})
        return ev
    merchant = (await db.execute(select(Merchant).where(Merchant.id==ev.merchant_id))).scalar_one()
    if not merchant.webhook_url:
        ev.status='skipped'
        ev.next_retry_at = None
        metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': ev.event_type, 'status': ev.status})
        logger.info('webhook_skipped', extra={'event_id': str(ev.id), 'merchant_id': str(ev.merchant_id), 'event_type': ev.event_type})
        return ev
    if ev.attempts >= settings.PLATFORM_WEBHOOK_RETRY_LIMIT:
        ev.status = 'dead_letter'
        ev.last_error = 'retry limit exceeded'
        ev.next_retry_at = None
        metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': ev.event_type, 'status': ev.status})
        logger.warning('webhook_retry_limit_exceeded', extra={'event_id': str(ev.id), 'merchant_id': str(ev.merchant_id), 'event_type': ev.event_type})
        return ev
    api_key = (await db.execute(
        select(ApiKey).where(ApiKey.merchant_id == merchant.id, ApiKey.is_active == True).order_by(ApiKey.created_at.desc())
    )).scalars().first()
    secret = decrypt_secret(api_key.secret_hash) if api_key else None
    if not secret:
        ev.status = 'configuration_error'
        ev.last_error = 'missing_webhook_secret'
        ev.last_status_code = None
        ev.response_snippet = ''
        ev.next_retry_at = None
        ev.attempts += 1
        db.add(WebhookDeliveryAttempt(
            webhook_event_id=ev.id,
            attempt_no=ev.attempts,
            status='configuration_error',
            error='missing_webhook_secret',
        ))
        metrics_registry.increment('processing_platform_webhook_events_total', {'event_type': ev.event_type, 'status': ev.status})
        logger.warning('webhook_configuration_error', extra={'event_id': str(ev.id), 'merchant_id': str(ev.merchant_id), 'event_type': ev.event_type, 'reason': 'missing_webhook_secret'})
        return ev
    body = json.dumps({'event': ev.event_type, 'payload': ev.payload}, separators=(',', ':')).encode()
    ts = str(int(time.time()))
    signature = sign_hmac(secret, ts, body)
    delivery_id = str(uuid.uuid4())
    ev.status = 'delivering'
    ev.attempts += 1
    attempt = WebhookDeliveryAttempt(webhook_event_id=ev.id, attempt_no=ev.attempts, status='pending')
    db.add(attempt)
    headers = {
        'Content-Type':'application/json',
        'X-Timestamp':ts,
        'X-Signature':signature,
        'X-Event-ID':str(ev.id),
        'X-Delivery-ID': delivery_id,
        'X-Request-ID': delivery_id,
        'X-Idempotency-Key': f'webhook:{ev.id}',
    }
    if api_key:
        headers['X-API-Key'] = api_key.api_key
    claim_attempt = ev.attempts
    event_type = ev.event_type
    merchant_id = ev.merchant_id
    webhook_url = merchant.webhook_url
    ev.next_retry_at = now + timedelta(minutes=2)
    await db.flush()
    attempt_id = attempt.id
    # Commit the delivery claim before DNS or HTTP. A worker crash leaves a
    # time-bounded "delivering" lease that the periodic scanner can reclaim.
    await db.commit()

    status_code = None
    final_status = 'failed'
    final_error = None
    try:
        await asyncio.to_thread(validate_public_webhook_url, webhook_url)
    except ValueError as exc:
        final_status = 'blocked'
        final_error = str(exc)[:1000]
    except Exception as exc:
        final_error = str(exc)[:1000]
    else:
        try:
            status_code = await _post_validated_webhook(
                webhook_url,
                body,
                headers,
                timeout_seconds=10.0,
            )
            final_status = 'delivered' if 200 <= status_code < 300 else 'failed'
            if final_status == 'failed':
                final_error = f'HTTP {status_code}'
        except Exception as exc:
            final_error = str(exc)[:1000]

    ev = (await db.execute(
        select(WebhookEvent)
        .where(WebhookEvent.id == event_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    attempt = (await db.execute(
        select(WebhookDeliveryAttempt)
        .where(WebhookDeliveryAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    # A reclaimed lease owns a newer attempt and must not be overwritten.
    if ev.status != 'delivering' or ev.attempts != claim_attempt:
        return ev

    ev.status = final_status
    ev.last_status_code = status_code
    ev.response_snippet = ''
    ev.last_error = final_error
    ev.next_retry_at = (
        None
        if final_status in {'delivered', 'blocked'}
        else datetime.now(timezone.utc)
        + timedelta(minutes=min(60, 2 ** min(ev.attempts, 6)))
    )
    attempt.status = final_status
    attempt.status_code = status_code
    attempt.response_snippet = ''
    attempt.error = final_error
    metrics_registry.increment(
        'processing_platform_webhook_events_total',
        {'event_type': event_type, 'status': final_status},
    )
    if final_status == 'delivered':
        logger.info(
            'webhook_delivered',
            extra={
                'event_id': str(ev.id),
                'merchant_id': str(merchant_id),
                'event_type': event_type,
                'status_code': status_code,
                'status': final_status,
            },
        )
    elif final_status == 'blocked':
        logger.warning(
            'webhook_blocked',
            extra={
                'event_id': str(ev.id),
                'merchant_id': str(merchant_id),
                'event_type': event_type,
                'reason': final_error,
            },
        )
    else:
        logger.warning(
            'webhook_delivery_failed',
            extra={
                'event_id': str(ev.id),
                'merchant_id': str(merchant_id),
                'event_type': event_type,
                'reason': final_error,
            },
        )
    return ev
