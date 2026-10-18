"""Dedicated read-only quote process; secrets arrive via stdin, never Docker env."""
import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone

from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import settings
from app.core.tajikistan import PARSER_ID
from app.db.session import AsyncSessionLocal, engine
from app.models import User
from app.services import bybit_p2p, rate_registry


async def run(*, activate):
    pair = json.loads(sys.stdin.readline())
    settings.BYBIT_P2P_API_KEY = SecretStr(pair.pop('api_key'))
    settings.BYBIT_P2P_API_SECRET = SecretStr(pair.pop('api_secret'))
    settings.BYBIT_P2P_FETCH_IN_PROCESS = True
    if not settings.BYBIT_P2P_ENABLED or not bybit_p2p.verified_contract():
        raise bybit_p2p.BybitUnavailable('bybit_verified_contract_required')
    access = (await bybit_p2p.readonly_request(settings.BYBIT_P2P_API_KEY.get_secret_value(),
        settings.BYBIT_P2P_API_SECRET.get_secret_value())).get('result') or {}
    if access.get('readOnly') != 1 or 'Advertising' not in (access.get('permissions') or {}).get('FiatP2P', []):
        raise bybit_p2p.BybitUnavailable('bybit_readonly_p2p_permission_required')
    if activate:
        async with AsyncSessionLocal() as db:
            config = await rate_registry.configuration(db, PARSER_ID)
            if not config.is_active:
                actor = await db.scalar(select(User).where(User.email == settings.SUPERADMIN_EMAIL))
                await rate_registry.configure(db, actor=actor, parser_id=PARSER_ID,
                    refresh_seconds=30, ttl_seconds=60, is_active=True)
                await db.commit()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    while not stop.is_set():
        delay = 30
        try:
            async with AsyncSessionLocal() as db:
                config = await rate_registry.configuration(db, PARSER_ID)
                delay = config.parameters['refresh_seconds']
                state = await rate_registry.parser_state(db, PARSER_ID, refresh=True)
                await db.commit()
                quote = state.get('quote') or {}
                print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'status': state['status'],
                    'available': state['available'], 'error': state.get('last_error'),
                    'ad_ids': quote.get('ad_ids'), 'prices': quote.get('raw_values'), 'rate': quote.get('rate'),
                    'unit': quote.get('unit'), 'fetched_at': quote.get('fetched_at'),
                    'expires_at': quote.get('expires_at'), 'last_success_at': state.get('last_success_at')}), flush=True)
        except Exception as exc:
            # Do not log exception text, request objects, headers or credentials.
            print(json.dumps({'status': 'unavailable', 'error': type(exc).__name__}), flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(15, min(int(delay), 120)))
        except TimeoutError:
            pass
    await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read-only Bybit quote worker; no payment admission.')
    parser.add_argument('--activate', action='store_true', help='Explicitly activate the approved quote configuration once.')
    try:
        asyncio.run(run(activate=parser.parse_args().activate))
    except Exception as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc) if isinstance(exc, bybit_p2p.BybitUnavailable) else type(exc).__name__}))
        raise SystemExit(1) from None
