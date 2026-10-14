# Remediation 2026-10-12: эксплуатация и проверка

Схема остаётся на `0034_canonical_usdt`; новых таблиц и миграций эта доработка не вводит. API, worker и beat не выполняют миграции при запуске. Существующие пользователи, ключи, неизменяемые страны, остатки и ledger сохраняются.

## Тарифы и кабинеты

Для GE Trader тарифный метод — `kz-ge` («Трансгран KZ → GE»). Он доступен при создании кабинета и на вкладке «Тарифы» существующего трейдера. Страна определяет каталог: RU сохраняет СБП, карты, мобильную коммерцию и QR НСПК. Карта `c2c` остаётся транспортом реквизита GE, но не отдельным RU-тарифом для GE.

GE использует существующий `TariffPlan`, `direction=kz-ge`, `basis=base_usdt`, `currency=USDT`. Admin/SuperAdmin сохраняют новые версии через `/api/v1/crossborder/tariffs` или защищённую CSRF кабинетную форму. Индивидуальный план имеет приоритет над общим, применяется к новым заявкам и требует утверждённого справочного курса/даты. Принятые операции сохраняют финансовый снимок. Ни Merchant, ни Trader тарифы не редактируют. Сервер отвергает несовместимые страну, роль, сторону, метод и дополнительные поля.

Trader видит операции, KZT, ожидаемый/фактический GEL, реквизиты и баланс/резерв USDT. Назначения парсеров, конфигурации и технические снимки доступны Admin/SuperAdmin. Создание заявок остаётся только через [Merchant HMAC v2](merchant-hmac-v2.md); рабочий контракт описан в [KZ → GE](crossborder-contract.md).

## Авторизация и входящие запросы

Refresh и logout блокируют строку refresh token в PostgreSQL. Потребление и замена выполняются одной транзакцией: конкурентный повтор не выпускает вторую замену, ошибка откатывает отзыв. Набор транзитивных зависимостей зафиксирован в `constraints.txt`; PyJWT закреплён на 2.15.0, проверка подписи ограничена HS256; JWK и выбор алгоритма из входящего JWT не используются.

Лимит тела проверяет реальные ASGI-байты до обработчиков/CSRF. Размер не зависит от Content-Length; spool удерживает не больше 1 MiB в памяти, остальное в ограниченном временном файле. Сверхлимитный запрос получает 413. Reverse proxy сохраняет собственный лимит.

ExternalProcessorPayment синхронизируется с терминальным Deposit. Поздний pending не отменяет paid/failed/cancelled/expired; новые pending обрабатываются. Уникальный event и блокировки защищают от повторного ledger. Банковский webhook Трансграна использует свой прежний маршрут и подпись.

## Healthcheck и мониторинг

Production Compose вызывает `python -m scripts.healthcheck`: соединение loopback, Host из `TRUSTED_HOSTS`, проверки `/health` и `/ready`. В production нельзя использовать `*` или `0.0.0.0`. Для Prometheus оставьте `api` в доверенных Host.

Сохраните случайный токен (не меньше 24 символов) в защищённом файле вне checkout. В Compose env задайте `METRICS_TOKEN_SECRET_FILE` — путь к этому файлу на хосте. Не путайте его с `METRICS_TOKEN_FILE`: monitoring override задаёт приложению контейнерный путь `/run/secrets/metrics_token`. Без monitoring можно использовать существующий `METRICS_TOKEN` в защищённом env. Содержимое секретов не выводите в консоль.

```bash
docker compose --env-file .env.production -f docker-compose.production.yml -f docker-compose.monitoring.yml --profile monitoring config --quiet
docker compose --env-file .env.production -f docker-compose.production.yml -f docker-compose.monitoring.yml --profile monitoring up -d api prometheus grafana loki promtail
```

Prometheus подключён к `backend` API и к сети мониторинга. Bearer берётся из Docker secret; `/metrics` без токена получает 403. UI мониторинга публикуются только на loopback. Конфигурация Grafana требует отдельного `GRAFANA_ADMIN_PASSWORD`. Эти команды — инструкция эксплуатации; проверка релиза должна выполняться в отдельном локальном Compose-проекте и с новыми томами.

## Wheel

Namespace discovery включает все `app.*` и `scripts.*`. Wheel содержит шаблоны, статику, branding и копию дерева миграций в `app/_migrations`, не затеняя библиотеку Alembic. Статика/шаблоны разрешаются относительно установленного пакета.

```bash
python -m pip wheel --no-deps --wheel-dir dist .
python -m venv /tmp/ozark-wheel-check
/tmp/ozark-wheel-check/bin/pip install -c constraints.txt dist/processing_platform-*.whl
cd /tmp
# Передайте тестовые обязательные env-настройки, не production-секреты.
/tmp/ozark-wheel-check/bin/python -m scripts.verify_package_install
/tmp/ozark-wheel-check/bin/python -m scripts.migrate --preflight
```

`verify_package_install --manifest FILE` дополнительно сравнивает весь перечень исходных Python/шаблонов/статики с RECORD wheel. Проверка импортирует модули, загружает шаблоны, открывает login/static/health и сверяет Alembic head. Миграция установленного пакета выполняется через `python -m scripts.migrate`, которому не нужен checkout.

## Резервные копии

Скрипты работают только с OzarkPay, требуют Bash, Docker CLI и age. Закрытая age identity хранится отдельно от checkout и резервных копий. Окончания строк LF закреплены в `.gitattributes`, Docker выставляет режим 755.

```bash
bash scripts/backup_encrypted.sh --output-dir /protected/ozark-backups --recipient-file /protected/recipients.txt --ozark-db ozark-project-db-1
bash scripts/verify_encrypted_backup_restore.sh --backup-set /protected/ozark-backups/backup-set-TIMESTAMP --identity-file /protected/identity.txt
```

Первая команда читает указанную БД через pg_dump, шифрует, сохраняет SHA256 и удаляет plaintext. Вторая проверяет hashes и восстанавливает в новом временном PostgreSQL на internal-сети без опубликованных портов; затем удаляет только созданный контейнер и его временный том. Основная БД не восстанавливается поверх существующих данных. Проверяйте exit code: ошибка backup/restore запрещает считать копию проверенной.

## Регрессия и ограничения

```bash
python -m compileall -q app scripts
ruff check app scripts tests --select E9,F63,F7,F82
bash -n scripts/run_tests_workflow.sh scripts/backup_encrypted.sh scripts/verify_encrypted_backup_restore.sh
bash scripts/run_tests_workflow.sh
```

Полный pytest необходимо повторить после последнего изменения исполняемых файлов. Тестовый Compose использует отдельные PostgreSQL/Redis и не публикует их порты. Конкурентные тесты используют независимые PostgreSQL-сессии. Финансовая проверка использует синтетические provider observations: это не доказательство успешного банковского перевода. Живые Rapira/Binance проверяются отдельно; доступность зависит от внешнего сервиса. Production, внешние переводы, TLS инфраструктура и интеграция с реальным банком требуют отдельной приёмки.
