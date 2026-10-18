"""Provider-neutral contract. No real provider protocol or QA adapter is shipped."""
import asyncio
import http.client
import ipaddress
import socket
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit


class ProviderError(ValueError):
    """Safe diagnostic codes only; never attach remote bodies, URLs or keys."""


class ProviderRequestNotSent(ProviderError):
    """Adapter guarantees that no request bytes could have reached the provider."""


class ProviderOrderAbsent(ProviderError):
    """Documented, authoritative lookup proves absence by stable external ID."""


@dataclass(frozen=True)
class PaymentRequest:
    external_id: str
    amount: Decimal
    currency: str
    method: str
    callback_url: str


@dataclass(frozen=True)
class ProviderResult:
    external_id: str
    provider_order_id: str
    amount: Decimal
    currency: str
    status: str
    requisites: dict | None = None
    expires_at: datetime | None = None
    no_credit_confirmed: bool = False
    payment_confirmed: bool = False


@dataclass(frozen=True)
class VerifiedWebhook:
    """Returned only after adapter-specific crypto, timestamp and ID validation."""
    event_id: str
    webhook_id: str
    occurred_at: datetime
    result: ProviderResult


class AggregatorAdapter(ABC):
    supports_idempotent_create = False
    supports_status_by_external_id = False

    @abstractmethod
    async def create_payment(self, request: PaymentRequest, credentials: dict) -> ProviderResult: ...

    @abstractmethod
    async def get_payment_status(self, request: PaymentRequest, provider_order_id: str | None,
                                 credentials: dict) -> ProviderResult: ...

    @abstractmethod
    def validate_webhook(self, body: bytes, headers: dict, credentials: dict) -> VerifiedWebhook: ...

    @abstractmethod
    def normalize_requisites(self, requisites: dict) -> dict: ...

    @abstractmethod
    def normalize_status(self, status: str) -> str: ...

    async def cancel_payment(self, request, provider_order_id, credentials):
        raise ProviderError('cancel_not_supported')

    async def verify_connection(self, credentials):
        return False, 'verification_not_supported'

    async def reconcile_payment(self, request, provider_order_id, credentials):
        return await self.get_payment_status(request, provider_order_id, credentials)


def validate_base_url(value):
    """Configuration validation; DNS is rechecked and pinned before each send."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.port not in (None, 443)
                or parts.username or parts.password or parts.query or parts.fragment
                or '\\' in value or any(ord(c) < 33 for c in value)
                or len(value) > 500 or host.endswith('.') or '.' not in host):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        host = host.encode('idna').decode('ascii').lower()
        if host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
            raise ValueError
        return 'https://' + host + parts.path.rstrip('/')
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ProviderError('unsafe_api_base_url') from exc


def public_addresses(host):
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = sorted({item[4][0] for item in records})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ProviderError('unsafe_api_destination')
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=8, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


async def safe_request(base_url, approved_host, method, path, headers, body=b''):
    """TLS/SNI pinned to a checked public IP; no redirects, proxy env or retries.

    A reviewed concrete adapter must supply its documented path and auth headers.
    This helper does not invent a common external API.
    """
    base = validate_base_url(base_url)
    host = urlsplit(base).hostname
    if host != approved_host or not path.startswith('/') or path.startswith('//') or '?' in path:
        raise ProviderError('unapproved_api_destination')
    if method not in ('GET', 'POST', 'DELETE') or len(body) > 262144:
        raise ProviderError('invalid_provider_request')
    addresses = await asyncio.wait_for(asyncio.to_thread(public_addresses, host), 5)

    connection = PinnedHTTPSConnection(host, addresses[0])
    def send():
        try:
            connection.request(method, urlsplit(base).path + path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(262145)
            if len(payload) > 262144 or 300 <= response.status < 400:
                raise ProviderError('unsafe_provider_response')
            return response.status, payload
        except (OSError, http.client.HTTPException) as exc:
            raise ProviderError('provider_transport_unknown') from exc
        finally:
            connection.close()

    try:
        return await asyncio.wait_for(asyncio.to_thread(send), 15)
    except asyncio.TimeoutError:
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
        raise ProviderError('provider_transport_unknown') from None
