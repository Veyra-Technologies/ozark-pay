"""Read-only infrastructure diagnostic. Credentials arrive through stdin only.

Does not enable a parser, place orders, modify account permissions or activate TJ.
The Windows launcher decrypts the current user's DPAPI vault into this process.
"""
import asyncio
import json
import sys
from decimal import Decimal, localcontext

from app.services.bybit_p2p import BybitUnavailable, readonly_request


async def main():
    credentials = json.load(sys.stdin)
    key, secret = credentials['api_key'], credentials['api_secret']
    permissions = await readonly_request(key, secret)
    account = permissions.get('result') or {}
    print(json.dumps({'authentication': 'PASS', 'read_only': account.get('readOnly'),
        'p2p_permissions': (account.get('permissions') or {}).get('FiatP2P', [])}), flush=True)
    for side in ('0', '1'):
        await asyncio.sleep(1.2)
        try:
            payload = await readonly_request(key, secret, side=side)
            ads = (payload.get('result') or {}).get('items') or []
            top = [{field: ad.get(field) for field in ('id', 'side', 'price', 'tokenId', 'currencyId', 'nickName')}
                for ad in ads[:3]]
            with localcontext() as context:
                context.prec = 50
                average = str(sum(Decimal(ad['price']) for ad in top) / Decimal(3)) if len(top) == 3 else None
            print(json.dumps({'request_side': side, 'ads': top, 'decimal_avg3': average,
                'unit': 'TJS per 1 USDT', 'green_order_verification': 'required', 'financial_admission': False}), flush=True)
        except BybitUnavailable as exc:
            print(json.dumps({'request_side': side, 'status': 'BLOCKED', 'code': str(exc)}), flush=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (BybitUnavailable, KeyError, ValueError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'bybit_access_verification_failed'}))
        raise SystemExit(1) from None
