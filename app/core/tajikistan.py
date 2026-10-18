"""TJ cabinet policy. No bank/provider identifiers are inferred from names."""
from decimal import Decimal, InvalidOperation

COUNTRY = 'TJ'
CURRENCY = 'TJS'
DIRECTION = 'tj-tj'
PARSER_ID = 'bybit_p2p_tjs_green_avg3'
MIN_AMOUNT = Decimal('10.00')
PAYMENT_SYSTEMS = {'korti_milli': 'Корти Милли'}
BLOCK_REASON = 'Приём TJ → TJ закрыт до отдельной финансовой приёмки и разрешения владельца.'
QR_BLOCK_REASON = 'QR для TJ недоступен до утверждения формата и процедуры обработки.'


def admission_status():
    # Deliberately has no environment/UI override. Production requires a reviewed release.
    return {'country': COUNTRY, 'currency': CURRENCY, 'direction': DIRECTION,
            'method': 'c2c', 'minimum_amount': str(MIN_AMOUNT), 'enabled': False,
            'code': 'tj_contract_unconfirmed', 'reason': BLOCK_REASON,
            'parser_id': PARSER_ID, 'qr_enabled': False, 'settlement_enabled': False}


def validate_amount(amount):
    try:
        value = Decimal(str(amount))
        valid = value.is_finite() and value >= MIN_AMOUNT and value == value.quantize(Decimal('.01'))
    except (InvalidOperation, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError('Минимальная сумма — 10 TJS; точность до двух знаков.')
    return value
