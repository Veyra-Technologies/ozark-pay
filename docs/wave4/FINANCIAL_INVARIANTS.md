# Финансовые инварианты Wave4

Все нижеуказанные автоматические сценарии используют отдельные PostgreSQL/Redis и синтетические данные. Подтверждение SQL/ledger не является подтверждением движения денег во внешней системе.

| Механизм | Проверяемое равенство/ограничение |
|---|---|
| Roll account | principal = recovered + outstanding; все суммы ≥ 0 |
| Roll aggregates | account = суммы confirmed transfers; pending/disputed не входят в principal |
| Transfer recovery | recovered = Σ recovery consumption − Σ reversal consumption |
| Paid TJ allocation | net_USDT = recovery_USDT + Merchant_credit_USDT |
| Local evidence | net_TJS = recovery_local + settle_local; RUB-поля TJ остаются нулевыми/NULL по контракту |
| Executor payment | executor_debit = Merchant_credit + Roll_recovery + platform_margin |
| Margin | executor_debit − полный Merchant net; recovery не является второй margin |
| Merchant Settlement | payout + 5 = hold = terminal debit; recipient amount = payout |
| Executor wallet | available = balance − hold ≥ 0; balance ≥ hold ≥ 0 |
| Merchant balance | available ≥ 0; frozen ≥ 0; собственная currency/cabinet |
| Refund | net movement paid + inverse = 0; recovery возвращается в outstanding |
| Idempotency | один business event → одна финансовая проводка/consumption; конфликт payload не является retry |

## Независимые вычисления

В QA `qa_oracle.py` использует stdlib Decimal, собственные округления и FIFO. Он не вызывает `rolling`, `fee_amount`, `base_amount`, `usdt`, `settlements` или другие финансовые функции приложения. Проверяются 120 детерминированных комбинаций суммы, процентов, 12-place курса и outstanding. Пример экономического объёма находится в ROLL_FINANCIAL_CONTRACT.md.

`qa_ledger_oracle.py` отдельно читает persisted snapshots и journals. Он пересчитывает fee, net, debit и margin по входным значениям, затем проверяет фактические Merchant credit/refund, recovery/reversal, executor debit/refund и platform entries. Aggregator route дополнительно не допускает TraderWalletLedgerEntry той же операции.

Не следует складывать recovery с margin как доход. Внутренний net Merchant уже включает комиссию. Утверждение «margin = executor_debit − только wallet overflow» было бы неверно при Roll.

## Eligibility и scope

Payment created_at, Merchant/business/country, financial executor, fee plan/version и quote фиксируются. Применимый transfer должен быть confirmed не позже created_at и иметь sequence не выше сохранённой eligibility. Аванс, подтверждённый одновременно с оплатой более старого Deposit, не погашается этим старым платёжным событием. Коллатеральный topup Aggregator не меняет Merchant debt.

Для TJ нет QR, есть C2C минимум 10 TJS и утверждённые банки. Другой country wallet/parser/tariff не подставляется при отсутствии своего. RU Rapira/RUB и KZ/GE BASE_USDT-контракт проверяются отдельно.

## Транзакции и конкуренция

Финансовые переходы держат parent/Deposit locks, затем свои allocation/account/transfers/wallet/balance locks. Изменения проводок, статуса, snapshot settlement state и outbox сохраняются одной DB-транзакцией. UNIQUE constraints дополняют application idempotency. Deferred guards проверяют итог транзакции; промежуточное состояние внутри корректной инверсии не является состоянием для внешнего чтения.

Проверены concurrent paid, refunds/appeals/Settlement actions из актуального suite, отдельная гонка cancel/confirm funding, concurrent signed callbacks и crash recovery outbox. Для неизвестного результата внешнего вызова hold сохраняется; повторный create требует доказанного отсутствия по стабильному external ID и подтверждённого idempotent-create контракта.

## Дополнительные preflight-ограничения

`Numeric(24,8)` хранит 8 дробных и 16 целых знаков. Перед 0045 обязательно проверить, что старые 24,6 значения укладываются в новую целую часть. PostgreSQL остановит несовместимый ALTER транзакционно; нельзя округлять/обнулять историю для успешного upgrade. Результат QA на небольших суммах не заменяет эту проверку Production.

Два TXID с разным регистром, но одинаковыми 64 hex в TRC20, являются одним evidence. 0046 откажется строить index при исторических дубликатах. Требуется ручное выяснение основания каждого обязательства, без удаления финансовой истории.
