"""Country-scoped bank directory validation for trader requisites."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import PaymentMethod
from app.core.ge_bank_requisites import GE_REQUISITE_METHODS
from app.core.payment_methods import normalize_payment_method
from app.core.requisite_providers import resolve_requisite_provider
from app.models import BankDirectory, User


@dataclass(frozen=True, slots=True)
class RequisiteJurisdiction:
    method: str
    bank_id: str | None
    bank_code: str | None
    operator_code: str | None
    bank_name: str
    country: str
    currency: str
    enabled: bool
    status: str
    provider_type: str


async def list_country_banks(db, country: str | None) -> list[BankDirectory]:
    if country not in {'AM', 'GE', 'TJ'}:
        return []
    return (await db.execute(
        select(BankDirectory).where(
            BankDirectory.country == country,
            BankDirectory.status == 'active',
        ).order_by(BankDirectory.institution_type, BankDirectory.name)
    )).scalars().all()


async def resolve_requisite_jurisdiction(
    db,
    account: User,
    *,
    method: str,
    bank_id: str = '',
    bank_code: str = '',
    operator_code: str = '',
    bank_name: str = '',
    requested_country: str | None = None,
    requested_currency: str | None = None,
    allow_legacy: bool = False,
) -> RequisiteJurisdiction:
    country = account.working_country
    currency = account.working_currency
    if requested_country and requested_country != country:
        raise HTTPException(422, 'Страна реквизита не соответствует назначенному профилю')
    if requested_currency and requested_currency != currency:
        raise HTTPException(422, 'Валюта реквизита не соответствует назначенному профилю')
    normalized = normalize_payment_method(method, canonical_mobile=True)
    if country == 'RU':
        if bank_id:
            raise HTTPException(422, 'Иностранный банк нельзя назначить российскому профилю')
        if normalized not in {PaymentMethod.sbp.value, PaymentMethod.c2c.value, PaymentMethod.mobile_commerce.value}:
            raise HTTPException(422, 'Недопустимый способ оплаты')
        provider = resolve_requisite_provider(
            normalized,
            bank_code=bank_code,
            operator_code=operator_code,
            bank_name=bank_name,
            allow_legacy=allow_legacy,
        )
        if not provider:
            raise HTTPException(422, 'Выберите банк или оператора из справочника')
        return RequisiteJurisdiction(
            method=normalized,
            bank_id=None,
            bank_code=provider.bank_code,
            operator_code=provider.operator_code,
            bank_name=provider.display_name,
            country='RU',
            currency='RUB',
            enabled=True,
            status='active',
            provider_type=provider.provider_type,
        )
    if country not in {'AM', 'GE', 'TJ'} or currency != {'AM': 'AMD', 'GE': 'GEL', 'TJ': 'TJS'}[country]:
        raise HTTPException(409, 'Рабочий профиль не назначен')
    allowed = GE_REQUISITE_METHODS if country == 'GE' else (PaymentMethod.c2c.value,)
    if normalized not in allowed:
        raise HTTPException(409, 'Метод реквизита недоступен для назначенной страны')
    if bank_code or operator_code:
        raise HTTPException(422, 'Российские коды банка и оператора неприменимы к иностранному профилю')
    bank = await db.get(BankDirectory, bank_id.strip()) if bank_id else None
    if not bank or bank.status != 'active':
        raise HTTPException(422, 'Выберите банк из справочника')
    if bank.country != country:
        raise HTTPException(422, 'Банк не соответствует стране кабинета')
    return RequisiteJurisdiction(
        method=normalized,
        bank_id=bank.id,
        bank_code=None,
        operator_code=None,
        bank_name=bank.name,
        country=country,
        currency=currency,
        enabled=country in ('GE', 'TJ'),
        status='active' if country in ('GE', 'TJ') else 'review',
        provider_type=bank.institution_type,
    )
