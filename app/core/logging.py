import json
import logging
import re
import sys
from datetime import datetime, timezone
from collections.abc import Mapping

from app.core.config import settings


RESERVED_LOG_RECORD_FIELDS = {
    'args',
    'asctime',
    'created',
    'exc_info',
    'exc_text',
    'filename',
    'funcName',
    'levelname',
    'levelno',
    'lineno',
    'module',
    'msecs',
    'message',
    'msg',
    'name',
    'pathname',
    'process',
    'processName',
    'relativeCreated',
    'stack_info',
    'thread',
    'threadName',
}

SENSITIVE_KEY_PARTS = {
    'authorization',
    'password',
    'secret',
    'token',
    'refresh',
    'access',
    'hmac',
    'signature',
    '2fa',
    'otp',
    'totp',
    'private_key',
    'seed',
    'api_key',
    'x-api-key',
    'x-signature',
}

BEARER_RE = re.compile(r'\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', re.IGNORECASE)
ASSIGNMENT_RE = re.compile(
    r'\b(password|secret|token|refresh_token|access_token|otp|totp|api_key|signature)=([^&\s,;]+)',
    re.IGNORECASE,
)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key or '').lower().replace('-', '_')
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def mask_secret(value: object) -> str:
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 8:
        return '***redacted***'
    return f'{text[:4]}...{text[-4:]}'


def mask_token(value: object) -> str:
    text = str(value or '')
    if not text:
        return ''
    if len(text) <= 12:
        return '***redacted***'
    return f'{text[:6]}...{text[-6:]}'


def redact_sensitive(value, key: object | None = None):
    if _is_sensitive_key(key):
        if isinstance(value, str) and BEARER_RE.search(value):
            return BEARER_RE.sub(lambda match: f'{match.group(1)} ***redacted***', value)
        key_text = str(key or '').lower()
        return mask_token(value) if 'token' in key_text or 'authorization' in key_text else mask_secret(value)
    if isinstance(value, Mapping):
        return {item_key: redact_sensitive(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        redacted = BEARER_RE.sub(lambda match: f'{match.group(1)} ***redacted***', value)
        return ASSIGNMENT_RE.sub(lambda match: f'{match.group(1)}=***redacted***', redacted)
    return value


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': redact_sensitive(record.getMessage()),
            'service': settings.APP_NAME,
            'env': settings.ENV,
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in RESERVED_LOG_RECORD_FIELDS or key.startswith('_'):
                continue
            safe_value = redact_sensitive(value, key)
            if isinstance(safe_value, (str, int, float, bool)) or safe_value is None:
                payload[key] = safe_value
            else:
                payload[key] = redact_sensitive(str(safe_value))
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))


def configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    formatter: logging.Formatter
    if settings.LOG_FORMAT.lower() == 'json':
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)

    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
