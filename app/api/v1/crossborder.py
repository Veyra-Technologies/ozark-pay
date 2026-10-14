from typing import Literal
from uuid import UUID
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import require_roles
from app.core.enums import Role
from app.db.session import get_db
from app.models import CrossBorderQuote
from app.services.crossborder_quote import create_preview, public_quote, CrossBorderUnavailable
from app.services.xe import XeUnavailable, get_state
from app.services.binance_p2p import BinanceP2PUnavailable, get_state as get_binance_state
from app.services.crossborder_rate_settings import set_parser, public_setting, RateSettingError
from app.services.fee_tiers import FeeConfigurationError
from app.services.audit import audit
from app.core.client_ip import client_ip

router = APIRouter(prefix='/admin/crossborder', tags=['crossborder'])


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    direction: Literal['kz-ge']
    merchant_id: UUID
    trader_id: UUID
    amount_kzt: Decimal = Field(gt=0, le=Decimal('999999999999.99'), decimal_places=2)


class RateSettingIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    merchant_id: UUID
    direction: Literal['kz-ge']
    destination_country: Literal['GE']
    parser_id: Literal['binance_p2p_kaspi_buy_100000_avg10']


@router.get('/rates')
async def rates(actor=Depends(require_roles(Role.superadmin))):
    return await get_state()


@router.get('/binance-rate')
async def binance_rate(actor=Depends(require_roles(Role.superadmin))):
    return await get_binance_state()


@router.post('/rate-settings')
async def rate_setting(data: RateSettingIn, request: Request,
                       db: AsyncSession = Depends(get_db),
                       actor=Depends(require_roles(Role.superadmin))):
    try:
        row = await set_parser(db, actor=actor, merchant_id=data.merchant_id,
                               direction=data.direction,
                               destination_country=data.destination_country,
                               parser_id=data.parser_id)
        await audit(db, 'crossborder_rate_parser_assigned',
                    'merchant_crossborder_rate_setting', actor.id, row.id,
                    client_ip(request), {'merchant_id': str(data.merchant_id),
                                         'direction': row.direction,
                                         'parser_id': row.parser_id})
        await db.commit()
        return public_setting(row)
    except RateSettingError as exc:
        await db.rollback()
        raise HTTPException(422, {'code': exc.code, 'reason': str(exc)}) from exc


@router.post('/quotes')
async def preview(data: PreviewIn, db: AsyncSession = Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    try:
        quote = await create_preview(db, actor=actor, direction_id=data.direction,
            merchant_id=data.merchant_id, trader_id=data.trader_id, amount=data.amount_kzt)
        await db.commit()
        return public_quote(quote)
    except (CrossBorderUnavailable, XeUnavailable, BinanceP2PUnavailable, FeeConfigurationError) as exc:
        await db.rollback()
        raise HTTPException(409, {'code': exc.code, 'reason': str(exc)}) from exc


@router.get('/quotes/{quote_id}')
async def saved(quote_id: UUID, db: AsyncSession = Depends(get_db), actor=Depends(require_roles(Role.superadmin))):
    quote = await db.get(CrossBorderQuote, quote_id)
    if not quote or quote.actor_id != actor.id:
        raise HTTPException(404, 'Расчёт не найден')
    return public_quote(quote)
