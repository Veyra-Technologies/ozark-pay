"""Use existing requisite controls for both GE identifier types, in received GEL."""
from datetime import timedelta
from decimal import Decimal

from app.models import CrossBorderOperation, Deposit
from app.services.crossborder_money import CrossBorderError
from sqlalchemy import func, select


async def validate_ge_requisite_limits(db, req, expected_gel, at):
    active = ('created', 'pending', 'appeal_opened')
    counted = (*active, 'paid')
    cross_counted = ('accepted', 'paid', 'refunded')
    cb = (CrossBorderOperation.requisite_id == req.id,)
    dep = (Deposit.requisites_id == req.id,)

    async def counts(since=None, pending=False):
        cross_conditions = (*cb, CrossBorderOperation.status.in_(('accepted',) if pending else cross_counted))
        deposit_conditions = (*dep, Deposit.status.in_(active if pending else counted))
        if since is not None:
            cross_conditions += (CrossBorderOperation.created_at >= since,)
            deposit_conditions += (Deposit.created_at >= since,)
        return (await db.scalar(select(func.count()).select_from(CrossBorderOperation).where(*cross_conditions))
                + await db.scalar(select(func.count()).select_from(Deposit).where(*deposit_conditions)))

    if await counts(pending=True) >= max(1, req.simultaneous_limit):
        raise CrossBorderError('Реквизит уже занят активными заявками')
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if req.operation_limit and await counts(day) >= req.operation_limit:
        raise CrossBorderError('Достигнут суточный лимит количества операций реквизита')
    window = day if req.timeframe == 'день' else at - timedelta(hours=1)
    if req.request_count and await counts(window) >= req.request_count:
        raise CrossBorderError('Достигнут лимит заявок реквизита за таймфрейм')
    last = await db.scalar(select(func.max(CrossBorderOperation.confirmed_at)).where(
        *cb, CrossBorderOperation.status.in_(('paid', 'refunded'))))
    last = max((stamp for stamp in (last, req.last_success_at) if stamp), default=None)
    if req.success_delay_minutes and last and at - last < timedelta(minutes=req.success_delay_minutes):
        raise CrossBorderError('Не истекла задержка после успешной операции реквизита')
    if expected_gel is None:
        if req.min_check > 0 or req.daily_limit > 0 or req.max_check > 0:
            raise CrossBorderError('Для проверки лимитов GEL нужен свежий информационный курс; резерв не создан')
        return
    amount = Decimal(expected_gel)
    if amount < req.min_check or amount > req.max_check:
        raise CrossBorderError('Сумма GEL вне лимитов одного платежа реквизита')
    if req.daily_limit and req.daily_limit > 0:
        cross_sum = await db.scalar(select(func.coalesce(func.sum(func.coalesce(
            CrossBorderOperation.actual_gel, CrossBorderOperation.expected_gel)), 0)).where(
            *cb, CrossBorderOperation.status.in_(cross_counted), CrossBorderOperation.created_at >= day))
        deposit_sum = await db.scalar(select(func.coalesce(func.sum(Deposit.amount), 0)).where(
            *dep, Deposit.status.in_(counted), Deposit.created_at >= day))
        if Decimal(cross_sum) + Decimal(deposit_sum) + amount > req.daily_limit:
            raise CrossBorderError('Превышен суточный лимит GEL реквизита')
