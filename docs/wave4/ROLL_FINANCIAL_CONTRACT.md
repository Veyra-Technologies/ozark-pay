# Roll Merchant: проверенный финансовый контракт

Дата аудита: 19.10.2026. Область: локальная QA, feature/tj-local-payments. Production не изменялся.

## Источник аванса и момент возникновения долга

Источник — внешний USDT-перевод OzarkPay Merchant, зарегистрированный SuperAdmin. Приложение не отправляет токены и не доказывает исполнение в блокчейне. `register_rolling_transfer` сохраняет сумму, сеть, адрес, TXID, время и ключ идемпотентности; создаётся только pending confirmation. Получение подтверждает владелец этого Merchant через `confirm_rolling_transfer`. Тогда ровно один раз возникают principal и outstanding. На внутренний расчётный баланс Merchant аванс не зачисляется: USDT уже переданы вне системы. Это предотвращает двойной доступ к одному авансу.

Долг принадлежит Merchant и его собственному account. Для выдачи не нужны Trader, Aggregator, их кошельки, обеспечение или заявки. Funding не создаёт executor hold/debit, Merchant Settlement credit, platform margin либо комиссию 5 USDT. Подтверждённый аванс нельзя отменить изменением статуса; для финансового обратного события требуется компенсирующее основание.

Код: [rolling.py](../../app/services/rolling.py), функции `_tj_funding_guard`, `register_rolling_transfer`, `confirm_rolling_transfer`, `set_rolling_suspension`.

## Допустимое основание погашения

Существующий RU-контракт: подтверждённый аванс → новая Merchant payment Deposit → подтверждённая оплата → FIFO из чистой суммы Merchant. Это сохранено для TJ. Deposit здесь является платёжной заявкой, а не внесением обеспечения исполнителя.

Для TJ при создании операции фиксируются индивидуальный Merchant tariff/version, сумма, комиссия TJS, Bybit snapshot, net USDT, время операции и максимальная допустимая sequence подтверждённых авансов. Более позднее funding не финансирует старую заявку задним числом. При оплате используются только подтверждённые к этому времени transfers соответствующего Merchant/account; FIFO ограничен сохранённой sequence. Изменение чужого wallet не является событием погашения.

Обычная заявка сохраняет собственный collateral-контракт исполнителя. Изъятие этого требования ради Roll было бы ошибкой. Проверено: при нулевом доступном Trader collateral обычный Deposit отклоняется, но новый Merchant advance подтверждается. Отдельно проверены funding с полностью отсутствующими и с нулевыми кошельками обоих типов исполнителей.

## Математика

Обозначения: G — gross TJS, p — действующий индивидуальный процент Merchant, q — TJS за USDT, D — допустимый остаток долга.

```text
fee_TJS = ROUND_HALF_UP(G × p / 100, 0.01)
net_TJS = G - fee_TJS
net_USDT = ROUND_HALF_UP(net_TJS / q, 0.00000001)
recovery = min(net_USDT, eligible_D)
Merchant_credit = net_USDT - recovery
new_D = D - recovery
```

Новая Roll-ставка не вводится. Ставка берётся из индивидуального effective TariffPlan и неизменяемого OperationFeeSnapshot. Нет fallback на общий процент.

Пример: аванс 100 USDT, процент Merchant 10%, курс 10 TJS/USDT. Платёж 1000 TJS даёт fee 100 TJS и net 90 USDT; остаётся долг 10 USDT. Для полного погашения достаточно 1111,11 TJS: fee 111,11, net 1000 TJS = 100 USDT. Формула не занижает объём обязательства: погашается net после комиссии. При другой ставке/курсе объём меняется по snapshot; нельзя заменить его фиксированными 1100 TJS или новой процентной надбавкой к principal.

RU сохраняет исходные RUB-округления и USDT до 6 знаков. TJ сохраняет USDT до 8, курс до 12, TJS до 2. В Wave4 исправлена регистрация аванса TJ: `100.00000001` больше не превращается в `100.000000`.

Независимый oracle `qa_oracle.py` находится в QA-пакете и не импортирует финансовые функции приложения. 120 параметризованных примеров сравнивают расчёт приложения с ним; отдельный скрипт сверяет сохранённые проводки.

## Обратные операции и сверка

`release_pending_allocation` снимает только pending exposure, без погашения principal. `reopen_released_allocation` сохраняет исходную eligibility и quote. `reverse_paid_allocation` создаёт reversal consumption, восстанавливает recovery в outstanding, отдельно сторнирует только реально зачисленный overflow. Сама Roll-инверсия не двигает collateral исполнителя. Refund всей платёжной операции дополнительно возвращает её собственный executor debit и сторнирует margin; это самостоятельная часть инверсии Deposit.

Если overflow уже недоступен, refund не создаёт отрицательный Merchant balance: требуется отдельный receivable workflow. Никакой банковский или блокчейн-перевод не выполняется автоматически.

`reconcile_rolling_account` сверяет principal/recovered/outstanding, transfers, net consumptions, pending exposure, paid splits и overflow journal. PostgreSQL guards дополняют проверку сохранённого fee/quote и независимой бухгалтерии исполнителя. Перед обновлением следует отдельно сверять исторические funding evidence с журналом; сама числовая сверка не доказывает реальный перевод в сети.

## Исправления Wave4

- 8-place quantizer выбран до валидации/идемпотентного сравнения TJ advance.
- TRC20 TXID из 64 hex нормализуется по регистру; независимый unique index защищает SQL bypass. Исторические case duplicates блокируют upgrade, а не объединяются.
- 0046 запрещает изменение/удаление зарегистрированного evidence, перенос account, изменение подтверждения и возврат terminal state назад.
- TJ cancellation получает Merchant mutex до transfer lock, как confirmation. Locked rows обновляются из БД, чтобы старый ORM-object не нарушал идемпотентность.

## BLOCKED: прямой перевод Merchant → Trader/Aggregator

В действующем RU/TJ движке нет самостоятельного доказательного события для такого погашения. Простой перевод, пополнение collateral или webhook неизвестного протокола не равны paid Deposit. Не создавались фиктивные оплаченные заявки.

Для открытия отдельного маршрута нужны утверждённые правила: получатель и country/business scope, issuer доказательства, уникальный внешний event ID, amount/currency/FX и время фиксации, связь с обязательством, индивидуальный tariff, частичное исполнение/переплата, повтор/конфликт, возврат, независимый журнал и сверка. До этого маршрут закрыт. TJ Production funding/payment admission также остаётся закрытым.
