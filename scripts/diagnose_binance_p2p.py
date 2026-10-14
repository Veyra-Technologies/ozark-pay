"""Safe live diagnostic for the public Binance P2P market response."""
import asyncio
import json

from app.services.binance_p2p import _fetch_quote


async def main():
    quote = await _fetch_quote()
    print(json.dumps({
        'source': quote['source'],
        'parser_id': quote['parser_id'],
        'asset': quote['asset'],
        'fiat': quote['fiat'],
        'trade_type': quote['trade_type'],
        'payment_method': quote['payment_method'],
        'amount_filter': quote['amount_filter'],
        'rate': quote['rate'],
        'unit': quote['unit'],
        'ad_count': quote['ad_count'],
        'ads': [{'id': row['id'], 'price': row['price'],
                 'min_amount': row['min_amount'], 'max_amount': row['max_amount'],
                 'surplus_usdt': row['surplus_usdt']}
                for row in quote['ads']],
        'provider_timestamp': quote['provider_timestamp'],
        'fetched_at': quote['fetched_at'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
