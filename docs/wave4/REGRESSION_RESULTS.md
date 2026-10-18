# Регрессия Wave4

Исходный доступный набор — 393 случая, не исторические 782. Добавлено 156 случаев Wave4: precision/immutable evidence, отсутствие/ноль collateral, deterministic cancel-confirm race, 120 независимых Decimal-комбинаций, экономический пример/FIFO/Settlement oracle, 10 Linux secret-file вариантов, 10 финансовых статусов и два настоящих HTTPS Trader TJ/RU menu tests. Итоговый полный набор — 549.

Финальные фактические counts, duration, exit codes и image/source digests находятся в release `QA-RESULTS.json`, `pytest-full.log`, `package-reproduction/` и manifest. Нельзя считать ранний/прерванный лог итоговым PASS.

## Дополнительные PostgreSQL/API проверки

| Группа | Проверки |
|---|---:|
| Wave3.2.1 Settlement + FIFO + inverse | 31 |
| Negative/concurrent/appeal acceptance | 31 |
| KZ/GE current engine | 8 |
| Signed external callbacks | 12 |
| Новый independent Merchant HMAC signer | 8 |
| Bybit real Redis/PG fault/concurrency | 12 |
| Roll independence / concurrent old payment / zero collateral | 9 |
| External outbox crash/unknown/lease/callback concurrency | 17 |

Итого 128 именованных дополнительных проверок, отдельно от pytest. Persisted Decimal/ledger oracle проверяет каждую подготовленную TJ-операцию; количество строк зависит от созданного набора и приводится в JSON-логе. Linux process/container lifecycle и migration restore — отдельные acceptance evidence, не прибавлены к pytest для красивого числа.

## Воспроизводимость

`RUN_QA.py --source-zip OzarkPay-Wave4-source.zip --qa-zip OzarkPay-Wave4-QA.zip` создаёт собственную Docker network, PostgreSQL, Redis, authenticated mock и test container, генерирует только synthetic credentials в памяти, сверяет source ZIP с bytes image, мигрирует четыре QA-базы и запускает suite/acceptance. `--smoke` ограничивает pytest 156 новыми случаями, но выполняет financial acceptance. Ни host test tree, ни private owner DB для этого не нужны. По завершении runner удаляет только свои именованные ресурсы, без global prune. Logs сохраняются в заданный output.

Реальные Bybit ключи не входят в пакет: LIVE evidence не воспроизводится synthetic credentials. Реальный внешний Aggregator API не проверен; mock явно назван mock.

## Предварительные неуспешные прогоны

- Старый QA ZIP не содержал scripts/seed.py; collection остановилась на двух imports. Точный synthetic helper восстановлен из original ZIP исключительно для QA.
- Ранний запуск с BINANCE_P2P_ENABLED=false дал ожидаемый отказ mock-cache test; runner использует штатный enabled contract этого теста. Тест не удалён.
- Новые UI tests сначала использовали HTTP, при котором HTTPX не отправляет Secure session cookie. Исправлена фикстура на HTTPS, без выключения secure cookies, CSRF или 2FA. Все 156 новых случаев затем прошли.
- Неподдерживаемый unlinked Aggregator debit был правильно отклонён PostgreSQL. Zero-collateral acceptance перенесена на отдельную законно подготовленную синтетическую базу; защиту не убирали.
- Ранний Linux probe budget не принят как successful Docker health. Финальный runtime проверен с достаточным memory/CPU и startup interval.

## Статические и браузерные проверки

Python compile всех source .py, Ruff E9/F63/F7/F82: без находок. Bandit HIGH/HIGH: без находок/scan errors. OSV: 67 pinned PyPI dependencies, найденных advisories в момент запроса нет; OS package/image CVE scan не заявляется. Exact-known-secret/pattern scan и финальная поставка проверяются отдельно.

CUA-приёмка: Merchant, Trader и Aggregator TJ sections, desktop/mobile navigation, суммы и holds, Settlement/Roll history, four protected API inputs, GET filters, hash и reload. Статусы/Trader overview исправлены и проверены на финальном image. Screenshots содержат только synthetic данные. Все финансовые данные ручного стенда владельца сохранены.
