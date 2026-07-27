# Postpaid, Rolling и жизненный цикл Deposit

## Экономический смысл

`postpaid` — прежний режим. Мерчант сначала даёт трафик, а после `deposit.paid`
весь `merchant_net_rub` зачисляется в существующий RUB settle-баланс.

`rolling` — отдельный контур обязательств. Ozark заранее передаёт мерчанту USDT,
а будущий успешный `merchant_net` погашает этот аванс. Rolling не является
отрицательным `Balance`: обычный `Balance` — долг Ozark мерчанту, Rolling —
долг мерчанта Ozark.

Исчерпание Rolling не блокирует трафик. При `outstanding_usdt=0` весь net
следующей успешной заявки идёт в settle. Pending exposure и размер заявки также
могут превышать outstanding.

Все существующие мерчанты миграция оставляет в `postpaid`.

## Формулы и точность

Источником комиссии является существующий immutable
`OperationFeeSnapshot`:

```text
merchant_fee_rub = gross_rub × merchant_commission_percent / 100
merchant_net_rub = gross_rub - merchant_fee_rub
merchant_net_usdt = merchant_net_rub / rapira_ask_rate_rub
```

Используются `Decimal` и `ROUND_HALF_UP`: RUB — 2 знака, USDT — 6, курс —
8, процент — 6. Старые операции не пересчитываются по новым тарифам или курсу.

Для paid:

```text
rolling_applied_usdt = min(merchant_net_usdt, outstanding_usdt)
rolling_applied_rub =
    merchant_net_rub, если весь net ушёл в Rolling;
    min(merchant_net_rub, ROUND_HALF_UP(rolling_applied_usdt × rate, 2))
    для граничной заявки
settle_credited_rub = merchant_net_rub - rolling_applied_rub
```

Инвариант каждой paid allocation:

```text
merchant_net_rub = rolling_applied_rub + settle_credited_rub
```

Пример: outstanding 500 USDT, Rapira ask 100 RUB/USDT, gross 100 000 RUB,
комиссия 10%. Net равен 90 000 RUB / 900 USDT. Результат: 500 USDT и
50 000 RUB идут в Rolling recovery, 40 000 RUB — в settle, outstanding
становится 0, account — `exhausted`.

## Strict Rapira snapshot

Expected read-only provider contract (shown here as a schema, not as a captured
provider response):

```json
{
  "data": [
    {
      "symbol": "USDT/RUB",
      "askPrice": "100.12345678",
      "updatedAt": "2026-01-01T00:00:00Z"
    }
  ]
}
```

`data` may be a single market object or a list of market objects. The owner of
the Rapira integration must confirm the production field names and nesting for
symbol, ask price, and market update time before rollout. A numeric JSON
`askPrice` is accepted as well as a numeric string. The market timestamp is
mandatory, must contain a timezone, must not be stale, and must not be more than
five seconds in the future. No staging/read-only provider payload was available
during this review, so the strict parser has not been validated against a real
provider response. Production Rapira was not called.

Rolling использует только свежий реальный `askPrice` Rapira. Допустимы
USDT/RUB и явно инвертированная RUB/USDT. Snapshot содержит symbol, rate,
side=`ask`, source=`Rapira`, received timestamp, market timestamp (если он
есть), `stale=False` и имя provider field `askPrice`.

`bidPrice`, `last`, `close`, произвольный `price`, статический
`SETTLEMENT_USDT_RUB_RATE`, stale cache и last-success fallback запрещены.
Максимальный возраст задаёт `ROLLING_RAPIRA_MAX_AGE_SECONDS` (по умолчанию 60).

Сетевой запрос выполняется до выбора реквизита и долгих `FOR UPDATE`.
Если quote недоступна, rolling-create возвращает
`rolling_rate_unavailable`/HTTP 503 и транзакция не оставляет Deposit, trader
hold или allocation. Идемпотентный повтор существующей заявки не вызывает
Rapira и возвращает первоначальный snapshot.

Postpaid вообще не вызывает strict Rapira.

## Deposit TTL

All unsuccessful transitions use `finalize_unsuccessful_deposit()`. Direct
status assignments remain only for transitions that have different financial
semantics: successful confirmation sets `paid`; opening an appeal sets
`appeal_opened`; appeal approval restores the released allocation before using
the normal paid financing path; appeal rejection restores the recorded
pre-appeal terminal status. These are intentionally not routed through the
unsuccessful finalizer because that service releases holds/allocation and emits
terminal failure outbox records.

`DEPOSIT_PROCESSING_TTL_SECONDS` по умолчанию равен 900. При создании в
`Deposit.expires_at` сохраняется абсолютный UTC deadline. Изменение настройки
не меняет уже созданные заявки.

Миграция backfill устанавливает старым строкам
`created_at + interval '15 minutes'`, после чего колонка становится NOT NULL.
DB default в 15 минут сохранён только как совместимость для старого экземпляра
приложения во время rolling deploy/rollback. Новый код всегда явно записывает
deadline из `DEPOSIT_PROCESSING_TTL_SECONDS`.

Confirm после deadline под `SELECT ... FOR UPDATE`, но до любых финансовых
движений, выполняет единую timeout-финализацию:

- внешний совместимый status остаётся `failed`;
- техническая причина — `trader_timeout`;
- trader hold и pending allocation освобождаются ровно один раз;
- webhook и aggregator callback создаются в той же DB-транзакции;
- delivery task ставится только после commit;
- paid settlement не выполняется.

Тот же lifecycle service используется Celery, ручным Admin/Superadmin decline,
Merchant cancellation и SMS confirm. Повторная финализация является no-op.

## Pending exposure

A pending allocation created while `outstanding_usdt == 0` **does recover a
later top-up** if the allocation subsequently becomes paid. Outstanding is not
reserved at create time. The paid split reads the then-current outstanding
under the Rolling account lock, so the new top-up participates in recovery.
This is the existing behavior, now documented and covered by a dedicated
PostgreSQL test.

При rolling-create создаются одна allocation на Deposit и immutable
`pending_added` ledger entry. Outstanding на create не уменьшается.

```text
pending_exposure_usdt = сумма net всех pending allocations
covered_pending_usdt = min(outstanding, pending_exposure)
uncovered_pending_usdt = max(pending_exposure - outstanding, 0)
```

Конкретный кусок outstanding не резервируется за заявкой. Окончательный split
происходит только при paid под lock Rolling account. При paid, failed,
timeout, decline или cancellation пишется компенсирующий `pending_released`.

## Funding, top-up и переключение режима

Funding/top-up доступен только Superadmin через CSRF-защищённую форму и
аутентифицированную 2FA-сессию. Обязательны amount, network, destination
address, tx hash, funded timestamp и reason. Первый перевод — `funding`,
следующие — `topup`.

Идемпотентность определяется стабильным ключом от merchant + normalized network
+ tx hash; в БД дополнительно действует partial unique index по network/tx hash.
Funding увеличивает `principal_usdt` и `outstanding_usdt`. Top-up после
`exhausted` возвращает account в `active`.

При включении rolling создаётся безопасный account с нулевыми totals.
Переключение rolling → postpaid запрещено при outstanding > 0 или pending
allocations. Скрытого write-off и автоматического зачёта существующего settle
нет. Suspension — только явное действие Superadmin с reason; `exhausted` трафик
не блокирует.

## Reversal и апелляции

Публичного UI reversal нет. Внутренний `reverse_paid_allocation()`:

- блокирует account и allocation;
- возвращает `rolling_applied_usdt` в outstanding;
- уменьшает recovered;
- дебетует только ранее начисленный `settle_credited_rub`;
- создаёт immutable reversal ledger entry и audit;
- повторный вызов идемпотентен.

Если settle уже выведен/заморожен и available недостаточен, reversal
останавливается. Отрицательный available не создаётся; нужен отдельный
receivable/debt workflow.

Одобрение апелляции по ранее failed rolling Deposit восстанавливает исходную
released allocation и её fee/rate snapshot, затем использует единую
`apply_merchant_financing()`.

## Reconciliation

The compensating workflow for a future public reversal is deliberately not
implemented in this change. The minimum design is:

1. create an immutable merchant receivable for the settle shortfall, linked to
   the original Deposit, allocation, attempted reversal, actor, and reason;
2. record any collection as a separate `manual_adjustment` ledger entry, never
   by editing Rolling totals or the original recovery;
3. require a separately authorized and audited `write_off`, with an explicit
   loss amount and approval identity;
4. reconcile receivable principal, collections, and write-offs independently
   from the Rolling account.

Its absence is not a blocker while reversal has no public route and an
insufficient available settle balance atomically rejects the internal reversal.
It becomes a production blocker before exposing reversal to staff or before an
appeal policy permits paid deposits to be reversed after settle withdrawal.

`reconcile_rolling_account()` ничего не исправляет. Он сравнивает:

- funding/top-up ledger с principal;
- recovery минус reversal с recovered;
- principal минус recovered с outstanding;
- `pending_added - pending_released` с pending allocations;
- paid RUB split;
- active settle overflow с ledger overflow с учётом reversal;
- количество released allocations.

Расхождение возвращается структурированным report, логируется и показывается
Superadmin. Manual adjustment/write-off в текущем UI не реализованы: для них
нужен отдельный компенсирующий сервис с явно документированным направлением,
а не изменение totals.

## Lock order и транзакции

Общий порядок:

1. Deposit;
2. Requisite и trader balance;
3. Rolling account;
4. Rolling allocation;
5. merchant RUB Balance.

В финансовой транзакции нет HTTP-вызовов. Fee, trader settlement, Rolling
split, platform/executor ledger, status, webhook outbox, aggregator callback и
audit фиксируются до одного commit. Доставка запускается после commit.

DB-инварианты: unique profile/merchant, account/merchant, allocation/deposit,
ledger idempotency key, funding network/tx hash; неотрицательные amounts;
immutable Rolling ledger защищён PostgreSQL trigger от UPDATE/DELETE.

## Миграция и rollback

Порядок upgrade:

1. `0014_deposit_finance`: nullable `expires_at`, backfill, NOT NULL, индексы,
   finance profiles с postpaid backfill.
2. `0015_merchant_rolling`: accounts, allocations, immutable ledger,
   constraints, индексы и trigger.

Перед production: backup, проверка одного Alembic head, upgrade на копии
production DB, reconciliation dry run, затем контролируемый rollout.
Downgrade `0015` удаляет только ещё неиспользуемый Rolling-контур. После
реального funding/traffic downgrade является разрушительным и без
архивирования ledger запрещён. Downgrade `0014` удаляет deadlines/profiles и
допустим только при полном откате приложения.

## Инструкция Superadmin

1. Убедиться, что 2FA включена и текущая сессия прошла 2FA.
2. Открыть раздел `Rolling`, выбрать мерчанта, включить `rolling` с причиной.
3. Записать funding: проверить сеть, destination, tx hash, UTC timestamp и
   сумму. Повторный submit с тем же tx hash безопасен.
4. Сверить principal/outstanding и reconciliation=`OK`.
5. Suspension применять только как явное операционное решение с причиной.
6. Возврат в postpaid выполнять только при нулевом outstanding и отсутствии
   pending allocations.

## Manual QA checklist

### Locking, outbox, and migration safety

For appeal resolution the order begins with `Deposit` and then `Appeal`; a
locked Appeal is never used to acquire its Deposit. Lock reads use
`populate_existing=True` so a waiter observes the committed winner rather than
a stale SQLAlchemy identity-map object.

Outbox delivery uses a two-phase claim. The worker locks the event, records a
`delivering` attempt and a two-minute lease, commits, and only then performs DNS
validation and HTTP. It reacquires the event/attempt after HTTP and will not
overwrite a newer attempt. Periodic scanners enqueue queued/failed records and
expired `delivering` leases; the scanners do not perform HTTP while scanning.

Migration `0014` is staged for a large Deposit table: add the nullable column
without a volatile-default rewrite, set the compatibility default separately,
backfill in 10,000-row committed batches, validate a temporary not-null check,
perform the short `SET NOT NULL`, then create the new Deposit indexes with
`CREATE INDEX CONCURRENTLY` outside the migration transaction. Migration
`0015` rejects downgrade once any funded/traffic ledger or non-zero Rolling
total exists.

- Postpaid create не вызывает Rapira и не создаёт allocation.
- Rolling create без fresh ask даёт 503 и не оставляет hold.
- Идемпотентный create возвращает прежний rate.
- Pending может превышать outstanding и не блокирует заявку.
- Paid полностью и частично гасит Rolling; overflow идёт в RUB settle.
- При outstanding=0 весь следующий net идёт в settle.
- Failed/timeout/cancel/decline освобождают pending один раз.
- Confirm после deadline возвращает конфликт и не проводит finance.
- Trader/operator видит только `Принять`; прямой decline получает 403.
- Admin/Superadmin decline требует reason.
- Funding требует 2FA/CSRF/reason и идемпотентен по tx hash.
- Top-up после exhausted возвращает active.
- rolling → postpaid с долгом/pending запрещён.
- Reversal восстанавливает точные Rolling/settle части.
- Merchant API сохраняет available/frozen/currency и добавляет string Rolling.
- Webhook содержит только аддитивные Rolling fields.
- Reconciliation сходится после funding → pending → paid → overflow → reversal.
