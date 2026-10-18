"""Read-only shared-cache health; never loads API credentials or fetches ads."""
import asyncio
import json

from app.core.tajikistan import PARSER_ID
from app.db.session import AsyncSessionLocal, engine
from app.services.rate_registry import parser_state


async def health():
    try:
        async with AsyncSessionLocal() as db:
            state = await parser_state(db, PARSER_ID, refresh=False, persist=False)
            print(json.dumps({key: state.get(key) for key in (
                'available', 'status', 'last_error', 'last_success_at', 'age_seconds')}))
            return 0 if state['available'] else 1
    except Exception as exc:
        print(json.dumps({'available': False, 'error': type(exc).__name__}))
        return 1
    finally:
        await engine.dispose()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(health()))
