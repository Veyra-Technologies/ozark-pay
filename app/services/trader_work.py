"""Assigned payment jurisdiction. RUB settlement ledgers keep their original currency."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import User, Requisite, Deposit, TraderLedgerEntry

COUNTRIES = {'RU': ('Россия', 'RUB'), 'AM': ('Армения', 'AMD'), 'GE': ('Грузия', 'GEL')}
RATE_SOURCES = {'RU': 'rapira', 'AM': 'xe', 'GE': 'xe'}


def validate_profile(country, currency=None, source=None):
    if country not in COUNTRIES:
        raise HTTPException(422, 'Выберите страну работы')
    expected_currency, expected_source = COUNTRIES[country][1], RATE_SOURCES[country]
    if currency and currency != expected_currency:
        raise HTTPException(422, 'Валюта приёма не соответствует стране')
    if source and source != expected_source:
        raise HTTPException(422, 'Источник курса не соответствует стране')
    return expected_currency, expected_source


def trader_account_id(user):
    if user.role == 'operator':
        return getattr(user, 'trader_account_id', None)
    return user.id


def accepts_currency(user, currency='RUB'):
    return bool(user and getattr(user, 'working_country', None) == 'RU' and getattr(user, 'working_currency', None) == currency == 'RUB'
                and (user.role == 'trader' or getattr(user, 'trader_account_id', None) == user.id))


async def require_payment_account(db, account_id, currency='RUB'):
    account = await db.get(User, account_id) if account_id else None
    if not account or account.role not in ('trader', 'operator'):
        raise HTTPException(409, 'Трейдер не назначен')
    if not account.is_active or account.is_locked or account.is_archived:
        raise HTTPException(409, 'Кабинет трейдера недоступен')
    if not account.working_country:
        raise HTTPException(409, 'Страну работы должен назначить Admin или Superadmin')
    if not accepts_currency(account, currency):
        raise HTTPException(409, 'Приём в назначенной валюте пока не подключён; RUB недоступен')
    return account


async def configure_new_account(db, actor, target, country=None, currency=None, parent_id=None, source=None, tariff_method=None):
    from app.core.tariff_methods import validate_trader_tariff_method
    validate_trader_tariff_method(target.role, country, tariff_method)
    country = country or None
    currency = currency or None
    if target.role not in ('trader', 'operator'):
        if country or currency or parent_id or source:
            raise HTTPException(422, 'Рабочий профиль доступен только трейдеру и оператору')
        return
    if actor.role not in ('superadmin', 'admin'):
        raise HTTPException(403, 'Рабочий профиль назначает Admin или Superadmin')
    if target.role == 'operator':
        if country or currency or source or not parent_id:
            raise HTTPException(422, 'Выберите трейдера для оператора; страна наследуется')
        parent = await db.get(User, parent_id)
        if not parent or parent.role not in ('trader', 'operator') or not parent.is_active or parent.is_locked or parent.is_archived or (parent.role == 'operator' and parent.trader_account_id != parent.id):
            raise HTTPException(422, 'Трейдер недоступен')
        target.trader_account_id = parent.id
    else:
        if parent_id:
            raise HTTPException(422, 'Трейдер не может быть оператором другого кабинета')
        target.working_currency, target.working_rate_source = validate_profile(country, currency, source)
        target.working_country = country
        if target.id:
            from app.services.trader_finance import ensure_usdt_wallet
            await ensure_usdt_wallet(db, target.id)
        from app.services.parser_assignments import provision_trader
        await provision_trader(db, actor=actor, trader=target)


async def assign_country(db, actor, target_id, country, currency=None, source=None):
    if actor.role not in ('superadmin', 'admin'):
        raise HTTPException(403, 'Рабочий профиль назначает Admin или Superadmin')
    target = (await db.execute(select(User).where(User.id == target_id).with_for_update())).scalar_one_or_none()
    if not target or target.role not in ('trader', 'operator') or (target.role == 'operator' and target.trader_account_id != target.id):
        raise HTTPException(404, 'Кабинет трейдера не найден')
    if target.working_country:
        raise HTTPException(409, 'Страна уже назначена. Изменение требует отдельной миграции')
    currency, source = validate_profile(country, currency, source)
    has_requisites = (await db.execute(select(Requisite.id).where(Requisite.trader_id == target.id).limit(1))).first()
    has_ledger = (await db.execute(select(TraderLedgerEntry.id).where(TraderLedgerEntry.trader_id == target.id).limit(1))).first()
    if country != 'RU' and (target.trader_balance or target.trader_hold or has_requisites or has_ledger):
        raise HTTPException(409, 'Кабинет с рублёвой историей требует отдельной миграции')
    other_currency = (await db.execute(select(Deposit.id).join(Requisite, Requisite.id == Deposit.requisites_id).where(Requisite.trader_id == target.id, Deposit.currency != 'RUB').limit(1))).first()
    if other_currency:
        raise HTTPException(409, 'Валюту исторического кабинета нужно мигрировать отдельно')
    target.working_country, target.working_currency = country, COUNTRIES[country][1]
    target.working_rate_source = source
    from app.services.trader_finance import ensure_usdt_wallet
    await ensure_usdt_wallet(db, target.id)
    from app.services.parser_assignments import provision_trader
    await provision_trader(db, actor=actor, trader=target)
    return target
