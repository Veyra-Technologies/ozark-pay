import hashlib
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.config import settings
from app.core.client_ip import client_ip
from app.core.security import auth_state_marker, decode_token, decrypt_secret, verify_hmac
from app.core.validators import ip_in_whitelist
from app.models import ApiKey, ApiReplayNonce, Merchant, User
from app.core.enums import Role
from app.services.merchant_api_keys import normalize_api_key_mode, sandbox_key_allowed_for_merchant

oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/api/v1/auth/login')
STAFF_2FA_ROLES = {
    Role.superadmin.value,
    Role.admin.value,
    Role.support.value,
    Role.teamlead.value,
}
REPLAY_HEADER_NAMES = ('X-Request-ID', 'X-Nonce', 'Idempotency-Key', 'X-Idempotency-Key')
HMAC_REPLAY_PROTECTED_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

MERCHANT_AUTH_ERROR_MESSAGES = {
    'bad_api_key': 'Invalid API key.',
    'bad_timestamp': 'X-Timestamp must be a Unix timestamp.',
    'stale_timestamp': 'X-Timestamp is outside the allowed time window.',
    'bad_signature': 'Invalid HMAC signature.',
    'hmac_replay_detected': 'Signed request replay was detected.',
    'merchant_account_blocked': 'Merchant account is blocked.',
    'ip_not_allowed': 'Request IP is not allowed for this merchant.',
    'merchant_secret_missing': 'Merchant API secret needs regeneration.',
    'sandbox_key_not_allowed_for_production_traffic': 'Sandbox API keys are not allowed for production traffic.',
}


def merchant_auth_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(
        status_code,
        {
            'code': code,
            'message': MERCHANT_AUTH_ERROR_MESSAGES.get(code, code),
            'details': {},
        },
    )


def hmac_replay_hashes(request: Request, body: bytes) -> list[str]:
    body_hash = hashlib.sha256(body).hexdigest()
    query = request.url.query
    target = request.url.path + (f'?{query}' if query else '')
    route_fingerprint = f'{request.method.upper()}\n{target}\n{body_hash}'
    hashes = [hashlib.sha256(route_fingerprint.encode('utf-8')).hexdigest()]
    nonce_parts = []
    for name in REPLAY_HEADER_NAMES:
        value = (request.headers.get(name) or '').strip()
        if value:
            nonce_parts.append(f'{name.lower()}={value}')
    if nonce_parts:
        nonce_fingerprint = f'{request.method.upper()}\n{target}\n' + '\n'.join(sorted(nonce_parts))
        nonce_body_fingerprint = f'{route_fingerprint}\n' + '\n'.join(sorted(nonce_parts))
        hashes.append(hashlib.sha256(nonce_fingerprint.encode('utf-8')).hexdigest())
        hashes.append(hashlib.sha256(nonce_body_fingerprint.encode('utf-8')).hexdigest())
    return list(dict.fromkeys(hashes))


def should_enforce_hmac_replay(request: Request) -> bool:
    return request.method.upper() in HMAC_REPLAY_PROTECTED_METHODS


async def current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    try:
        payload = decode_token(token)
        if payload.get('type') != 'access':
            raise ValueError()
    except Exception:
        raise HTTPException(401, 'invalid credentials')
    user = (await db.execute(select(User).where(User.id == payload['sub']))).scalar_one_or_none()
    if not user or not user.is_active or user.is_locked or getattr(user, 'is_archived', False):
        raise HTTPException(403, 'user blocked')
    if payload.get('auth') != auth_state_marker(user.password_hash):
        raise HTTPException(401, 'session expired')
    return user


def require_roles(*roles: Role):
    async def dep(user: User = Depends(current_user)):
        if user.role not in [r.value for r in roles]:
            raise HTTPException(403, 'not enough permissions')
        if user.role in STAFF_2FA_ROLES and not user.twofa_enabled:
            raise HTTPException(403, '2FA setup required')
        return user
    return dep


async def merchant_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: str = Header(...),
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
) -> Merchant:
    key = (await db.execute(select(ApiKey).where(ApiKey.api_key == x_api_key, ApiKey.is_active == True))).scalar_one_or_none()
    if not key:
        raise merchant_auth_error(401, 'bad_api_key')
    merchant = (await db.execute(select(Merchant).where(Merchant.id == key.merchant_id))).scalar_one_or_none()
    if not merchant or getattr(merchant, 'is_archived', False):
        raise merchant_auth_error(401, 'bad_api_key')
    owner = (await db.execute(select(User).where(User.id == merchant.owner_id))).scalar_one_or_none()
    if not owner or not owner.is_active or owner.is_locked or getattr(owner, 'is_archived', False):
        raise merchant_auth_error(403, 'merchant_account_blocked')
    ip = client_ip(request)
    if merchant.ip_whitelist and not ip_in_whitelist(ip, merchant.ip_whitelist):
        raise merchant_auth_error(403, 'ip_not_allowed')
    try:
        ts = int(x_timestamp)
    except ValueError:
        raise merchant_auth_error(401, 'bad_timestamp')
    if abs(int(time.time()) - ts) > settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS:
        raise merchant_auth_error(401, 'stale_timestamp')
    body = await request.body()
    secret = decrypt_secret(key.secret_hash)
    if not secret:
        raise merchant_auth_error(401, 'merchant_secret_missing')
    if not verify_hmac(secret, x_timestamp, body, x_signature):
        raise merchant_auth_error(401, 'bad_signature')

    try:
        key_mode = normalize_api_key_mode(getattr(key, 'mode', 'sandbox'))
    except ValueError:
        raise merchant_auth_error(401, 'bad_api_key')
    if key_mode == 'sandbox' and not sandbox_key_allowed_for_merchant(merchant):
        raise merchant_auth_error(403, 'sandbox_key_not_allowed_for_production_traffic')

    key.last_used_at = datetime.now(timezone.utc)
    request.state.merchant_api_key_id = key.id
    request.state.merchant_api_key_mode = key_mode

    if not should_enforce_hmac_replay(request):
        return merchant

    request_hashes = hmac_replay_hashes(request, body)
    now = datetime.now(timezone.utc)
    existing_replay = (await db.execute(
        select(ApiReplayNonce).where(
            ApiReplayNonce.api_key_id == key.id,
            ApiReplayNonce.request_hash.in_(request_hashes),
            ApiReplayNonce.expires_at > now,
        ).limit(1)
    )).scalar_one_or_none()
    if existing_replay:
        raise merchant_auth_error(409, 'hmac_replay_detected')
    expires_at = now + timedelta(seconds=settings.HMAC_TIMESTAMP_TOLERANCE_SECONDS)
    for request_hash in request_hashes:
        db.add(ApiReplayNonce(
            api_key_id=key.id,
            signature=x_signature,
            request_hash=request_hash,
            timestamp=ts,
            expires_at=expires_at,
        ))
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise merchant_auth_error(409, 'hmac_replay_detected') from exc
    return merchant
