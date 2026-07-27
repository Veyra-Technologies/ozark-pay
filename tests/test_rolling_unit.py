import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.services.deposit_ttl import (
    deposit_deadline,
    deposit_is_expired,
    deposit_remaining_seconds,
    new_deposit_expires_at,
)
from app.services.rapira import (
    RollingRapiraQuote,
    RollingRateUnavailable,
    parse_strict_rolling_ask_quote,
)
from app.services.rolling import money, percentage, rolling_rate, usdt


ROOT = Path(__file__).resolve().parents[1]


def test_strict_rapira_accepts_direct_ask_only():
    now = datetime.now(timezone.utc)
    quote = parse_strict_rolling_ask_quote(
        {
            'data': [{
                'symbol': 'USDT/RUB',
                'askPrice': '100.12345678',
                'bidPrice': '99',
                'last': '101',
                'updatedAt': now.isoformat(),
            }]
        },
        received_at=now,
    )
    assert quote is not None
    assert quote.rate == Decimal('100.12345678')
    assert quote.side == 'ask'
    assert quote.source == 'Rapira'
    assert quote.provider_field == 'askPrice'
    assert quote.stale is False


def test_strict_rapira_correctly_inverts_reverse_ask():
    now = datetime.now(timezone.utc)
    quote = parse_strict_rolling_ask_quote({
        'data': [{
            'baseCurrency': 'RUB',
            'quoteCurrency': 'USDT',
            'askPrice': '0.01',
            'timestamp': now.isoformat(),
        }]
    }, received_at=now)
    assert quote is not None
    assert quote.rate == Decimal('100')
    assert quote.side == 'ask'


@pytest.mark.parametrize('field', ['bidPrice', 'last', 'close', 'price', 'rate'])
def test_strict_rapira_rejects_non_ask_fields(field):
    now = datetime.now(timezone.utc)
    assert parse_strict_rolling_ask_quote({
        'data': [{
            'symbol': 'USDT/RUB',
            field: '100',
            'timestamp': now.isoformat(),
        }]
    }, received_at=now) is None


def test_strict_rapira_rejects_unrelated_pair():
    now = datetime.now(timezone.utc)
    assert parse_strict_rolling_ask_quote({
        'data': [{
            'symbol': 'BTC/RUB',
            'askPrice': '100',
            'timestamp': now.isoformat(),
        }]
    }, received_at=now) is None


@pytest.mark.parametrize(
    'ask_price',
    [None, 0, '0', -1, '-1', 'not-a-number'],
)
def test_strict_rapira_rejects_missing_or_nonpositive_ask(ask_price):
    now = datetime.now(timezone.utc)
    assert parse_strict_rolling_ask_quote({
        'data': [{
            'symbol': 'USDT/RUB',
            'askPrice': ask_price,
            'timestamp': now.isoformat(),
        }]
    }, received_at=now) is None


def test_strict_rapira_accepts_numeric_json_and_later_valid_market_entry():
    now = datetime.now(timezone.utc)
    quote = parse_strict_rolling_ask_quote({
        'data': [
            {
                'symbol': 'BTC/RUB',
                'askPrice': 999,
                'timestamp': now.isoformat(),
            },
            {
                'symbol': 'USDT/RUB',
                'askPrice': 100.125,
                'timestamp': now.isoformat(),
            },
        ]
    }, received_at=now)
    assert quote is not None
    assert quote.rate == Decimal('100.125')


@pytest.mark.parametrize(
    'timestamp',
    [
        None,
        '2026-01-01T12:00:00',
        'not-a-timestamp',
    ],
)
def test_strict_rapira_rejects_missing_invalid_or_timezone_less_timestamp(
    timestamp,
):
    received_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_strict_rolling_ask_quote({
        'data': [{
            'symbol': 'USDT/RUB',
            'askPrice': '100',
            'timestamp': timestamp,
        }]
    }, received_at=received_at) is None


def test_strict_rapira_rejects_stale_and_future_provider_timestamp():
    received_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    for timestamp in (
        received_at - timedelta(hours=1),
        received_at + timedelta(minutes=1),
    ):
        assert parse_strict_rolling_ask_quote({
            'data': [{
                'symbol': 'USDT/RUB',
                'askPrice': '100',
                'timestamp': timestamp.isoformat(),
            }]
        }, received_at=received_at) is None


def test_financial_quantization_is_decimal_half_up():
    assert money('1.005') == Decimal('1.01')
    assert usdt('1.0000005') == Decimal('1.000001')
    assert rolling_rate('100.123456789') == Decimal('100.12345679')
    assert percentage('10.1234567') == Decimal('10.123457')


def test_ttl_uses_immutable_expires_at():
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expires = created + timedelta(seconds=123)
    deposit = SimpleNamespace(created_at=created, expires_at=expires)
    assert deposit_deadline(deposit) == expires
    assert deposit_remaining_seconds(
        deposit,
        now=created + timedelta(seconds=23),
    ) == 100
    assert deposit_is_expired(deposit, now=expires)


def test_new_deposit_ttl_uses_nondefault_application_setting(monkeypatch):
    import app.services.deposit_ttl as ttl

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(ttl.settings, 'DEPOSIT_PROCESSING_TTL_SECONDS', 600)
    assert new_deposit_expires_at(now) == now + timedelta(seconds=600)


def test_trader_templates_do_not_render_decline_without_admin_guard():
    rows = (ROOT / 'app/templates/_deposit_rows.html').read_text(encoding='utf-8')
    cabinet = (ROOT / 'app/templates/cabinet.html').read_text(encoding='utf-8')
    for template in (rows, cabinet):
        decline = template.index('/decline')
        guard = template.rfind(
            "{% if role in ['admin','superadmin'] %}",
            0,
            decline,
        )
        assert guard >= 0
        assert template.find('{% endif %}', decline) >= 0


def test_decline_endpoint_has_server_side_role_check_and_required_reason():
    routes = (ROOT / 'app/web/routes.py').read_text(encoding='utf-8')
    start = routes.index('async def cabinet_decline_deposit')
    block = routes[start:routes.index(
        "@router.get('/cabinet/appeals/files",
        start,
    )]
    assert "reason: str = Form('')" in block
    assert 'Role.superadmin.value, Role.admin.value' in block
    assert 'HTTPException(403' in block
    assert 'user.role == Role.superadmin.value and not reason' in block
    assert 'finalize_unsuccessful_deposit' in block


def test_migrations_backfill_postpaid_and_legacy_ttl():
    migration = (
        ROOT / 'alembic/versions/0014_deposit_expiry_finance_profile.py'
    ).read_text(encoding='utf-8')
    assert "created_at + interval '15 minutes'" in migration
    assert "'postpaid'" in migration
    assert 'nullable=False' in migration
    assert 'LIMIT {_EXPIRES_BACKFILL_BATCH_SIZE}' in migration
    assert 'CREATE INDEX CONCURRENTLY' in migration

    rolling_migration = (
        ROOT / 'alembic/versions/0015_merchant_rolling.py'
    ).read_text(encoding='utf-8')
    assert 'refusing destructive Rolling downgrade after funding or traffic' in (
        rolling_migration
    )


def test_balance_api_keeps_legacy_fields_and_adds_rolling():
    merchant_api = (ROOT / 'app/api/v1/merchant.py').read_text(encoding='utf-8')
    start = merchant_api.index("async def balance(")
    block = merchant_api[start:merchant_api.index(
        "@router.get('/statistics')",
        start,
    )]
    for field in ("'available'", "'frozen'", "'currency'"):
        assert field in block
    assert "'settlement_mode'" in block
    assert "'rolling'" in block
    assert "Balance.currency=='RUB'" in block


def test_deposit_timer_has_no_hard_coded_fifteen_minutes():
    cabinet = (ROOT / 'app/templates/cabinet.html').read_text(encoding='utf-8')
    rows = (ROOT / 'app/templates/_deposit_rows.html').read_text(encoding='utf-8')
    assert '>15:00<' not in cabinet
    assert '>15:00<' not in rows


def test_application_imports_openapi_and_templates_compile():
    from app.main import app
    from app.web.routes import templates

    schema = app.openapi()
    assert schema['openapi'] == '3.1.0'
    assert schema['paths']

    template_root = ROOT / 'app/templates'
    names = [
        path.relative_to(template_root).as_posix()
        for path in template_root.rglob('*.html')
    ]
    assert names
    for name in names:
        templates.env.get_template(name)


def test_strict_rolling_rate_rejects_stale_cache_and_configured_fallback(
    monkeypatch,
):
    import app.services.rapira as rapira

    old = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(
        rapira,
        '_strict_rolling_cache',
        RollingRapiraQuote(
            symbol='USDT/RUB',
            rate=Decimal('100'),
            side='ask',
            source='Rapira',
            received_at=old,
            market_updated_at=old,
            stale=False,
            provider_field='askPrice',
        ),
    )
    monkeypatch.setattr(rapira.settings, 'RAPIRA_RATES_ENABLED', False)
    monkeypatch.setattr(rapira.settings, 'RAPIRA_RATES_URL', '')
    monkeypatch.setattr(
        rapira.settings,
        'SETTLEMENT_USDT_RUB_RATE',
        Decimal('777'),
    )
    with pytest.raises(RollingRateUnavailable):
        asyncio.run(rapira.get_strict_rolling_ask_quote())


def test_strict_rolling_rate_rejects_malformed_provider_json(monkeypatch):
    import app.services.rapira as rapira

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError('malformed JSON')

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get(self, url, headers):
            return FakeResponse()

    monkeypatch.setattr(rapira, '_strict_rolling_cache', None)
    monkeypatch.setattr(rapira.settings, 'RAPIRA_RATES_ENABLED', True)
    monkeypatch.setattr(
        rapira.settings,
        'RAPIRA_RATES_URL',
        'https://provider.invalid/read-only',
    )
    monkeypatch.setattr(
        rapira.httpx,
        'AsyncClient',
        lambda **kwargs: FakeClient(),
    )
    with pytest.raises(RollingRateUnavailable):
        asyncio.run(rapira.get_strict_rolling_ask_quote())


def test_direct_decline_request_is_forbidden_for_trader(monkeypatch):
    import app.web.routes as web_routes

    async def trader_user(request, db):
        return SimpleNamespace(
            id='00000000-0000-0000-0000-000000000001',
            role='trader',
            twofa_enabled=True,
        )

    monkeypatch.setattr(web_routes, 'get_current_web_user', trader_user)
    request = Request({
        'type': 'http',
        'method': 'POST',
        'path': '/cabinet/deposits/test/decline',
        'headers': [],
        'query_string': b'',
        'scheme': 'http',
        'server': ('testserver', 80),
        'client': ('127.0.0.1', 12345),
    })
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(web_routes.cabinet_decline_deposit(
            '00000000-0000-0000-0000-000000000002',
            request,
            reason='not allowed',
            db=None,
        ))
    assert exc_info.value.status_code == 403


def test_rolling_funding_form_requires_superadmin_2fa_and_audit():
    routes = (ROOT / 'app/web/routes.py').read_text(encoding='utf-8')
    helper_start = routes.index('async def _require_superadmin_web')
    helper = routes[helper_start:routes.index(
        "@router.post('/cabinet/antiscam/settings')",
        helper_start,
    )]
    assert 'Role.superadmin.value' in helper
    assert 'staff_2fa_setup_required(actor)' in helper

    route_start = routes.index('async def cabinet_rolling_funding')
    route = routes[route_start:routes.index(
        "@router.post('/cabinet/rolling/merchants/{merchant_id}/suspension')",
        route_start,
    )]
    assert '_require_superadmin_web(request, db)' in route
    assert "reason: str = Form(...)" in route
    assert "'rolling_funding_recorded'" in route


def test_superadmin_without_2fa_is_redirected_before_funding(monkeypatch):
    import app.web.routes as web_routes

    async def superadmin_without_2fa(request, db):
        return SimpleNamespace(role='superadmin', twofa_enabled=False)

    monkeypatch.setattr(
        web_routes,
        'get_current_web_user',
        superadmin_without_2fa,
    )
    request = Request({
        'type': 'http',
        'method': 'POST',
        'path': '/cabinet/rolling/merchants/test/funding',
        'headers': [],
        'query_string': b'',
        'scheme': 'http',
        'server': ('testserver', 80),
        'client': ('127.0.0.1', 12345),
    })
    response = asyncio.run(web_routes._require_superadmin_web(request, None))
    assert response.status_code == 303
    assert response.headers['location'].startswith('/cabinet')


def test_balance_api_serializes_all_decimal_rolling_fields_as_strings():
    merchant_api = (ROOT / 'app/api/v1/merchant.py').read_text(encoding='utf-8')
    start = merchant_api.index("async def balance(")
    block = merchant_api[start:merchant_api.index(
        "@router.get('/statistics')",
        start,
    )]
    assert "str(value) if isinstance(value, Decimal)" in block
