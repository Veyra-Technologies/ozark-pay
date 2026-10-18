from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import DepositStatus
from app.core.security import decrypt_secret, encrypt_secret
from app.models import (
    Deposit,
    ExternalProcessorConnection,
    ExternalProcessorPayment,
    ExternalProcessorVersion,
    ExternalProcessorWebhookEvent,
    FeeRule,
)
from app.services.audit import audit
from app.services.deposit_confirmation import (
    DepositConfirmationConflict,
    confirm_deposit_payment,
    enqueue_deposit_confirmation_deliveries,
)
from app.services.deposit_lifecycle import finalize_unsuccessful_deposit, new_deposit_expires_at
from app.services.fee_tiers import (
    create_fee_rule,
    create_operation_fee_snapshot,
    fee_amount,
    money,
    rate,
    resolve_fee_rule,
    utcnow,
    validate_margin,
)
from app.services.rolling import create_pending_allocation
from app.services.webhook_enqueue import enqueue_webhook_delivery


logger = logging.getLogger('app.external_processors')
PROCESSOR_METHODS = {'sbp', 'c2c', 'mobile_commerce', 'sbp_qr'}
PROCESSOR_CURRENCIES = {'RUB'}
COUNTED_PAYMENT_STATUSES = {'prepared', 'sending', 'unknown', 'created', 'paid'}
TERMINAL_EXTERNAL_STATUSES = {'paid', 'failed', 'expired', 'cancelled'}
FINAL_REQUEST_STATUSES = TERMINAL_EXTERNAL_STATUSES | {'refused'}


class ExternalProcessorError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409, unknown: bool = False):
        self.code = code
        self.status_code = status_code
        self.unknown = unknown
        super().__init__(message)


@dataclass(frozen=True)
class ProcessorHttpResponse:
    status_code: int
    payload: dict


def _decimal(value, default='0') -> Decimal:
    return Decimal(str(value if value not in (None, '') else default))


def _clean_methods(values) -> list[str]:
    result = []
    for raw in values or []:
        value = str(raw).strip().lower()
        if value == 'mobile':
            value = 'mobile_commerce'
        if value in PROCESSOR_METHODS and value not in result:
            result.append(value)
    if not result:
        raise ExternalProcessorError('methods_required', 'Выберите хотя бы один совместимый метод')
    return result


def _clean_currencies(values) -> list[str]:
    result = []
    for raw in values or []:
        value = str(raw).strip().upper()
        if value in PROCESSOR_CURRENCIES and value not in result:
            result.append(value)
    if not result:
        raise ExternalProcessorError('currencies_required', 'Внешняя площадка принимает платежные заявки только в RUB')
    return result


def _blocked_ip(address: str) -> bool:
    parsed = ipaddress.ip_address(address)
    return any((parsed.is_private, parsed.is_loopback, parsed.is_link_local, parsed.is_reserved, parsed.is_multicast, parsed.is_unspecified))


def normalize_processor_base_url(value: str, environment: str) -> str:
    environment = (environment or '').strip().lower()
    if environment not in {'sandbox', 'production'}:
        raise ExternalProcessorError('environment_invalid', 'Среда должна быть Sandbox или Production')
    raw = (value or '').strip().rstrip('/')
    parsed = urlsplit(raw)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ExternalProcessorError('base_url_invalid', 'Укажите полный HTTP(S) адрес Merchant API')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExternalProcessorError('base_url_invalid', 'Адрес API не должен содержать учётные данные, query или fragment')
    host = parsed.hostname.lower().rstrip('.')
    path = (parsed.path or '').rstrip('/')
    if not path.endswith('/api/v1/merchant'):
        raise ExternalProcessorError('base_url_path_invalid', 'Адрес должен оканчиваться на /api/v1/merchant')
    if environment == 'production':
        if parsed.scheme != 'https':
            raise ExternalProcessorError('production_https_required', 'Production подключается только по HTTPS')
        try:
            if _blocked_ip(host):
                raise ExternalProcessorError('production_host_private', 'Production адрес не может быть локальным или приватным')
        except ValueError:
            try:
                resolved = {
                    info[4][0]
                    for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
                }
            except socket.gaierror as exc:
                raise ExternalProcessorError('production_host_unresolved', 'Production host не разрешается через DNS') from exc
            if not resolved or any(_blocked_ip(address) for address in resolved):
                raise ExternalProcessorError('production_host_private', 'Production адрес не может вести в локальную или приватную сеть')
    else:
        try:
            private = _blocked_ip(host)
        except ValueError:
            private = host in {'localhost'} or host.endswith('.local') or host.endswith('.internal')
        if private and host not in settings.external_processor_sandbox_hosts:
            raise ExternalProcessorError('sandbox_host_not_allowed', 'Локальный Sandbox host не входит в разрешённый список')
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def _connection_values(*, name, adapter_type, environment, api_base_url, api_key, api_secret,
                       webhook_key_id, webhook_secret, supported_methods, supported_currencies,
                       min_amount, max_amount, daily_limit, monthly_limit, executor_fee_percent,
                       priority, routing_enabled) -> dict:
    if (adapter_type or '').strip().lower() != 'merchant_hmac_v2':
        raise ExternalProcessorError('adapter_unsupported', 'Поддерживается только адаптер внешней площадки')
    clean_name = (name or '').strip()
    if not clean_name or len(clean_name) > 160:
        raise ExternalProcessorError('name_invalid', 'Название обязательно и должно быть короче 160 символов')
    environment = (environment or '').strip().lower()
    minimum = money(min_amount)
    maximum = money(max_amount)
    if maximum <= minimum:
        raise ExternalProcessorError('amount_range_invalid', 'Максимальная сумма должна быть больше минимальной')
    daily = money(daily_limit) if daily_limit not in (None, '') else None
    monthly = money(monthly_limit) if monthly_limit not in (None, '') else None
    fee = rate(executor_fee_percent)
    if not (api_key or '').strip() or not (api_secret or '').strip():
        raise ExternalProcessorError('api_credentials_required', 'Нужны API key и API secret внешней площадки')
    if not (webhook_key_id or '').strip() or not (webhook_secret or '').strip():
        raise ExternalProcessorError('webhook_credentials_required', 'Нужны Key-ID и secret отдельного webhook-ключа внешней площадки')
    return {
        'name': clean_name,
        'adapter_type': 'merchant_hmac_v2',
        'environment': environment,
        'api_base_url': normalize_processor_base_url(api_base_url, environment),
        'api_key': (api_key or '').strip(),
        'api_secret': (api_secret or '').strip(),
        'webhook_key_id': (webhook_key_id or '').strip(),
        'webhook_secret': (webhook_secret or '').strip(),
        'supported_methods': _clean_methods(supported_methods),
        'supported_currencies': _clean_currencies(supported_currencies),
        'min_amount': minimum,
        'max_amount': maximum,
        'daily_limit': daily,
        'monthly_limit': monthly,
        'executor_fee_percent': fee,
        'priority': max(0, int(priority)),
        'routing_enabled': bool(routing_enabled),
    }


async def _replace_processor_fee_rules(db: AsyncSession, connection: ExternalProcessorConnection, actor_id: uuid.UUID) -> None:
    now = utcnow()
    existing = (await db.execute(select(FeeRule).where(
        FeeRule.entity_type == 'external_processor',
        FeeRule.entity_id == connection.id,
        FeeRule.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))).scalars().all()
    next_versions: dict[str, int] = {}
    for rule in existing:
        next_versions[rule.payment_method] = max(next_versions.get(rule.payment_method, 1), int(rule.version or 1) + 1)
        rule.is_active = False
        rule.effective_to = min(rule.effective_to, now) if rule.effective_to else now
        rule.updated_by = actor_id
    await db.flush()
    for method in connection.supported_methods:
        await create_fee_rule(
            db,
            actor_id=actor_id,
            version=next_versions.get(method, 1),
            entity_type='external_processor',
            entity_id=connection.id,
            fee_side='executor_fee',
            payment_method=method,
            currency='RUB',
            min_amount=connection.min_amount,
            max_amount=money(connection.max_amount) + Decimal('0.01'),
            rate_percent=connection.executor_fee_percent,
            effective_from=now,
            effective_to=None,
        )


async def _snapshot_connection(db: AsyncSession, connection: ExternalProcessorConnection, actor_id: uuid.UUID) -> ExternalProcessorVersion:
    version = ExternalProcessorVersion(
        connection_id=connection.id,
        version=connection.config_version,
        adapter_type=connection.adapter_type,
        environment=connection.environment,
        api_base_url=connection.api_base_url,
        webhook_header_prefix=connection.webhook_header_prefix,
        api_key_encrypted=connection.api_key_encrypted,
        api_secret_encrypted=connection.api_secret_encrypted,
        webhook_key_id=connection.webhook_key_id,
        webhook_secret_encrypted=connection.webhook_secret_encrypted,
        supported_methods=list(connection.supported_methods or []),
        supported_currencies=list(connection.supported_currencies or []),
        min_amount=connection.min_amount,
        max_amount=connection.max_amount,
        daily_limit=connection.daily_limit,
        monthly_limit=connection.monthly_limit,
        executor_fee_percent=connection.executor_fee_percent,
        priority=connection.priority,
        routing_enabled=connection.routing_enabled,
        created_by=actor_id,
    )
    db.add(version)
    await db.flush()
    return version


async def create_connection(db: AsyncSession, *, actor_id: uuid.UUID, **raw) -> ExternalProcessorConnection:
    values = _connection_values(**raw)
    if values['routing_enabled']:
        raise ExternalProcessorError('connection_check_required', 'Сначала сохраните подключение и выполните проверку')
    connection = ExternalProcessorConnection(
        name=values['name'], adapter_type=values['adapter_type'], environment=values['environment'],
        api_base_url=values['api_base_url'], api_key_encrypted=encrypt_secret(values['api_key']),
        api_secret_encrypted=encrypt_secret(values['api_secret']), webhook_key_id=values['webhook_key_id'],
        webhook_secret_encrypted=encrypt_secret(values['webhook_secret']), supported_methods=values['supported_methods'],
        supported_currencies=values['supported_currencies'], min_amount=values['min_amount'], max_amount=values['max_amount'],
        daily_limit=values['daily_limit'], monthly_limit=values['monthly_limit'],
        executor_fee_percent=values['executor_fee_percent'], priority=values['priority'], routing_enabled=False,
        config_version=1, created_by=actor_id, updated_by=actor_id,
    )
    db.add(connection)
    await db.flush()
    await _replace_processor_fee_rules(db, connection, actor_id)
    await _snapshot_connection(db, connection, actor_id)
    return connection


async def update_connection(db: AsyncSession, connection: ExternalProcessorConnection, *, actor_id: uuid.UUID, **raw) -> ExternalProcessorConnection:
    api_key = (raw.pop('api_key', '') or '').strip() or decrypt_secret(connection.api_key_encrypted)
    api_secret = (raw.pop('api_secret', '') or '').strip() or decrypt_secret(connection.api_secret_encrypted)
    webhook_secret = (raw.pop('webhook_secret', '') or '').strip() or decrypt_secret(connection.webhook_secret_encrypted)
    webhook_key_id = (raw.pop('webhook_key_id', '') or '').strip() or connection.webhook_key_id
    values = _connection_values(api_key=api_key, api_secret=api_secret, webhook_key_id=webhook_key_id, webhook_secret=webhook_secret, **raw)
    credentials_changed = any((
        connection.api_base_url != values['api_base_url'],
        decrypt_secret(connection.api_key_encrypted) != values['api_key'],
        decrypt_secret(connection.api_secret_encrypted) != values['api_secret'],
    ))
    if values['routing_enabled'] and (connection.last_check_status != 'success' or credentials_changed):
        raise ExternalProcessorError('connection_check_required', 'После изменения адреса или API-ключей повторите проверку подключения')
    connection.name = values['name']
    connection.adapter_type = values['adapter_type']
    connection.environment = values['environment']
    connection.api_base_url = values['api_base_url']
    connection.api_key_encrypted = encrypt_secret(values['api_key'])
    connection.api_secret_encrypted = encrypt_secret(values['api_secret'])
    connection.webhook_key_id = values['webhook_key_id']
    connection.webhook_secret_encrypted = encrypt_secret(values['webhook_secret'])
    connection.supported_methods = values['supported_methods']
    connection.supported_currencies = values['supported_currencies']
    connection.min_amount = values['min_amount']
    connection.max_amount = values['max_amount']
    connection.daily_limit = values['daily_limit']
    connection.monthly_limit = values['monthly_limit']
    connection.executor_fee_percent = values['executor_fee_percent']
    connection.priority = values['priority']
    connection.routing_enabled = values['routing_enabled']
    connection.updated_by = actor_id
    connection.config_version = int(connection.config_version or 1) + 1
    if credentials_changed:
        connection.last_check_status = 'never'
        connection.last_check_error = None
        connection.last_checked_at = None
    await db.flush()
    await _replace_processor_fee_rules(db, connection, actor_id)
    await _snapshot_connection(db, connection, actor_id)
    return connection


def _normalize_path(path: str) -> str:
    normalized = unicodedata.normalize('NFC', path or '/')
    if not normalized.startswith('/'):
        normalized = '/' + normalized
    return quote(normalized, safe='/-._~')


def _canonical_query(query: str) -> str:
    values = [(quote(k, safe='-._~'), quote(v, safe='-._~')) for k, v in parse_qsl(query, keep_blank_values=True)]
    values.sort(key=lambda item: (item[0].encode(), item[1].encode()))
    return '&'.join(f'{key}={value}' for key, value in values)


def _signed_headers(version: ExternalProcessorVersion | ExternalProcessorConnection, method: str, url: str, body: bytes, idempotency_key: str | None = None) -> dict[str, str]:
    parsed = urlsplit(url)
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24).replace('=', '')
    content_type = 'application/json' if body else ''
    canonical = '\n'.join((
        'v2', timestamp, nonce, method.upper(), _normalize_path(parsed.path), _canonical_query(parsed.query),
        content_type, hashlib.sha256(body).hexdigest(),
    ))
    signature = hmac.new(decrypt_secret(version.api_secret_encrypted).encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        'X-API-Key': decrypt_secret(version.api_key_encrypted),
        'X-Signature-Version': '2', 'X-Timestamp': timestamp, 'X-Nonce': nonce,
        'X-Signature': signature, 'Accept': 'application/json',
    }
    if body:
        headers['Content-Type'] = content_type
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key
    return headers


async def _request(version: ExternalProcessorVersion | ExternalProcessorConnection, method: str, suffix: str, *, payload: dict | None = None, idempotency_key: str | None = None) -> ProcessorHttpResponse:
    # Revalidate on every request so a saved production hostname cannot later
    # be changed to a private destination through DNS or configuration drift.
    base = normalize_processor_base_url(version.api_base_url, version.environment)
    url = base + suffix
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode() if payload is not None else b''
    headers = _signed_headers(version, method, url, body, idempotency_key)
    parsed_url = urlsplit(url)
    if version.environment == 'sandbox' and parsed_url.hostname == 'host.docker.internal':
        # Docker Desktop reaches the Windows host through this DNS alias, while
        # the independently configured local processor accepts loopback Host.
        headers['Host'] = f'127.0.0.1:{parsed_url.port or 80}'
    try:
        async with httpx.AsyncClient(timeout=settings.EXTERNAL_PROCESSOR_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url, content=body if body else None, headers=headers)
    except httpx.TransportError as exc:
        raise ExternalProcessorError('external_processor_result_unknown', 'Ответ внешней площадки не получен; состояние будет сверено автоматически', status_code=503, unknown=True) from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    return ProcessorHttpResponse(response.status_code, data if isinstance(data, dict) else {})


def _response_error(response: ProcessorHttpResponse) -> str:
    error = response.payload.get('error') if isinstance(response.payload, dict) else None
    if isinstance(error, dict):
        return str(error.get('code') or error.get('message') or f'HTTP {response.status_code}')[:300]
    detail = response.payload.get('detail') if isinstance(response.payload, dict) else None
    return str(detail or f'HTTP {response.status_code}')[:300]


async def check_connection(db: AsyncSession, connection: ExternalProcessorConnection) -> tuple[bool, str]:
    probe = f'ozark-check-{uuid.uuid4().hex}'
    try:
        response = await _request(connection, 'GET', f'/deposits/{probe}')
        if response.status_code == 404:
            ok, message = True, 'Авторизация и Merchant API доступны'
        elif response.status_code in {401, 403}:
            ok, message = False, f'Авторизация отклонена: {_response_error(response)}'
        else:
            ok, message = False, f'Неожиданный ответ внешней площадки: HTTP {response.status_code} ({_response_error(response)})'
    except ExternalProcessorError as exc:
        ok, message = False, str(exc)
    connection.last_checked_at = datetime.now(timezone.utc)
    connection.last_check_status = 'success' if ok else 'failed'
    connection.last_check_error = None if ok else message[:500]
    await db.flush()
    return ok, message


async def _limit_used(db: AsyncSession, connection_id: uuid.UUID, *, since: datetime) -> Decimal:
    value = (await db.execute(select(func.coalesce(func.sum(ExternalProcessorPayment.amount), 0)).where(
        ExternalProcessorPayment.connection_id == connection_id,
        ExternalProcessorPayment.request_status.in_(COUNTED_PAYMENT_STATUSES),
        ExternalProcessorPayment.created_at >= since,
    ))).scalar_one()
    return money(value)


async def select_connection(db: AsyncSession, *, environment: str, method: str, currency: str, amount: Decimal) -> ExternalProcessorConnection | None:
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(ExternalProcessorConnection).where(
        ExternalProcessorConnection.routing_enabled.is_(True),
        ExternalProcessorConnection.is_archived.is_(False),
        ExternalProcessorConnection.environment == environment,
        ExternalProcessorConnection.last_check_status == 'success',
    ).order_by(
        ExternalProcessorConnection.priority.asc(),
        ExternalProcessorConnection.created_at.asc(),
    ).with_for_update().execution_options(populate_existing=True))).scalars().all()
    amount = money(amount)
    for row in rows:
        if method not in (row.supported_methods or []) or currency not in (row.supported_currencies or []):
            continue
        if amount < money(row.min_amount) or amount > money(row.max_amount):
            continue
        if row.daily_limit is not None:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if await _limit_used(db, row.id, since=start) + amount > money(row.daily_limit):
                continue
        if row.monthly_limit is not None:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if await _limit_used(db, row.id, since=start) + amount > money(row.monthly_limit):
                continue
        return row
    return None


async def prepare_payment(db: AsyncSession, *, merchant_id: uuid.UUID, external_id: str, idempotency_key: str,
                          request_fingerprint: str, amount: Decimal, currency: str, method: str,
                          metadata: dict, client_ip: str, integration_mode: str, merchant_rate_rule,
                          rolling_quote, risk_score: int, risk_reason: str | None, created_at: datetime) -> tuple[Deposit, ExternalProcessorPayment]:
    environment = integration_mode if integration_mode in {'sandbox', 'production'} else 'sandbox'
    connection = await select_connection(db, environment=environment, method=method, currency=currency, amount=amount)
    if not connection:
        raise ExternalProcessorError('no_external_processor', 'Нет доступного внешнего обработчика для этих условий')
    version = (await db.execute(select(ExternalProcessorVersion).where(
        ExternalProcessorVersion.connection_id == connection.id,
        ExternalProcessorVersion.version == connection.config_version,
    ))).scalar_one()
    executor_rule = await resolve_fee_rule(db, entity_type='external_processor', entity_id=connection.id,
                                           fee_side='executor_fee', payment_method=method, currency=currency, amount=amount)
    validate_margin(merchant_rate_rule, executor_rule)
    deposit = Deposit(
        merchant_id=merchant_id, external_id=external_id, idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint, amount=money(amount), currency=currency, method=method,
        status=DepositStatus.pending.value, requisites_id=None, client_ip=client_ip, created_at=created_at,
        expires_at=new_deposit_expires_at(), metadata_json={
            **(metadata or {}), 'risk_score': risk_score, 'risk_reason': risk_reason,
            'integration_mode': integration_mode, 'processing_route': 'external_processor',
            'external_processor_connection_id': str(connection.id), 'external_processor_version': version.version,
        },
    )
    db.add(deposit)
    await db.flush()
    snapshot = await create_operation_fee_snapshot(
        db, deposit=deposit, merchant_rule=merchant_rate_rule, executor_rule=executor_rule,
        executor_type='external_processor', executor_id=connection.id,
    )
    await create_pending_allocation(db, deposit=deposit, snapshot=snapshot, quote=rolling_quote)
    processor_payment = ExternalProcessorPayment(
        deposit_id=deposit.id, connection_id=connection.id, connection_version_id=version.id,
        idempotency_key=f'ozark:{deposit.id}', external_order_id=f'ozark-{deposit.id}',
        amount=money(amount), currency=currency, payment_method=method, request_fingerprint=request_fingerprint,
        request_status='prepared', fee_percent=executor_rule.rate_percent,
        fee_amount=fee_amount(money(amount), executor_rule.rate_percent),
        metadata_json={'merchant_external_id': external_id, 'merchant_id': str(merchant_id)},
    )
    db.add(processor_payment)
    await db.flush()
    dep_meta = dict(deposit.metadata_json or {})
    dep_meta['external_processor_payment_id'] = str(processor_payment.id)
    deposit.metadata_json = dep_meta
    return deposit, processor_payment


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_operation_payload(payment: ExternalProcessorPayment, payload: dict) -> None:
    if str(payload.get('external_id') or '') != payment.external_order_id:
        raise ExternalProcessorError('external_response_mismatch', 'Внешняя площадка вернула другой external_id', status_code=502)
    if payload.get('amount') is not None and money(payload['amount']) != money(payment.amount):
        raise ExternalProcessorError('external_response_mismatch', 'Внешняя площадка вернула другую сумму', status_code=502)
    if str(payload.get('currency') or payment.currency).upper() != payment.currency:
        raise ExternalProcessorError('external_response_mismatch', 'Внешняя площадка вернула другую валюту', status_code=502)
    returned_method = str(payload.get('method') or payload.get('payment_method') or payment.payment_method)
    if returned_method == 'mobile':
        returned_method = 'mobile_commerce'
    if returned_method != payment.payment_method:
        raise ExternalProcessorError('external_response_mismatch', 'Внешняя площадка вернула другой метод', status_code=502)


def _preserve_terminal_state(payment: ExternalProcessorPayment, deposit: Deposit) -> bool:
    """Terminal deposits are authoritative; late provider messages never reopen them."""
    if deposit.status in TERMINAL_EXTERNAL_STATUSES:
        payment.request_status = deposit.status
        payment.external_status = deposit.status
        return True
    return payment.request_status in FINAL_REQUEST_STATUSES


async def _payment_deposit_locked(db: AsyncSession, payment: ExternalProcessorPayment) -> Deposit:
    return (await db.execute(select(Deposit).where(Deposit.id == payment.deposit_id)
        .with_for_update().execution_options(populate_existing=True))).scalar_one()


def _record_created_response(payment: ExternalProcessorPayment, payload: dict, details: dict, status_code: int) -> None:
    """Store returned requisites without regressing a webhook-finalized attempt."""
    payment.external_payment_id = str(payload.get('id') or '') or payment.external_payment_id
    if payment.request_status not in FINAL_REQUEST_STATUSES:
        payment.external_status = str(payload.get('status') or 'pending')
        payment.request_status = 'created'
    payment.payment_details_snapshot = details
    payment.response_status_code = status_code
    payment.last_error = None
    payment.last_reconciled_at = datetime.now(timezone.utc)


async def _apply_created_response(db: AsyncSession, payment: ExternalProcessorPayment, response: ProcessorHttpResponse) -> Deposit:
    payload = response.payload
    _validate_operation_payload(payment, payload)
    details = payload.get('payment_details')
    if not isinstance(details, dict) or not details:
        raise ExternalProcessorError('external_requisites_missing', 'Внешняя площадка не вернула платёжные реквизиты', status_code=502)
    deposit = (await db.execute(select(Deposit).where(Deposit.id == payment.deposit_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    _preserve_terminal_state(payment, deposit)
    _record_created_response(payment, payload, details, response.status_code)
    expires = _parse_iso(payload.get('expires_at'))
    if expires:
        payment.expires_at = expires
        deposit.expires_at = expires
    meta = dict(deposit.metadata_json or {})
    meta.update({'external_payment_id': payment.external_payment_id, 'external_payment_status': payment.external_status,
                 'external_payment_details': details})
    deposit.metadata_json = meta
    await db.flush()
    return deposit


async def dispatch_payment(db: AsyncSession, payment_id: uuid.UUID) -> Deposit:
    payment = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    version = (await db.execute(select(ExternalProcessorVersion).where(ExternalProcessorVersion.id == payment.connection_version_id))).scalar_one()
    deposit = await _payment_deposit_locked(db, payment)
    if _preserve_terminal_state(payment, deposit):
        await db.commit()
        return deposit
    payment.request_status = 'sending'
    payment.last_error = None
    await db.commit()
    payload = {
        'external_id': payment.external_order_id, 'amount': str(money(payment.amount)),
        'currency': payment.currency, 'method': payment.payment_method,
        'metadata': {'source': 'ozarkpay', 'ozark_deposit_id': str(payment.deposit_id)},
    }
    try:
        response = await _request(version, 'POST', '/deposits', payload=payload, idempotency_key=payment.idempotency_key)
    except ExternalProcessorError as exc:
        payment = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        deposit = await _payment_deposit_locked(db, payment)
        if _preserve_terminal_state(payment, deposit):
            await db.commit()
            return deposit
        payment.request_status = 'unknown' if exc.unknown else 'failed'
        payment.last_error = exc.code
        payment.last_reconciled_at = datetime.now(timezone.utc)
        await db.commit()
        raise
    payment = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    if 200 <= response.status_code < 300:
        deposit = await _apply_created_response(db, payment, response)
        await db.commit()
        return deposit
    deposit = await _payment_deposit_locked(db, payment)
    if _preserve_terminal_state(payment, deposit):
        await db.commit()
        return deposit
    error = _response_error(response)
    payment.response_status_code = response.status_code
    payment.last_error = error
    payment.last_reconciled_at = datetime.now(timezone.utc)
    if response.status_code >= 500:
        payment.request_status = 'unknown'
        await db.commit()
        raise ExternalProcessorError('external_processor_result_unknown', 'Внешняя площадка вернула временную ошибку; состояние будет сверено автоматически', status_code=503, unknown=True)
    payment.request_status = 'refused'
    await db.commit()
    raise ExternalProcessorError('external_processor_refused', f'Внешняя площадка отклонила заявку: {error}', status_code=409)


async def reconcile_payment(db: AsyncSession, payment_id: uuid.UUID, *, retry_create_if_missing: bool = True) -> Deposit | None:
    payment = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment_id))).scalar_one_or_none()
    if not payment:
        return None
    version = (await db.execute(select(ExternalProcessorVersion).where(ExternalProcessorVersion.id == payment.connection_version_id))).scalar_one()
    response = await _request(version, 'GET', f'/deposits/{quote(payment.external_order_id, safe="-._~")}')
    if 200 <= response.status_code < 300:
        locked = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment.id).with_for_update().execution_options(populate_existing=True))).scalar_one()
        deposit = await _apply_created_response(db, locked, response)
        if _preserve_terminal_state(locked, deposit):
            await db.commit()
            return deposit
        external_status = str(response.payload.get('status') or '').lower()
        delivery_ids: list[uuid.UUID] = []
        if external_status == 'paid' and deposit.status != DepositStatus.paid.value:
            try:
                confirmation = await confirm_deposit_payment(
                    db, deposit.id, actor_id=None, actor_ip='',
                    audit_action='external_processor_deposit_reconciled_paid',
                    description='External processor reconciliation', commit=False,
                )
            except DepositConfirmationConflict:
                await db.rollback()
                return None
            locked = (await db.execute(select(ExternalProcessorPayment).where(
                ExternalProcessorPayment.id == payment.id
            ).with_for_update().execution_options(populate_existing=True))).scalar_one()
            locked.request_status = 'paid' if confirmation.confirmed else confirmation.status
            locked.external_status = locked.request_status
            if confirmation.webhook_event_id:
                delivery_ids.append(confirmation.webhook_event_id)
        elif external_status in {'failed', 'expired', 'cancelled'} and deposit.status not in {
            DepositStatus.failed.value, DepositStatus.expired.value, DepositStatus.cancelled.value,
        }:
            target = {
                'failed': DepositStatus.failed.value,
                'expired': DepositStatus.expired.value,
                'cancelled': DepositStatus.cancelled.value,
            }[external_status]
            finalization = await finalize_unsuccessful_deposit(
                db, deposit, reason=f'external_processor_{external_status}', target_status=target,
                actor_id=None, actor_role='external_processor', actor_ip=None,
                audit_action='external_processor_deposit_reconciled_unsuccessful',
            )
            locked.request_status = external_status
            locked.external_status = external_status
            if finalization.webhook_event_id:
                delivery_ids.append(finalization.webhook_event_id)
        await db.commit()
        for delivery_id in delivery_ids:
            try:
                enqueue_webhook_delivery(delivery_id)
            except Exception:
                logger.exception('external_processor_reconcile_webhook_enqueue_failed', extra={'event_id': str(delivery_id)})
        return deposit
    if response.status_code == 404 and retry_create_if_missing and payment.request_status in {'prepared', 'sending', 'unknown'}:
        return await dispatch_payment(db, payment.id)
    locked = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.id == payment.id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    locked.last_error = _response_error(response)
    locked.last_reconciled_at = datetime.now(timezone.utc)
    await db.commit()
    return None


async def due_reconciliation_payment_ids(db: AsyncSession, *, limit: int = 100) -> list[uuid.UUID]:
    """Return non-terminal outbound attempts that need a status check or safe retry."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=45)
    return list((await db.execute(
        select(ExternalProcessorPayment.id)
        .where(
            ExternalProcessorPayment.request_status.in_(['prepared', 'sending', 'unknown', 'created']),
            or_(
                ExternalProcessorPayment.last_reconciled_at.is_(None),
                ExternalProcessorPayment.last_reconciled_at <= cutoff,
            ),
        )
        .order_by(ExternalProcessorPayment.created_at.asc())
        .limit(limit)
    )).scalars().all())


async def payment_details_for_deposit(db: AsyncSession, deposit_id: uuid.UUID) -> dict | None:
    payment = (await db.execute(select(ExternalProcessorPayment).where(ExternalProcessorPayment.deposit_id == deposit_id))).scalar_one_or_none()
    return dict(payment.payment_details_snapshot or {}) if payment and payment.payment_details_snapshot else None


async def recover_existing_payment(db: AsyncSession, deposit: Deposit) -> Deposit:
    payment = (await db.execute(select(ExternalProcessorPayment).where(
        ExternalProcessorPayment.deposit_id == deposit.id,
    ))).scalar_one_or_none()
    if not payment:
        return deposit
    if payment.request_status in {'created', 'paid'}:
        return deposit
    if payment.request_status in {'refused', 'failed'}:
        raise ExternalProcessorError(
            'external_processor_refused',
            f'Внешний обработчик не принял заявку: {payment.last_error or "отказ без описания"}',
            status_code=409,
        )
    recovered = await reconcile_payment(db, payment.id, retry_create_if_missing=True)
    if recovered is None:
        raise ExternalProcessorError(
            'external_processor_result_unknown',
            'Результат передачи во внешний обработчик пока неизвестен; сверка продолжится автоматически',
            status_code=503,
            unknown=True,
        )
    return recovered


def _verify_webhook_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or '').strip().lower())


async def handle_processor_webhook(db: AsyncSession, connection_id: uuid.UUID, *, headers, body: bytes, source_ip: str | None) -> tuple[int, tuple[uuid.UUID, ...]]:
    connection = await db.get(ExternalProcessorConnection, connection_id)
    if not connection:
        raise ExternalProcessorError('connection_not_found', 'Подключение не найдено', status_code=404)
    prefix = connection.webhook_header_prefix
    from starlette.datastructures import Headers
    headers = Headers(headers)
    event_id = (headers.get(prefix + '-Event-ID') or '').strip()
    event_type = (headers.get(prefix + '-Event-Type') or '').strip()
    timestamp = (headers.get(prefix + '-Timestamp') or '').strip()
    key_id = (headers.get(prefix + '-Key-ID') or '').strip()
    signature_version = (headers.get(prefix + '-Signature-Version') or '').strip()
    signature = (headers.get(prefix + '-Signature') or '').strip()
    correlation_id = (headers.get('X-Correlation-ID') or '').strip() or None
    if not all((event_id, event_type, timestamp, key_id, signature)) or signature_version != '1':
        raise ExternalProcessorError('webhook_headers_invalid', 'Обязательные заголовки webhook отсутствуют', status_code=401)
    try:
        event_ts = int(timestamp)
    except ValueError as exc:
        raise ExternalProcessorError('webhook_timestamp_invalid', 'Некорректное время webhook', status_code=401) from exc
    if abs(int(time.time()) - event_ts) > settings.EXTERNAL_PROCESSOR_WEBHOOK_TOLERANCE_SECONDS:
        raise ExternalProcessorError('webhook_expired', 'Webhook находится вне допустимого окна времени', status_code=401)
    versions = (await db.execute(select(ExternalProcessorVersion).where(
        ExternalProcessorVersion.connection_id == connection_id,
        ExternalProcessorVersion.webhook_key_id == key_id,
    ).order_by(ExternalProcessorVersion.version.desc()))).scalars().all()
    if not versions or not any(_verify_webhook_signature(decrypt_secret(row.webhook_secret_encrypted), timestamp, body, signature) for row in versions):
        raise ExternalProcessorError('webhook_signature_invalid', 'Неверная подпись webhook', status_code=401)
    try:
        envelope = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalProcessorError('webhook_json_invalid', 'Некорректный JSON webhook', status_code=400) from exc
    if not isinstance(envelope, dict) or envelope.get('event') != event_type or not isinstance(envelope.get('payload'), dict):
        raise ExternalProcessorError('webhook_payload_invalid', 'Тип события или payload не совпадает с заголовком', status_code=400)
    payload = envelope['payload']
    payment = (await db.execute(select(ExternalProcessorPayment).where(
        ExternalProcessorPayment.connection_id == connection_id,
        or_(
            ExternalProcessorPayment.external_payment_id == str(payload.get('id') or ''),
            ExternalProcessorPayment.external_order_id == str(payload.get('external_id') or ''),
        ),
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    existing = (await db.execute(select(ExternalProcessorWebhookEvent).where(
        ExternalProcessorWebhookEvent.connection_id == connection_id,
        ExternalProcessorWebhookEvent.external_event_id == event_id,
    ))).scalar_one_or_none()
    if existing:
        return 204, ()
    event = ExternalProcessorWebhookEvent(
        connection_id=connection_id, processor_payment_id=payment.id if payment else None,
        external_event_id=event_id, event_type=event_type, key_id=key_id, event_timestamp=event_ts,
        correlation_id=correlation_id, payload_json=envelope, status='processing',
    )
    db.add(event)
    await db.flush()
    if not payment:
        event.status = 'rejected'
        event.result_message = 'unknown_external_payment'
        event.processed_at = datetime.now(timezone.utc)
        await db.commit()
        raise ExternalProcessorError('external_payment_not_found', 'Связанная заявка OzarkPay не найдена', status_code=404)
    try:
        _validate_operation_payload(payment, payload)
    except ExternalProcessorError as exc:
        event.status = 'rejected'
        event.result_message = exc.code
        event.processed_at = datetime.now(timezone.utc)
        await db.commit()
        raise
    deposit = (await db.execute(select(Deposit).where(Deposit.id == payment.deposit_id).with_for_update().execution_options(populate_existing=True))).scalar_one()
    payment.external_payment_id = str(payload.get('id') or payment.external_payment_id or '') or None
    external_status = str(payload.get('status') or event_type.removeprefix('deposit.'))
    valid_statuses = {
        'deposit.paid': {'paid'}, 'deposit.failed': {'failed'},
        'deposit.expired': {'expired'}, 'deposit.cancelled': {'cancelled'},
        'deposit.pending': {'pending', 'created'},
    }
    if external_status not in valid_statuses.get(event_type, set()):
        event.status = 'rejected'
        event.result_message = 'unsupported_or_mismatched_event'
        event.processed_at = datetime.now(timezone.utc)
        await db.commit()
        raise ExternalProcessorError('webhook_event_unsupported', 'Тип события не соответствует статусу', status_code=409)
    payment.last_reconciled_at = datetime.now(timezone.utc)
    delivery_ids: tuple[uuid.UUID, ...] = ()
    if _preserve_terminal_state(payment, deposit):
        event.status = 'duplicate' if external_status == payment.request_status else 'ignored'
        event.result_message = 'terminal_state_preserved'
    elif event_type == 'deposit.paid' and external_status == 'paid':
        if deposit.status == DepositStatus.paid.value:
            event.status = 'duplicate'
            event.result_message = 'deposit_already_paid'
        else:
            try:
                result = await confirm_deposit_payment(
                    db, deposit.id, actor_id=None, actor_ip=source_ip or '',
                    audit_action='external_processor_deposit_paid',
                    description='External processor confirmation', commit=False,
                )
            except DepositConfirmationConflict as exc:
                payment_id = payment.id
                db.add(ExternalProcessorWebhookEvent(
                    connection_id=connection_id, processor_payment_id=payment_id,
                    external_event_id=event_id, event_type=event_type, key_id=key_id,
                    event_timestamp=event_ts, correlation_id=correlation_id,
                    payload_json=envelope, status='rejected',
                    result_message=exc.api_detail[:500], processed_at=datetime.now(timezone.utc),
                ))
                await db.commit()
                raise ExternalProcessorError('external_transition_conflict', exc.public_message, status_code=409) from exc
            payment.request_status = 'paid' if result.confirmed else result.status
            payment.external_status = payment.request_status
            event.status = 'applied' if result.confirmed else 'rejected'
            event.result_message = 'merchant_webhook_queued' if result.confirmed else (result.reason or 'confirmation_rejected')
            delivery_ids = (result.webhook_event_id,) if result.webhook_event_id else ()
    elif event_type in {'deposit.failed', 'deposit.expired', 'deposit.cancelled'}:
        target = {'deposit.failed': DepositStatus.failed.value, 'deposit.expired': DepositStatus.expired.value,
                  'deposit.cancelled': DepositStatus.cancelled.value}[event_type]
        finalization = await finalize_unsuccessful_deposit(
            db, deposit, reason=f'external_processor_{external_status}', target_status=target,
            actor_id=None, actor_role='external_processor', actor_ip=source_ip,
            request_id=correlation_id, audit_action='external_processor_deposit_finalized',
        )
        payment.request_status = external_status if external_status in TERMINAL_EXTERNAL_STATUSES else target
        payment.external_status = payment.request_status
        event.status = 'applied' if finalization.changed else 'duplicate'
        event.result_message = finalization.reason
        delivery_ids = (finalization.webhook_event_id,) if finalization.webhook_event_id else ()
    elif event_type == 'deposit.pending' and external_status in {'created', 'pending'}:
        payment.request_status = 'created'
        payment.external_status = external_status
        event.status = 'applied'
        event.result_message = 'pending_status_recorded'
    else:
        event.status = 'rejected'
        event.result_message = 'unsupported_or_out_of_order_event'
        event.processed_at = datetime.now(timezone.utc)
        await db.commit()
        raise ExternalProcessorError('webhook_event_unsupported', 'Событие не поддерживается для текущего состояния', status_code=409)
    event.processed_at = datetime.now(timezone.utc)
    await audit(db, 'external_processor_webhook_processed', 'external_processor_payment', None, payment.id, source_ip,
                {'event_id': event_id, 'event_type': event_type, 'result': event.status})
    await db.commit()
    for delivery_id in delivery_ids:
        try:
            enqueue_webhook_delivery(delivery_id)
        except Exception:
            logger.exception('external_processor_merchant_webhook_enqueue_failed', extra={'event_id': str(delivery_id)})
    return 204, delivery_ids
