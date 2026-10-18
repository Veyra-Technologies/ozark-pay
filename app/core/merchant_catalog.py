"""Country policy shared by cabinet forms, APIs and settlement admission."""
MERCHANT_COUNTRIES = {
    'RU': {'label': 'Россия', 'currency': 'RUB', 'parsers': {'rapira': 'Rapira · USDT/RUB · askPrice'},
           'methods': {'sbp': 'СБП', 'c2c': 'Карта', 'mobile_commerce': 'Мобильная коммерция', 'sbp_qr': 'СБП QR'}},
    'KZ': {'label': 'Казахстан', 'currency': 'KZT',
           'parsers': {'binance_p2p_kaspi_buy_100000_avg10': 'Binance · KZT/USDT · Kaspi · 100 000 KZT · среднее 10'},
           'methods': {'kz-ge': 'KZ → GE · Kaspi'}},
    'TJ': {'label': 'Таджикистан', 'currency': 'TJS',
           'parsers': {'bybit_p2p_tjs_green_avg3': 'Bybit P2P Green 1–3 · источник требует подтверждения'},
           'methods': {'c2c': 'Таджикистан — Локальный (TJ → TJ) · C2C'}},
}
