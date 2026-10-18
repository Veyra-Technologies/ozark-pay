# Миграции и сохранность данных

Опубликованные 0038–0045 не переписаны. Добавлена forward revision `0046_rolling_evidence_integrity`, parent `0045_tj_rolling_admission`. Единственная migration head проверяется Alembic. 0046 добавляет canonical TRON unique index и защиту зарегистрированного evidence; существующие суммы/история не пересчитываются.

## Независимые результаты

| Проверка | Результат |
|---|---|
| Fresh PostgreSQL upgrade всей цепочки | Выполнен в отдельных QA-базах |
| Копия существующей локальной QA 0045 | pg_dump custom 515650 bytes, pg_restore --list, фактический изолированный restore |
| Точное сравнение копии | 75 защищённых таблиц / 236 строк; все row SHA256 совпали |
| 0045 → 0046 на копии | 7,407 s в локальном измерении; все защищённые строки сохранены |
| Повторный upgrade | Не меняет данные |
| FK / индексы копии | 0 orphan rows, 0 unvalidated constraints, 0 invalid indexes |
| Legacy RU/KZ 0042 → 0046 | 76 таблиц / 87 строк; old-column projection, Decimal precision и ciphertext сохранены |
| Existing Settlement | Две завершённые RU/KZ записи, hold/debit/fee journals сохранены |
| Existing Roll | RU principal/history сохранены; downgrade с новым evidence отказал |
| Generic processor 0043 | Исходный `tradespace` dialect из original 0030 превращён в generic adapter с сохранённым `X-tradespace` header prefix |

Указанное время относится только к маленькой локальной QA-копии. Production duration/locks/volume: NOT MEASURED. Приватный dump не сохранён на ноутбук, не добавлен в Git/ZIP и не отправлен на Timeweb.

## Что именно проверялось

User passwords/2FA ciphertext, Merchant production API ciphertext/mode, parser assignments, balances, financial journals, histories и settings сравниваются до/после. Сравнение legacy использует исходные колонки; новые nullable/local columns и 6→8 Decimal scale не считаются изменением экономического значения. Для криптографических строк сравниваются точные значения, а не декодирование/перевыпуск.

0043 использует реальные сохранённые HTTP header names как данные. Проверка использовала точную 0030 из исходного ZIP: SHA256 `afc94990372b1797e66955728dc0b931a7ed91a7e30da378801845ee1517a0e5`. Это реконструкция legacy QA, не заявление о чтении текущей Production DB.

0044 расширяет country Settlement и не открывает TJ Production. 0045 расширяет precision, добавляет TJS evidence/immutability, deferred Roll accounting и связывает independent executor accounting с net Roll recovery. Anchored SQL function transform остановится при неожиданном определении функции; нельзя обходить этот отказ изменением истории.

## Обязательные проверки настоящего сервера до upgrade

1. Узнать actual PostgreSQL major, Alembic revision, image digests, schema drift и размеры таблиц. QA проверена на PostgreSQL 18.6; использование production compose со ссылкой postgres:16-alpine не означает, что actual server — 16. Нельзя менять major вместе с этой поставкой.
2. Проверить диапазон всех затрагиваемых USDT columns: 24,8 оставляет 16 целых знаков. Значения, не помещающиеся без потери, блокируют upgrade; не округлять данные.
3. Проверить case-insensitive duplicates TRC20 64-hex TXID и согласованность registered confirmed transfer ↔ funding/topup journal: Merchant/account, amount, network, destination, hash, sent_at. Никакой автоматической дедупликации долгов.
4. Проверить constraints/index validity, orphan rows, FK, immutable function definitions и current reconciliation. Сохранить ciphertext/API mode/HMAC header dialect/version fingerprints.
5. Создать согласованный backup и восстановить его отдельно совместимыми средствами. Выполнить upgrade копии, repeat upgrade и приёмку до изменения server DB.

## Downgrade и recovery

Нельзя обещать универсальный Alembic downgrade. 0046 отказывается удалять новые integrity guards при наличии Rolling transfers. 0045 блокирует откат TJ Rolling history или narrowing с потерей дробной точности; 0044 — TJ Settlement history; 0043 — потерю legacy wire dialect.

Предпочтительный rollback — прежнее совместимое приложение при сохранении расширенной схемы и guards, с закрытым TJ admission. Если совместимость не доказана, удержать writers остановленными и восстановить полный проверенный backup в согласованную точку приложения/БД. Нельзя восстановить старую БД поверх внешне исполненных новых платежей и затем повторить callbacks. Подробная процедура — ROLLBACK_PLAN.md.
