# План Wave5: существующий Timeweb, без deploy в Wave4

Ни SSH-команд на Production, ни server migrations, ни новых серверов/платных услуг в Wave4 не выполнялось. Из прежних сведений: 1 vCPU / 2 GB RAM / 30 GB SSD. Actual CPU/RAM/disk/PG major/schema/image/DB roles/traffic/backup space на сервере: **NOT MEASURED**. Эти параметры необходимо получить до выбора окна обновления.

План относится только к существующему OzarkPay. Veyra, PlaySpace, TradeSpace и другие проекты не останавливать, не запускать и не чистить. Не менять существующие RU/KZ accounts, SuperAdmin/password/2FA, API/HMAC secrets, balances, histories или настройки ради приёмки.

## 1. Read-only preflight и привязка к actual deployment

Зафиксировать точные Compose project/file/env paths, имена/ID Ozark containers, networks/volumes и pinned image digests. Сравнить actual main/release commit и Alembic revision с release manifest. Проверить /ready, error rate, очереди, PostgreSQL version/role, extensions, largest tables, pg_database_size, WAL/archive, pg_stat_activity/locks, Docker memory/CPU/PIDs, swap pressure, df -h/df -i и storage latency. Не печатать docker inspect Config.Env или .env.

Повторить FINANCIAL_INVARIANTS preflight: reconciliation, orphan/FK/index validity, Precision bounds 24,8, canonical TXID duplicates, funding evidence ↔ journal, parser assignments/country, API mode/header dialect и credential ciphertext fingerprints. Любое расхождение — STOP до выяснения. Не исправлять историю SQL UPDATE ради upgrade.

Проверить независимый административный доступ к PostgreSQL для migrations, maintenance и restore; приложение не должно получать SUPERUSER. Не отключать LOGIN исходной роли, не переименовывать её и не менять пароль, пока не установлены назначение, зависимости других сервисов и рабочий альтернативный admin/rollback. Wave4 этих действий не выполняла.

## 2. Capacity gate

В локальном Docker QA лимит VM был около 3 GB / 4 CPUs; это не профиль Timeweb. Проверенный Linux parser envelope: 384 MiB, burst до 1 CPU, pids 96, без inbound ports; health 30/30 s, startup 90 s с interval 30 s. Worker/health импортируют отдельные Python processes, поэтому учитывать их совместный пик, не только idle quote worker.

На сервере измерить API, Celery worker/beat, PostgreSQL, Redis, nginx, parser и kernel reserve одновременно при типовой и пиковой нагрузке. Проверить worker concurrency=1 как отдельное обоснованное решение для 1 vCPU, без автоматической правки существующего deployment. Не назначать лимиты, при которых PG или API вытесняются/OOM. Если реальная ёмкость недостаточна — NO-GO, не выключать финансовые проверки и не создавать новый сервер без разрешения.

Диск рассчитывается из реальных размеров: encrypted backup E, временный plaintext dump D, изолированная restore DB P, uploads U, новые image layers I, migration rewrite T, возможный WAL growth W и reserve R. Консервативный одновременный запас: E + D + P + U + I + T + W + R с учётом файловых систем и стадий. На 30 GB может не хватить места; это необходимо измерить. Использовать существующее защищённое внешнее backup storage, если оно доступно и разрешено. Нельзя полагаться на будущую глобальную Docker cleanup.

## 3. Backup и доказанная возможность восстановления

Сначала сохранить защищённые release/config metadata, ciphertext fingerprints, uploads и согласованное состояние очередей. Для финального recovery point остановить поступление новых финансовых операций и изолировать все Ozark writers: API mutations/callback consumers, worker, beat, reconciliation и внешние dispatch jobs. Согласовать drain in-flight requests и provider unknown states; не освобождать их holds.

Существующий encrypted helper работает только с указанной Ozark DB:

```bash
set -Eeuo pipefail
umask 077
: "${OZARK_DB_CONTAINER:?Discover and verify the exact existing Ozark DB}"
: "${BACKUP_DIR:?Protected path outside checkout with measured free space}"
: "${AGE_RECIPIENT_FILE:?Approved public recipient file}"
bash scripts/backup_encrypted.sh --ozark-db "$OZARK_DB_CONTAINER" \
  --output-dir "$BACKUP_DIR" --recipient-file "$AGE_RECIPIENT_FILE"
```

Не использовать default container name без inventory. Сохранить SHA256 encrypted dump и manifest. Проверить доступность age private identity отдельно от backup; не передавать его приложению. pg_restore --list недостаточно: обязательно фактическое восстановление в isolated network без опубликованного порта, совместимым **actual PostgreSQL major/digest**:

```bash
: "${VERIFIED_BACKUP_SET:?Exact freshly verified backup set}"
: "${AGE_IDENTITY_FILE:?Private identity outside checkout}"
: "${POSTGRES_RESTORE_IMAGE:?Compatible pinned PostgreSQL image}"
export POSTGRES_RESTORE_IMAGE
bash scripts/verify_encrypted_backup_restore.sh \
  --backup-set "$VERIFIED_BACKUP_SET" --identity-file "$AGE_IDENTITY_FILE"
```

Дополнительно сравнить users/password/TOTP ciphertext, keys/modes/dialect, balances, operations, journals, Settlement/Roll, assignments и settings. Проверить dump не пустой, orphan/index/constraints, файл checksum и пробный запуск совместимого приложения. Не переносить plaintext backup в public storage/Git/ZIP. Изолированный restore не должен получать callbacks или отправлять реальные webhooks.

## 4. Проверенная копия → готовые images и secrets

На копии actual DB выполнить target Alembic upgrade и repeat upgrade, измерить locks/duration/WAL/space. Проверить RU/KZ/GE, reconciliation, inverse flows и закрытый TJ gate. Production 18.6 не предполагать: QA 18.6 не разрешает major upgrade сервера.

Собрать/доставить release image заранее, проверить Git commit, source ZIP SHA256, image digest и application file manifest. Не собирать большое pip окружение в production maintenance window при недостатке CPU/disk. Сохранить старый image/config до приёмки. Проверить runtime secret files и права без вывода значений. Не запускать demo/seed/bootstrap поверх имеющейся БД.

Linux Bybit: не переносить Windows DPAPI blob. До отдельной защищённой передачи действующей пары оставить parser profile выключенным. После решения владельца — UID 1000, directory 0700, файлы 0400, read-only bind, отсутствие raw Bybit env/argv, approved backend network, HTTPS egress только Bybit + DNS/свои DB/Redis. Host firewall/egress проверить отдельно: Compose не создаёт domain firewall автоматически. Concrete Aggregator production adapter остаётся закрыт.

## 5. Окно обновления — только после решения Wave5

1. Подтвердить ещё раз recovery point, restore result и current resource gates; сохранить in-flight IDs.
2. Изолировать только заранее проверенные Ozark writers, дождаться согласованного drain. Не отбрасывать webhook/outbox события и не помечать unknown как cancelled.
3. Выполнить target upgrade административной migration identity с ограниченными lock/statement timeout, выбранными по репетиции. Миграции не переписывать; при ошибке оставить writers закрытыми.
4. Убедиться в единственной нужной Alembic head и сохранности protected fingerprints/ledger totals. Запустить API на pinned release, затем worker и beat в согласованном порядке.
5. Выполнить RU/KZ login/2FA/API-HMAC checks существующих кабинетов без изменения credentials; проверить balances, histories, /ready, errors, queues и reconciliation. Не выполнять реальную оплату без отдельного разрешения.
6. Убедиться, что TJ Deposit/Settlement/Roll Production и QR остаются закрытыми. Проверить, что ошибка provider/Redis закрывает финансовый rate, а не подставляет старый.
7. Если одобрен запуск parser, проверить два fresh observations, cache TTL, PG history, healthy, bounded restart и sanitized logs. Не включать финансовое TJ admission этим действием.
8. Возобновлять traffic/consumers только после проверки целостности и совместимости. Не повторять неопределённые внешние creates без authoritative lookup.

## 6. Stop/recovery и финальная приёмка

STOP при несовпадении digest/schema/credentials/ledger, drift/duplicates, insufficient disk/RAM, stale rate там, где он финансовый, migration timeout, orphan/unbalanced entries, repeated debit либо неизвестном provider execution. Сначала сохранить evidence и остановить financial writers; действия — по ROLLBACK_PLAN.md.

Приёмка: прежние пользователи и secrets сохранены, RU/KZ/GE проходят, числовые сверки совпадают, worker/outbox восстановимы, новых failed/unknown событий нет без разобранного основания, резервные копии доступны, TJ gates закрыты, rollback ещё доступен. Фактическое окно downtime определяется репетицией на server copy, а не локальными 7,407 s. Запуск реальных TJ-платежей — отдельное решение и отдельные контракты.
