"""Read-only quote presentation for the assigned receiving profile."""
from app.services import xe
from app.services.rapira import get_strict_rolling_ask_quote, RollingRateUnavailable
from app.services.trader_work import RATE_SOURCES


async def profile_quote(account, *, db):
    currency = account.working_currency if account else None
    source = account.working_rate_source if account else None
    result = {'available': False, 'source': source, 'currency': currency,
              'rate': None, 'provider_timestamp': None, 'fetched_at': None,
              'rate_type': 'mid-market' if source == 'xe' else 'ask',
              'reason': 'Рабочий профиль не назначен', 'code': 'profile_unassigned'}
    if account and account.role == 'trader' and account.working_country == 'GE':
        from app.services.binance_p2p import GEL_PARSER_ID
        from app.services.parser_assignments import require_assignment
        from app.services.rate_registry import parser_state
        from app.services.crossborder_money import CrossBorderError
        result.update(source='binance_tbc', rate_type='informational',
                      reason='Информационный курс GEL/TBC недоступен', code='gel_rate_unavailable')
        try:
            await require_assignment(db, trader_id=account.id, parser_id=GEL_PARSER_ID, purpose='informational')
        except CrossBorderError:
            return result
        state = await parser_state(db, GEL_PARSER_ID, refresh=False, persist=False)
        if state['available']:
            quote = state['quote']
            result.update(available=True, rate=quote['rate'], fetched_at=quote['fetched_at'], code=None,
                          reason='Binance GEL/TBC · информационный ориентир', parser_id=GEL_PARSER_ID)
        return result
    if not source or source != RATE_SOURCES.get(account.working_country):
        return result
    if source == 'xe':
        state = await xe.get_state()
        result.update(available=state['available'], reason=state['reason'], code=state['code'])
        bundle = state.get('bundle')
        if bundle:
            result.update(rate=bundle['rates'].get(currency), provider_timestamp=bundle['provider_timestamp'], fetched_at=bundle['fetched_at'])
    else:
        try:
            quote = await get_strict_rolling_ask_quote()
            result.update(available=True, code=None, reason='Свежая котировка Rapira', rate=str(quote.rate),
                provider_timestamp=quote.provider_timestamp.isoformat() if quote.provider_timestamp else None,
                fetched_at=quote.fetched_at.isoformat())
        except RollingRateUnavailable:
            result.update(reason='Нет свежей котировки Rapira', code='rapira_unavailable')
    return result
