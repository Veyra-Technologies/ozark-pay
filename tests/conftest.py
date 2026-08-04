import asyncio
import hashlib
import os

import httpx
import pytest
from redis.asyncio import Redis

from app.core.config import settings


def _isolated_client_ip(node_id: str) -> str:
    worker_id = os.getenv('PYTEST_XDIST_WORKER', 'main')
    digest = hashlib.sha256(f'{worker_id}:{node_id}'.encode()).hexdigest()
    groups = [digest[index:index + 4] for index in range(0, 24, 4)]
    return '2001:db8:' + ':'.join(groups)


async def _clear_test_rate_limit_keys(client_ip: str) -> None:
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        keys = set()
        for pattern in (f'rl:{client_ip}:*', f'rl:*:{client_ip}:*'):
            async for key in redis.scan_iter(
                match=pattern,
                count=100,
            ):
                keys.add(key)
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


@pytest.fixture
def clear_test_rate_limit_keys():
    return _clear_test_rate_limit_keys


@pytest.fixture(autouse=True)
def isolated_rate_limit_ip(monkeypatch, request):
    if settings.ENV.lower() != 'test':
        yield None
        return

    client_ip = _isolated_client_ip(request.node.nodeid)
    asyncio.run(_clear_test_rate_limit_keys(client_ip))
    original_init = httpx.ASGITransport.__init__

    def isolated_transport_init(self, *args, **kwargs):
        client_was_positional = len(args) >= 4
        if not client_was_positional and 'client' not in kwargs:
            kwargs['client'] = (client_ip, 123)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.ASGITransport, '__init__', isolated_transport_init)
    try:
        yield client_ip
    finally:
        asyncio.run(_clear_test_rate_limit_keys(client_ip))
