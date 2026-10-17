"""Tariff routes are distinct from the card/phone requisite transport."""
from fastapi import HTTPException

from app.core.payment_methods import PAYMENT_METHOD_LABELS

KZ_GE_METHOD = 'kz-ge'
TRADER_TARIFF_METHODS = {
    'TJ': [{'value': 'c2c', 'label': 'Таджикистан — Локальный (TJ → TJ) · C2C'}],
    'RU': [{'value': method, 'label': PAYMENT_METHOD_LABELS[method]}
           for method in ('sbp', 'c2c', 'mobile_commerce', 'sbp_qr')],
    'GE': [{'value': KZ_GE_METHOD, 'label': 'Трансгран KZ → GE'}],
    'AM': [],
}


def validate_trader_tariff_method(role, country, method):
    # Older clients omit this field. Country still determines the only legal catalog.
    if not method:
        return
    if role != 'trader' or method not in {item['value'] for item in TRADER_TARIFF_METHODS.get(country, [])}:
        raise HTTPException(422, 'Тарифный метод не соответствует роли и стране кабинета')
