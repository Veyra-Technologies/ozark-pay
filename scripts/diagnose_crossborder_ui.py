"""Read-only authenticated smoke check for the local cross-border cabinet."""
import re
from pathlib import Path

import httpx
import pyotp
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def main():
    env = dotenv_values(ROOT / '.env.docker.local')
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=30,
                      follow_redirects=False, trust_env=False,
                      headers={'X-Real-IP': '2001:db8:11::9'}) as client:
        login = client.get('/staff/login')
        login.raise_for_status()
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login.text)
        if not csrf:
            raise RuntimeError('login CSRF token missing')
        response = client.post('/staff/login', data={
            'email': env['SUPERADMIN_EMAIL'],
            'password': env['SUPERADMIN_PASSWORD'],
            'otp': pyotp.TOTP(env['SUPERADMIN_2FA_SECRET']).now(),
            'csrf_token': csrf.group(1),
        })
        if response.status_code != 303:
            raise RuntimeError(f'local Superadmin login failed: HTTP {response.status_code}')
        cabinet = client.get('/staff/cabinet')
        cabinet.raise_for_status()
        required = (
            'data-merchant-parser-setting', 'Страна получателя',
            'data-parser-country', 'data-parser-select',
            'Binance P2P · Kaspi Bank', 'Курс стороны мерчанта · KZ-GE',
            'Источник GEL остаётся XE',
        )
        missing = [marker for marker in required if marker not in cabinet.text]
        if missing:
            raise RuntimeError(f'cross-border cabinet markers missing: {missing}')
        state = client.get('/staff/cabinet/crossborder/binance-rate')
        state.raise_for_status()
        payload = state.json()
        if (not payload.get('available') or payload.get('status') != 'current'
                or payload.get('quote', {}).get('ad_count') != 10):
            raise RuntimeError(f'Binance cabinet state is not current: {payload.get("code")}')
        script = client.get('/static/crossborder.js?v=20261011-binance')
        script.raise_for_status()
        for marker in ("country.value !== 'GE'", "direction.value = 'kz-ge'",
                       'binance_p2p_kaspi_buy_100000_avg10'):
            if marker not in script.text:
                raise RuntimeError(f'country/parser binding missing: {marker}')
        print('PASS authenticated cross-border cabinet and Binance state')
        print(f"PASS status={payload['status']} ads={payload['quote']['ad_count']} "
              f"rate={payload['quote']['rate']} fetched_at={payload['quote']['fetched_at']}")


if __name__ == '__main__':
    main()
