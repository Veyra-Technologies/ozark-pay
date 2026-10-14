"""Merchant settlement policy; independent of deposit and trader tariffs."""
from decimal import Decimal

SETTLEMENT_FEE_USDT = Decimal('5.00')
SETTLEMENT_NETWORK = 'TRC20'
SETTLEMENT_CURRENCIES = {'RU': 'RUB', 'KZ': 'USDT'}
SETTLEMENT_ACTIVE_STATUSES = ('pending', 'approved', 'sent', 'reconciliation')

SETTLEMENT_STATUS_LABELS = {
    'pending': 'Ожидает одобрения', 'approved': 'Одобрено, ожидает отправки',
    'sent': 'Отправка зарегистрирована', 'reconciliation': 'Ожидает сверки',
    'completed': 'Исполнение подтверждено администратором', 'rejected': 'Отклонено',
}
SETTLEMENT_ERROR_MESSAGES = {
    'permission_denied': 'Недостаточно прав для действия с заявкой.',
    'merchant_settlement_country_required': 'Для новых заявок назначьте кабинету страну RU или KZ.',
    'invalid_usdt_amount': 'Укажите положительную сумму выплаты USDT.',
    'invalid_trc20_address': 'Укажите корректный адрес TRON с действительной контрольной суммой.',
    'invalid_trc20_network': 'Адрес не соответствует сети TRON (TRC20).',
    'invalid_tron_txid': 'TXID должен содержать 64 шестнадцатеричных символа.',
    'tx_hash_required': 'Укажите TXID отправленной транзакции TRON.',
    'merchant_settlement_rate_unavailable': 'Свежий расчётный курс Rapira временно недоступен.',
    'merchant_settlement_insufficient_available': 'Недостаточно доступных средств: комиссия 5 USDT оплачивается сверх выплаты.',
    'merchant_settlement_pending': 'Сначала завершите обработку текущей заявки Settle.',
    'merchant_settlement_quote_changed': 'Расчёт изменился. Обновите полную стоимость и подтвердите заявку заново.',
    'merchant_settlement_idempotency_mismatch': 'Ключ повторного запроса уже использован с другими платёжными данными.',
    'merchant_settlement_not_found': 'Заявка данного кабинета и страны не найдена.',
    'merchant_settlement_owner_changed': 'Страна, кабинет или расчётный кошелёк не соответствуют зафиксированной заявке.',
    'merchant_settlement_invalid_frozen_balance': 'Резерв заявки не согласован с финансовым журналом. Требуется проверка.',
    'merchant_settlement_approval_required': 'Сначала одобрите заявку без регистрации отправки.',
    'merchant_settlement_dispatch_required': 'Сначала зарегистрируйте состоявшуюся отправку и TXID.',
    'merchant_settlement_dispatch_confirmation_required': 'Подтвердите, что отправка выполнена вручную.',
    'merchant_settlement_execution_evidence_required': 'Подтвердите исполнение и укажите основание проверки выплаты.',
    'merchant_settlement_tx_hash_duplicate': 'Этот TXID уже зарегистрирован для другой выплаты.',
    'merchant_settlement_tx_hash_mismatch': 'TXID не совпадает с уже зарегистрированной отправкой.',
    'merchant_settlement_already_completed': 'Завершённую выплату нельзя изменить.',
    'merchant_settlement_already_rejected': 'Заявка уже отклонена.',
    'merchant_settlement_reject_reason_required': 'Укажите причину длиной до 500 символов.',
    'merchant_settlement_reconciliation_required': 'После отправки резерв нельзя освободить обычным отказом. Выполните сверку.',
    'merchant_settlement_reconciliation_evidence_required': 'Для освобождения резерва нужны сверка, основание и подтверждение отсутствия выплаты.',
}
