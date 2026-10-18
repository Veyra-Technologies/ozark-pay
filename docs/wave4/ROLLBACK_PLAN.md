# Rollback / recovery Wave5

Этот документ — план; в Wave4 Production rollback не выполнялся. Восстановление реальной финансовой БД требует отдельно одобренного окна и подтверждённого согласованного recovery point.

## До изменения

Сохранить exact old/new image digests, commit/schema, Compose/env metadata и permissions, protected data fingerprints, in-flight/outbox/provider IDs, encrypted PG backup + SHA256, uploads и необходимые secrets в защищённом хранилище. Успешно восстановить backup отдельно и проверить данные/приложение. Key/age identity должен быть доступен независимо от app host. Без этого — NO-GO.

## Сценарий A: upgrade не завершён

Не поднимать writers автоматически. Проверить, откатилась ли транзакция Alembic и осталась ли прежняя version_num. Проверить schema/index/function definitions, финансовые fingerprints и отсутствие принятых внешних изменений. Если схема и старый runtime совместимы, восстановить exact старый digest/config и приёмку. Миграционный error не означает разрешение удалить таблицы, функции или history.

## Сценарий B: схема обновлена, бизнес-трафик ещё закрыт

Предпочесть совместимый application rollback с сохранением расширенной схемы и integrity/admission guards. Прежде доказать compatibility на изолированной копии. Слепой Alembic downgrade запрещён: 0046 сохраняет Roll evidence guards при существовании transfers, 0045 блокирует TJ Roll/precision loss, 0044 — TJ Settlement, 0043 — потерю legacy wire dialect.

Если совместимого старого приложения нет, оставить writers остановленными и восстановить проверенный recovery point по сценарию C. Не менять owner passwords/2FA/API keys ради запуска старой версии.

## Сценарий C: требуется restore

1. Закрыть incoming mutations и callbacks; остановить только идентифицированные API/worker/beat/dispatch/reconciliation Ozark writers. Сохранить все события после backup point и статусы внешних исполнителей. Holds неизвестных результатов не освобождать.
2. Снять защищённую forensic copy текущей БД/логов и сохранить исходный повреждённый deployment. Не удалять единственную копию данных.
3. Проверить encrypted checksum, age identity и нужный backup-set timestamp. Расшифровать в owner-only временный каталог вне web root/checkout с измеренным запасом диска. Печатать только технический результат, не secrets.
4. Административной identity создать новую изолированную recovery DB или отдельный PG volume, совместимый по major. Восстановить **в пустую** БД через `pg_restore --exit-on-error --no-owner --no-acl`; явно восстановить/назначить утверждённые ownership/grants. Не выполнять `--clean` против неизвестной действующей БД.
5. Сравнить protected fingerprints, balances/journals, operation statuses, key modes, ciphertext, HMAC dialect, settings и assignments. Проверить FK/index/orphans, schema и read-only reconciliation. Запустить совместимый exact image без provider egress/real consumers, убедиться в /ready и auth.
6. Восстановить согласованные uploads/config/secret metadata. Redis не восстанавливать как финансовую истину: проверить leases/queues/replay state и восстановить согласованную доставку из durable outbox. Не допускать replay ранее исполненного внешнего события после потери nonce/cache.
7. Сопоставить каждый внешний платёж после backup с provider/bank evidence. При unknown использовать authoritative status lookup и ручную сверку. Не делать повторный create, автоматический refund либо fake PAID для выравнивания totals.
8. Переключить только Ozark app на проверенную recovery DB и старый compatible digest; проверить RU/KZ/GE и закрытые TJ gates. Возобновить writers/traffic после решения оператора и сохранить связь между recovery и forensic evidence.
9. Удалить только созданные временные plaintext файлы после checksum/restore проверки. Сохранить encrypted backup, forensic copy и старые images до завершения приёмки/срока хранения.

## Что не является rollback

Переключение статуса completed → pending, удаление ledger/consumption, уменьшение precision, перегенерация пользовательских ключей, отключение DB triggers, обход 2FA, global Docker prune и принудительное освобождение hold неизвестного внешнего результата. Внешнее движение денег не отменяется восстановлением PostgreSQL.

## Проверено в Wave4

Custom dump локальной ручной QA находился только в RAM. Его catalog проверен, фактический restore в другую DB выполнен, 75 таблиц/236 строк совпали; после forward 0046 значения остались прежними. На отдельной legacy RU/KZ фикстуре сохранены 76 таблиц/87 строк и оба Settlement. Downgrade с новым Roll evidence корректно отказал. Эти результаты доказывают локальный механизм, но не существование свежего Production backup: он остаётся обязательным gate Wave5.
