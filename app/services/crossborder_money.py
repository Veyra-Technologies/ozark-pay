"""One rounding policy for KZT/GEL inputs and USDT accounting."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from app.services.trader_finance import LOCAL_QUANTUM, USDT_QUANTUM


class CrossBorderError(ValueError):
    code = 'crossborder_validation'


def number(value, *, maximum=Decimal('999999999999.99')) -> Decimal:
    try:
        if isinstance(value, (float, bool)):
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > maximum:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CrossBorderError('Некорректное десятичное число') from exc


def local_amount(value) -> Decimal:
    amount = number(value)
    if amount <= 0 or amount != amount.quantize(LOCAL_QUANTUM):
        raise CrossBorderError('Сумма должна быть положительной, не более двух знаков после запятой')
    return amount.quantize(LOCAL_QUANTUM)


def usdt(value) -> Decimal:
    return number(value).quantize(USDT_QUANTUM, rounding=ROUND_HALF_UP)


def percent(value) -> Decimal:
    rate = number(value)
    if not 0 <= rate <= 100 or rate != rate.quantize(Decimal('.0001')):
        raise CrossBorderError('Процент должен быть от 0 до 100, точность до 4 знаков')
    return rate


def base_amount(amount_kzt, rate) -> Decimal:
    rate = number(rate)
    if rate <= 0:
        raise CrossBorderError('Курс должен быть положительным')
    with localcontext() as context:
        context.prec = 50
        result = usdt(local_amount(amount_kzt) / rate)
    if result <= 0:
        raise CrossBorderError('Сумма ниже точности расчётов USDT')
    return result


def after_percent(base, rate) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return usdt(usdt(base) * (Decimal(1) - percent(rate) / Decimal(100)))


def result(base, merchant_percent, trader_percent, expenses=0) -> dict:
    base = usdt(base)
    merchant = after_percent(base, merchant_percent)
    trader = after_percent(base, trader_percent)
    expenses = usdt(expenses)
    if expenses < 0:
        raise CrossBorderError('Расходы не могут быть отрицательными')
    return {key: str(value) for key, value in {
        'base_usdt': base, 'merchant_usdt': merchant, 'trader_usdt': trader,
        'merchant_fee_usdt': base - merchant, 'trader_reward_usdt': base - trader,
        'margin_usdt': trader - merchant, 'expenses_usdt': expenses,
        'net_margin_usdt': trader - merchant - expenses,
    }.items()}
