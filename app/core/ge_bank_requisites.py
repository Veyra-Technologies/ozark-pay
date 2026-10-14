"""GE bank identifier formats; NBG IBAN registry verified 2026-10-12.

Sources: https://nbg.gov.ge/en/payment-system/iban
https://nbg.gov.ge/en/payment-system/iban/validator
Card PANs use ISO 7812 lengths and the Luhn checksum; this is not issuer verification.
"""
import re

GE_REQUISITE_METHODS = ('c2c', 'bank_account')
GE_IBAN_BANK_CODES = {
    'ge-bank-of-georgia': 'BG', 'ge-basisbank': 'BS', 'ge-silk': 'BT',
    'ge-cartu': 'CR', 'ge-halyk': 'HB', 'ge-terabank': 'KS',
    'ge-liberty': 'LB', 'ge-procredit': 'PC', 'ge-tbc': 'TB',
    'ge-vtb': 'VT', 'ge-ziraat': 'ZB', 'ge-pasha': 'PB',
    'ge-isbank': 'IS', 'ge-credo': 'CD', 'ge-paysera': 'PS',
    'ge-hash': 'HS', 'ge-pave': 'PV', 'ge-micro-mbc': 'MB',
    'ge-micro-crystal': 'BC',
}


def luhn_valid(value: str) -> bool:
    digits = [int(char) for char in value]
    for index in range(len(digits) - 2, -1, -2):
        digit = digits[index] * 2
        digits[index] = digit - 9 if digit > 9 else digit
    return sum(digits) % 10 == 0


def normalize_ge_requisite(method: str, value: str, bank_id: str, last4: str | None = None) -> str:
    if not value or len(value) > 255:
        raise ValueError('Введите банковский реквизит')
    if method == 'c2c':
        clean = re.sub(r'[\s-]', '', value)
        if not re.fullmatch(r'[0-9]{13,19}', clean) or len(set(clean)) == 1 or not luhn_valid(clean):
            raise ValueError('Неверный номер банковской карты: нужны 13–19 цифр и корректная контрольная цифра')
    elif method == 'bank_account':
        clean = re.sub(r'\s', '', value).upper()
        if not re.fullmatch(r'GE[0-9]{2}[A-Z]{2}[0-9]{16}', clean):
            raise ValueError('Введите грузинский IBAN: GE, 2 контрольные цифры, код банка и 16 цифр счёта')
        expected_code = GE_IBAN_BANK_CODES.get(bank_id)
        if not expected_code or clean[4:6] != expected_code:
            raise ValueError('Код банка в IBAN не соответствует выбранному банку')
        rearranged = clean[4:] + clean[:4]
        numeric = ''.join(str(ord(char) - 55) if char.isalpha() else char for char in rearranged)
        if int(numeric) % 97 != 1:
            raise ValueError('Неверные контрольные цифры IBAN')
    else:
        raise ValueError('Для GE доступны Номер карты и Номер счёта')
    if last4 and last4.strip() != clean[-4:]:
        raise ValueError('Последние четыре цифры не совпадают с банковским реквизитом')
    return clean


def masked_requisite(last4: str | None) -> str:
    suffix = last4 if last4 and re.fullmatch(r'[0-9]{4}', last4) else '••••'
    return f'•••• {suffix}'


def operation_requisite_payload(req, value: str) -> dict:
    from app.core.payment_methods import payment_method_label
    return {'bank': req.bank_name, 'beneficiary': req.full_name or req.owner_name,
            'method': req.method, 'method_label': payment_method_label(req.method, req.country),
            'requisite': value, 'card': value if req.method == 'c2c' else None,
            'bank_account': value if req.method == 'bank_account' else None,
            'currency': req.currency, 'country': req.country}
