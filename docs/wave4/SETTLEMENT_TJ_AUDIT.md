# TJ Settlement: USDT / TRC20

Контракт: ручной запрос Merchant, +5 USDT сверху, Admin/SuperAdmin approval, ручное исполнение и TXID. Код: [settlements.py](../../app/services/settlements.py), [0044](../../alembic/versions/0044_tj_settlement.py).

100 USDT запроса создают reserve 105 USDT. Получателю причитается 100. Доступного баланса 104 недостаточно. Для TJ нет RUB FX; country, Merchant, business, balance ID, address, network, payout, fee и reserve сохраняются в snapshot. Комиссия не переносится на Roll.

## Проверенный lifecycle

```text
pending → approved → sent → completed
pending/approved → rejected (full reserve release once)
sent → reconciliation → completed
reconciliation → rejected только после verified-not-sent evidence
```

Статус cancelled не выдумывается: отмена до отправки реализуется существующим rejected/release lifecycle. После sent нельзя просто освободить резерв; после completed нет произвольного status refund. Отдельная фактическая обратная выплата не реализуется этой заявкой.

Проверки: repeated create/approval/completion/rejection, concurrent active requests и staff completion, duplicate TXID, conflicting idempotency payload, valid TRON checksum и invalid address, недостаток available, запрет skip sent, чужой owner, staff без 2FA, неверная country queue, повторный reserve/debit/fee, reconciliation и not-sent workflow. RU/KZ сценарии доступны в полном suite; дополнительная смешанная RU/KZ миграционная фикстура содержит завершённые Settlement.

`network_verified` остаётся false. 64-hex TXID и checkbox администратора являются ручным evidence, а не blockchain verification. Запись истории показывает «Исполнение подтверждено администратором». Никаких реальных выплат, wallet SDK или транзакций TRON в Wave4 не было.

TJ разделён от RU/KZ и в UI, и по persisted country/balance fields. Web actions используют verified staff session, RBAC, 2FA и CSRF; service дополнительно проверяет активного actor и TJ twofa_enabled. Знание UUID заявки не даёт Merchant права на чужую заявку.

## Admission

Production TJ Settlement закрыт одновременно service и DB guards. ENV flag или доступный курс не открывают исполнение. Изолированная QA требует ENV=test, заданного префикса базы, роли ozark_tj_test, внедрённого synthetic provider и transaction marker. Без provider и при прямом SQL вне QA marker запись отклоняется.

QA показывает форму USDT/TRC20, +5 и историю с двумя десятичными знаками. Внутренняя точность не меняется отображением. Проверка интерфейса не отправляла заявку владельца и не меняла его данные.

## Rollback

0044 downgrade с TJ Settlement history закрыт. Возврат старого приложения допускается только при совместимости со схемой/историей и сохранении admission guards. Полное восстановление backup — отдельная согласованная процедура после остановки всех финансовых writers; см. ROLLBACK_PLAN.md.
