import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.services.rapira import (
    RapiraRateQuote,
    RollingRapiraQuote,
    parse_rapira_market_payload,
    parse_rapira_rub_usdt_rate,
    parse_strict_rolling_ask_quote,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / 'tests/fixtures/rapira_rub_usdt_response.json'


def load_redacted_rapira_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))


def test_redacted_actual_payload_preserves_legacy_ask_and_return_type():
    payload = load_redacted_rapira_payload()
    rows = parse_rapira_market_payload(payload)

    assert isinstance(payload, dict)
    assert isinstance(payload['data'], list)
    assert len(rows) == 1
    assert rows[0]['symbol'] == 'USDT/RUB'
    assert isinstance(rows[0]['askPrice'], float)

    parsed = parse_rapira_rub_usdt_rate(payload)
    assert isinstance(parsed, Decimal)
    assert parsed == Decimal('81.42')


def test_canonical_symbol_wins_over_opposite_provider_base_quote_metadata():
    payload = load_redacted_rapira_payload()
    row = payload['data'][0]

    assert row['symbol'] == 'USDT/RUB'
    assert row['baseCurrency'] == 'RUB'
    assert row['quoteCurrency'] == 'USDT'
    assert parse_rapira_rub_usdt_rate(payload) == Decimal('81.42')


def test_general_parser_keeps_legacy_price_priority():
    payload = {
        'data': [{
            'symbol': 'USDT/RUB',
            'askPrice': '101.1',
            'last': '102.2',
            'close': '103.3',
            'bidPrice': '100.0',
            'price': '104.4',
            'rate': '105.5',
        }]
    }

    assert parse_rapira_rub_usdt_rate(payload) == Decimal('101.1')


def test_general_parser_inverts_only_explicit_rub_usdt_symbol():
    assert parse_rapira_rub_usdt_rate({
        'data': [{
            'symbol': 'RUB/USDT',
            'askPrice': '0.0125',
        }]
    }) == Decimal('8E+1')


@pytest.mark.parametrize('ask_price', ['81.42', 81.42])
def test_general_parser_accepts_numeric_string_and_json_number(ask_price):
    parsed = parse_rapira_rub_usdt_rate({
        'data': [{
            'symbol': 'USDT/RUB',
            'askPrice': ask_price,
        }]
    })

    assert parsed == Decimal('81.42')


@pytest.mark.parametrize(
    'payload',
    [
        None,
        {},
        {'data': 'not-a-list'},
        {'data': [None, [], 'invalid']},
        {'data': [{'symbol': 'USDT/RUB', 'askPrice': 'not-a-number'}]},
        {'data': [{'symbol': 'USDT/RUB', 'askPrice': 'NaN'}]},
        {'data': [{'symbol': 'USDT/RUB', 'askPrice': 'Infinity'}]},
    ],
)
def test_general_parser_handles_malformed_payload_stably(payload):
    assert parse_rapira_rub_usdt_rate(payload) is None


def test_actual_payload_is_accepted_by_general_but_not_timestamp_strict_flow():
    payload = load_redacted_rapira_payload()
    now = datetime.now(timezone.utc)

    assert parse_rapira_rub_usdt_rate(payload) == Decimal('81.42')
    assert parse_strict_rolling_ask_quote(payload, received_at=now) is None


@pytest.mark.parametrize(
    'market_timestamp',
    [
        datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
    ],
)
def test_stale_and_future_timestamp_are_rejected_only_by_strict_rolling(
    market_timestamp,
):
    received_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        'data': [{
            'symbol': 'USDT/RUB',
            'askPrice': '81.42',
            'timestamp': market_timestamp.isoformat(),
        }]
    }

    assert parse_rapira_rub_usdt_rate(payload) == Decimal('81.42')
    assert parse_strict_rolling_ask_quote(
        payload,
        received_at=received_at,
    ) is None


def test_strict_rolling_uses_fresh_ask_and_returns_extended_quote():
    received_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    payload = load_redacted_rapira_payload()
    payload['data'][0]['timestamp'] = received_at.isoformat()

    quote = parse_strict_rolling_ask_quote(
        payload,
        received_at=received_at,
    )

    assert isinstance(quote, RollingRapiraQuote)
    assert quote.rate == Decimal('81.42')
    assert quote.side == 'ask'
    assert quote.provider_field == 'askPrice'
    assert quote.market_updated_at == received_at


def test_general_public_quote_keeps_origin_main_object_contract(monkeypatch):
    import app.services.rapira as rapira

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(rapira, '_cache_value', Decimal('81.42'))
    monkeypatch.setattr(
        rapira,
        '_cache_expires_at',
        now + timedelta(minutes=1),
    )
    monkeypatch.setattr(rapira, '_last_success_value', Decimal('81.42'))
    monkeypatch.setattr(rapira, '_last_success_at', now)

    quote = asyncio.run(rapira.get_rapira_rub_usdt_quote())

    assert isinstance(quote, RapiraRateQuote)
    assert quote.rate_rub == Decimal('81.42')
    assert quote.source == 'Rapira'
    assert quote.stale is False


def test_general_cache_does_not_depend_on_strict_rolling_cache(monkeypatch):
    import app.services.rapira as rapira

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(rapira, '_cache_value', Decimal('81.42'))
    monkeypatch.setattr(
        rapira,
        '_cache_expires_at',
        now + timedelta(minutes=1),
    )
    monkeypatch.setattr(rapira, '_last_success_at', now)
    monkeypatch.setattr(
        rapira,
        '_strict_rolling_cache',
        RollingRapiraQuote(
            symbol='USDT/RUB',
            rate=Decimal('999'),
            side='ask',
            source='Rapira',
            received_at=now - timedelta(hours=1),
            market_updated_at=now - timedelta(hours=1),
            stale=False,
            provider_field='askPrice',
        ),
    )

    quote = asyncio.run(rapira.get_rapira_rub_usdt_quote())

    assert quote.rate_rub == Decimal('81.42')
    assert quote.stale is False


def test_existing_ui_rate_route_uses_general_quote_object(monkeypatch):
    import app.web.routes as web_routes

    now = datetime.now(timezone.utc)

    async def unchanged_session(request):
        return False

    async def current_user(request, db):
        return SimpleNamespace(role='superadmin', twofa_enabled=True)

    async def general_quote():
        return RapiraRateQuote(
            rate_rub=Decimal('81.42'),
            source='Rapira',
            updated_at=now,
            stale=False,
            cache_seconds=15,
        )

    monkeypatch.setattr(
        web_routes,
        '_session_identity_changed',
        unchanged_session,
    )
    monkeypatch.setattr(web_routes, 'get_current_web_user', current_user)
    monkeypatch.setattr(
        web_routes,
        'get_rapira_rub_usdt_quote',
        general_quote,
    )
    request = Request({
        'type': 'http',
        'method': 'GET',
        'path': '/staff/cabinet/rates/usdt-rub',
        'headers': [],
        'query_string': b'',
        'scheme': 'http',
        'server': ('testserver', 80),
        'client': ('127.0.0.1', 12345),
    })

    response = asyncio.run(web_routes.cabinet_usdt_rub_rate(request, None))

    assert response['rate_rub'] == '81.42'
    assert response['rate_source'] == 'Rapira'
    assert response['stale'] is False


def test_existing_settlement_rate_keeps_general_quote_contract(monkeypatch):
    import app.services.settlements as settlements

    async def general_quote():
        return RapiraRateQuote(
            rate_rub=Decimal('81.42'),
            source='Rapira',
            updated_at=datetime.now(timezone.utc),
            stale=False,
            cache_seconds=15,
        )

    monkeypatch.setattr(
        settlements,
        'get_rapira_rub_usdt_quote',
        general_quote,
    )

    rate_rub, source = asyncio.run(settlements.settlement_rate_rub())

    assert rate_rub == Decimal('81.4200')
    assert source == 'Rapira'


def test_postpaid_path_never_calls_strict_rolling_quote(monkeypatch):
    import app.services.rolling as rolling

    async def postpaid_mode(db, merchant_id):
        return 'postpaid'

    async def forbidden_strict_quote():
        raise AssertionError('postpaid must not call strict Rapira')

    monkeypatch.setattr(rolling, 'merchant_settlement_mode', postpaid_mode)
    monkeypatch.setattr(
        rolling,
        'get_strict_rolling_ask_quote',
        forbidden_strict_quote,
    )

    quote = asyncio.run(
        rolling.rolling_quote_for_deposit_create(None, uuid4())
    )

    assert quote is None


def test_rolling_path_calls_only_strict_ask_quote(monkeypatch):
    import app.services.rolling as rolling

    marker = object()
    calls = []

    async def rolling_mode(db, merchant_id):
        return 'rolling'

    async def no_account(db, merchant_id):
        return None

    async def strict_quote():
        calls.append('strict')
        return marker

    monkeypatch.setattr(rolling, 'merchant_settlement_mode', rolling_mode)
    monkeypatch.setattr(rolling, 'get_rolling_account', no_account)
    monkeypatch.setattr(
        rolling,
        'get_strict_rolling_ask_quote',
        strict_quote,
    )

    quote = asyncio.run(
        rolling.rolling_quote_for_deposit_create(None, uuid4())
    )

    assert quote is marker
    assert calls == ['strict']
