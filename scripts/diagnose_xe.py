"""Server-side XE diagnostics; no credentials or raw provider bodies are printed."""
import asyncio
import json
from app.services.xe import BASE_URL, connection_status, get_state, refresh_seconds


async def main():
    configuration = connection_status()
    state = await get_state()
    bundle = state.get('bundle')
    print(json.dumps({
        'configuration': configuration,
        'available': state['available'], 'code': state['code'], 'reason': state['reason'],
        'base_url': BASE_URL, 'endpoints': ['currencies.json/?crypto=true',
            'convert_from.json/?from=USDT&to=KZT,AMD,GEL&amount=1&decimal_places=20&crypto=true',
            'convert_from.json/?from=USD&to=KZT,AMD,GEL,USDT&amount=1&decimal_places=20&crypto=true'],
        'verified_pairs': [f'USDT/{c}' for c in bundle['rates']] if bundle and state['available'] else [],
        'normalization': bundle.get('normalization') if bundle else None,
        'provider_timestamp': bundle.get('provider_timestamp') if bundle else None,
        'fetched_at': bundle.get('fetched_at') if bundle else None,
        'effective_refresh_seconds': refresh_seconds() if not configuration['code'] else None,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
