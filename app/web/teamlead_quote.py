"""Read-only TeamLead withdrawal presentation; no new balance or conversion."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, func

from app.models import TeamLeadBalance, TeamLeadSettlement
from app.services.rapira import get_strict_rolling_ask_quote, RollingRateUnavailable
from app.services.teamlead import (
    TEAMLEAD_SETTLEMENT_COOLDOWN, TEAMLEAD_SETTLEMENT_FEE_USDT,
    TeamLeadError, teamlead_max_withdrawal, teamlead_settlement_amounts,
)


async def withdrawal_preview(db, teamlead_id, requested=None):
    balance = (await db.execute(select(TeamLeadBalance).where(
        TeamLeadBalance.teamlead_id == teamlead_id))).scalar_one_or_none()
    available = Decimal(balance.available_rub) if balance else Decimal('0.00')
    result = {'available': False, 'reason': '', 'available_rub': str(available),
              'fee_usdt': str(TEAMLEAD_SETTLEMENT_FEE_USDT)}
    if balance and balance.debt_rub > 0:
        return {**result, 'reason': 'Вывод заблокирован до погашения задолженности.'}
    pending = (await db.execute(select(TeamLeadSettlement.id).where(
        TeamLeadSettlement.teamlead_id == teamlead_id,
        TeamLeadSettlement.status == 'pending').limit(1))).scalar_one_or_none()
    if pending:
        return {**result, 'reason': 'Сначала дождитесь завершения текущего запроса на вывод.'}
    last = (await db.execute(select(func.max(TeamLeadSettlement.completed_at)).where(
        TeamLeadSettlement.teamlead_id == teamlead_id,
        TeamLeadSettlement.status == 'completed'))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if last:
        next_at = last.replace(tzinfo=timezone.utc) + TEAMLEAD_SETTLEMENT_COOLDOWN if last.tzinfo is None else last + TEAMLEAD_SETTLEMENT_COOLDOWN
        if now < next_at:
            return {**result, 'reason': f'Следующий вывод доступен {next_at:%d.%m.%Y %H:%M} UTC.'}
    try:
        quote = await get_strict_rolling_ask_quote()
        now = datetime.now(timezone.utc)
        maximum = teamlead_max_withdrawal(available, quote, now=now)
        result.update(available=True, max_usdt=str(maximum), rate=str(quote.rate),
                      fetched_at=quote.fetched_at.isoformat(), source='Rapira · ask',
                      fee_rub=str(teamlead_settlement_amounts(Decimal('1'), quote, now=now).fee_rub))
        if requested == 'max':
            requested = maximum if maximum > 0 else None
        if requested is not None:
            amounts = teamlead_settlement_amounts(Decimal(str(requested).replace(',', '.')), quote, now=now)
            result.update(requested_usdt=str(amounts.requested_usdt), requested_rub=str(amounts.requested_rub),
                          total_rub=str(amounts.total_rub), affordable=amounts.total_rub <= available)
        return result
    except RollingRateUnavailable:
        return {**result, 'available': False, 'reason': 'Нет свежего курса Rapira ask. Повторите расчёт позже.'}
    except (TeamLeadError, InvalidOperation, ValueError):
        return {**result, 'available': False, 'reason': 'Не удалось рассчитать сумму: проверьте сумму и доступность свежего курса.'}
