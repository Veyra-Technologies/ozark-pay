"""Shared collateral arithmetic; ownership and journals stay in their own models."""
from decimal import Decimal, InvalidOperation


def collateral_delta(balance, hold, kind, amount):
    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount != amount.quantize(Decimal('.00000001')) or amount <= 0:
            raise ValueError('invalid collateral precision or amount')
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('invalid collateral amount') from exc
    if kind in ('hold', 'debit_available') and balance-hold < amount:
        raise ValueError('insufficient available USDT collateral')
    if kind in ('release', 'debit') and hold < amount:
        raise ValueError('insufficient USDT reserve')
    if kind == 'hold':
        hold += amount
    elif kind == 'release':
        hold -= amount
    elif kind == 'debit':
        balance -= amount
        hold -= amount
    elif kind == 'debit_available':
        balance -= amount
    elif kind == 'credit':
        balance += amount
    else:
        raise ValueError('invalid collateral movement')
    if balance < 0 or hold < 0 or hold > balance:
        raise ValueError('collateral invariant')
    return balance, hold
