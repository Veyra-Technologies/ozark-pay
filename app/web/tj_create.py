"""Aggregator creation participates in the same staff form and transaction."""
from decimal import Decimal

from fastapi import HTTPException

from app.services.audit import audit
from app.services.aggregators import create_aggregator_account
from app.services.merchant_cabinets import validate_dimensions
from app.services.tj_cabinets import confirm_parser, inline_tariff


async def create_aggregator_from_form(request, db, actor, email, password, form):
    from app.web.routes import _aggregator_redirect, _store_aggregator_secret_flash
    country = str(form.get('merchant_country') or '')
    if country not in ('RU', 'TJ'):
        raise HTTPException(422, 'Агрегатор поддерживает RU или TJ')
    currency = str(form.get('merchant_currency') or '')
    parser = str(form.get('merchant_parser_id') or '')
    methods = list(form.getlist('merchant_payment_methods'))
    validate_dimensions(country, currency, parser, methods)
    if len(password) < 12:
        raise HTTPException(422, 'Пароль должен содержать не менее 12 символов')
    if country == 'TJ':
        confirm_parser(form)
        if form.get('merchant_tariff_plan_id'):
            raise HTTPException(422, 'Для TJ введите индивидуальную сетку, не общий тариф')
        try:
            minimum, maximum = Decimal(str(form.get('agg_min') or '')), Decimal(str(form.get('agg_max') or ''))
            if not minimum.is_finite() or not maximum.is_finite() or minimum < 10 or maximum < minimum:
                raise ValueError
        except (ValueError, ArithmeticError) as exc:
            raise HTTPException(422, 'Укажите корректные лимиты TJS от 10') from exc
    else:
        minimum, maximum = Decimal('100'), Decimal('150000')
    account, secret = await create_aggregator_account(db, actor=actor,
        name=str(form.get('merchant_name') or email).strip(), country=country,
        parser_id=parser, login_email=email, login_password=password,
        min_payment_amount=minimum, max_payment_amount=maximum)
    if country == 'TJ':
        await inline_tariff(db, actor=actor, party='merchant', entity_id=account.platform_merchant_id, form=form)
    await audit(db, 'cabinet_aggregator_created', 'aggregator', actor.id, account.id,
                details={'country': country, 'login_created': True})
    await _store_aggregator_secret_flash(request, actor, {'name': account.name,
        'api_key': account.api_key, 'secret_key': secret})
    await db.commit()
    return _aggregator_redirect('Кабинет агрегатора создан. Сохраните показанные ключи.', success=True)
