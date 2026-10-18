# Security findings Wave4

Область: исходники feature-ветки, локальные PostgreSQL/Redis, собственные synthetic accounts, API/браузер и read-only Bybit. Production pentest, реальные банковские операции и реальный внешний Aggregator API не выполнялись.

## Находки и исправления

| ID | Приоритет | До исправления | Исправление / доказательство |
|---|---|---|---|
| FIN-001 | P1 | TJ advance 100.00000001 → 100.000000; минимальный 8-place advance мог исчезнуть при quantize | Country-aware quantizer до validation/idempotency; сохранение input/principal, RU 6-place совместимость |
| FIN-002 | P1 | Один 64-hex TRON TXID принимался повторно при смене регистра букв | Canonical service comparison и независимый unique index 0046; SQL bypass также отклоняется |
| FIN-003 | P1 | Registered TXID можно было изменить прямым SQL, оставив старый journal | 0046 immutable evidence/confirmation/terminal-state/account-owner guard; тесты TXID, amount, sent_at, sequence, address и confirmation time |
| CONC-004 | P2 | TJ cancellation брала transfer до Merchant mutex, confirmation — наоборот | Одинаковый lock order и refresh locked ORM rows; детерминированная конкурентная проверка |
| QA-005 | P2 | Старый QA ZIP не содержал seed helper, необходимый двум archived imports; 82 acceptance checks требовали готовую приватную фикстуру | Восстановлен точный synthetic helper из исходного ZIP; новая synthetic fixture factory и RUN_QA.py без зависимости от ручной базы |
| UI-006 | P2 | Готовый TJ Trader summary не был доступен из меню | Добавлен TJ-only раздел «Обзор», старые section IDs сохранены; реальные HTTP TJ/RU проверки |
| OPS-007 | P2 | Недостаточный бюджет/слишком частые startup health probes не давали устойчивого Docker healthy под QA-нагрузкой | 384 MiB, burst до 1 CPU, interval/start_interval 30 s, timeout 30 s; фактический healthy и SIGTERM 0 |
| UI-008 | P3 | В Roll/журнале отображались exhausted/credit/hold и другие внутренние коды | Пользовательские переводы; 10 статусных проверок, browser acceptance |

P0 в проверенной области не выявлены. Это не утверждение об отсутствии уязвимостей вне области аудита. Исправленные P1 не объявляются исторически исправленными в чужих базах: preflight проверяет дубликаты и соответствие funding evidence. История не объединяется и не переписывается.

## Authentication, sessions и доступ

Проверены действующие realm sessions Merchant/Trader/Aggregator/staff, role guards, active/locked/archive checks, staff 2FA, session expiry и auth-state marker, CSRF и Origin проверки. Новая HTTP-фикстура использует HTTPS, чтобы сохранить Secure cookie; ложный HTTP-отказ не исправлялся выключением защиты. Тесты не подменяют 2FA владельца и не меняют его email/password/TOTP.

Прямые TJ service-вызовы funding проверяют роль SuperAdmin с twofa_enabled, Merchant confirmation — owner соответствующего Merchant. Web endpoint дополнительно требует действующую verified staff/session и CSRF. Service-проверка enabled сама по себе не является повторной проверкой кода OTP; настоящая аутентификация находится на границе web/API. Подключение/ротация Aggregator credentials дополнительно требуют свежий TOTP.

Scope основан на authenticated Merchant/owner и immutable executor, не на переданном UUID. Страны, валюты, кабинеты, business, тарифные планы и parser assignments проверяются отдельно. Доступ к текущим данным чужого кабинета не следует из знания ID операции.

## Merchant HMAC v2

Canonical bytes включают version, timestamp, nonce, method, path, sorted query, normalized content-type и SHA256 исходного body. Сравнение constant-time. POST nonce резервируется NX/TTL в Redis; Redis failure закрывает replay guard, а не пропускает запрос. Business idempotency остаётся отдельной от nonce: новый подписанный retry с прежним Idempotency-Key возвращает прежнюю операцию, изменённый payload конфликтует.

8 дополнительных настоящих ASGI/HMAC проверок используют независимый stdlib signer, без override merchant_auth: tampered body, stale timestamp, корректные canonical bytes, exact replay, fresh nonce/idempotency, TJ key → RUB refusal, отключённый legacy HMAC и revoked key. Rotation/signing-key overlap и существующий incoming protocol проверяются актуальным suite. Реальные Merchant keys не вращались.

## Aggregator / outbox / webhook

Четыре credentials шифруются versioned snapshot и не возвращаются в inputs/HTML/JS. В браузере все четыре поля empty + disabled + readonly. API base URL требует HTTPS, разрешённый host, проверку публичных DNS IP и TLS/SNI pinning; redirects/private/reserved destinations запрещены. Concrete production adapter отсутствует: несовместимый протокол нельзя выдать за универсальный API.

Provider webhook связывается с integration version, event ID, raw-body hash, timestamp, webhook identity, external/provider order IDs, amount/currency и сохранённым executor. Повтор/конфликт не даёт второго финансового события. Простое PAID в create response не является доказательством оплаты.

17 crash/unknown acceptance checks дополняют 12 signed-callback checks: потеря ответа после создания заказа, сохранение hold/debt, lookup по stable external ID без blind create, два concurrent callbacks → один debit, поздний worker response, истёкший lease до send, подтверждённое отсутствие и один новый create, unsupported lookup → manual review. Все эти provider-вызовы — аутентифицированный synthetic HTTP mock. Реальный внешний Aggregator API: BLOCKED до предоставления конкретного протокола/песочницы и отдельного утверждения запуска.

## Secrets, temporary files и поставка

Build context/source ZIP строятся из Git snapshot, без .env, private runtime, uploads, dumps, DPAPI, tests/demo helpers. QA ZIP содержит только воспроизводимый synthetic QA-код и обезличенные evidence; raw credentials не публикуются. Резервная копия ручной QA для migration audit существовала только в RAM, затем была удалена из памяти процесса.

Использованы: Python compile всех исходников, Ruff E9/F63/F7/F82, Bandit HIGH severity/HIGH confidence, точные сравнения известных owner secrets и token/private-key patterns, проверка 67 pinned PyPI dependencies в OSV. Это не gitleaks/trivy audit и не полное сканирование OS packages; таких результатов не заявляется. Итоговые Git/ZIP/image checks фиксируются в release evidence.

## Оставшиеся ограничения

- Реальные TJ payments, Settlement и Roll Production остаются закрытыми.
- Direct-transfer repayment contract отсутствует: BLOCKED, без fake PAID.
- Production host metrics, firewall/egress, фактические DB roles/major version и server backups: NOT MEASURED в Wave4; обязательные gates Wave5.
- Blockchain evidence вручную подтверждается staff; TXID не доказывает сетевую finality.
- Provider 10006 возможен; правильное поведение — unavailable/retry, не stale financial fallback.
