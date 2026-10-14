"""Server-authoritative merchant rate parser assignments."""
from sqlalchemy import select

from app.models import Merchant, MerchantCrossBorderRateSetting, User
from app.services.binance_p2p import PARSER_ID, PARSER_LABEL
from app.services.crossborder import DIRECTIONS


class RateSettingError(ValueError):
    def __init__(self, code: str, reason: str):
        self.code = code
        super().__init__(reason)


PARSER_OPTION = {
    'id': PARSER_ID,
    'label': PARSER_LABEL,
    'direction': 'kz-ge',
    'merchant_country': 'KZ',
    'destination_country': 'GE',
    'pay_currency': 'KZT',
    'rate_unit': 'KZT per 1 USDT',
}


def compatible_parsers(*, destination_country: str, direction: str | None = None,
                       pay_currency: str | None = None, role: str = 'merchant') -> list[dict]:
    if role != 'merchant':
        return []
    option = PARSER_OPTION
    if destination_country != option['destination_country']:
        return []
    if direction is not None and direction != option['direction']:
        return []
    if pay_currency is not None and pay_currency != option['pay_currency']:
        return []
    return [dict(option)]


async def get_setting(db, merchant_id, direction: str):
    return await db.scalar(select(MerchantCrossBorderRateSetting).where(
        MerchantCrossBorderRateSetting.merchant_id == merchant_id,
        MerchantCrossBorderRateSetting.direction == direction,
    ))


async def set_parser(db, *, actor, merchant_id, direction: str,
                     destination_country: str, parser_id: str):
    if actor.role != 'superadmin':
        raise RateSettingError('forbidden', 'Недостаточно прав для настройки парсера')
    direction_data = next((item for item in DIRECTIONS if item['id'] == direction), None)
    if not direction_data:
        raise RateSettingError('invalid_direction', 'Неизвестное направление')
    compatible = compatible_parsers(
        destination_country=destination_country,
        direction=direction,
        pay_currency=direction_data['pay_currency'],
    )
    if not compatible or parser_id not in {item['id'] for item in compatible}:
        raise RateSettingError(
            'incompatible_parser',
            'Выбранный парсер несовместим со страной, валютой или направлением',
        )
    merchant = await db.get(Merchant, merchant_id)
    owner = await db.get(User, merchant.owner_id) if merchant else None
    if (not merchant or merchant.is_archived or not owner or owner.role != 'merchant'
            or not owner.is_active or owner.is_locked or owner.is_archived):
        raise RateSettingError('merchant_unavailable', 'Мерчант недоступен')
    row = await get_setting(db, merchant.id, direction)
    if row is None:
        row = MerchantCrossBorderRateSetting(merchant_id=merchant.id, direction=direction)
        db.add(row)
    row.merchant_country = direction_data['merchant_country']
    row.destination_country = destination_country
    row.pay_currency = direction_data['pay_currency']
    row.parser_id = parser_id
    row.updated_by = actor.id
    await db.flush()
    return row


def public_setting(row) -> dict:
    return {
        'id': str(row.id),
        'merchant_id': str(row.merchant_id),
        'direction': row.direction,
        'merchant_country': row.merchant_country,
        'destination_country': row.destination_country,
        'pay_currency': row.pay_currency,
        'parser_id': row.parser_id,
        'parser_label': PARSER_LABEL if row.parser_id == PARSER_ID else row.parser_id,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }
