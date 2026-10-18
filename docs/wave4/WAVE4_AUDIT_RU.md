# OzarkPay — Wave4: финансовый, security и deployment audit

19.10.2026, Europe/Moscow. UTC-даты 18.10 в логах соответствуют ночи 19.10 по Москве. Область — только OzarkPay, локальная изолированная QA и read-only Bybit. Timeweb/Production/main не изменялись. Источники evidence и точные итоговые digests прилагаются в release manifest и QA-пакете.

## A. Исходная версия

Проверен clean `feature/tj-local-payments`, HEAD `09aeadaccdff4fcaa9f11b83e735f0030e7347bd`. Public remote и открытый Draft PR #2 имели тот же HEAD/base main. Прежние 393/82 не приняты на веру: исходники восстановлены и проверки повторены.

## B. Git и runtime

Между прежним application commit d2b69a3 и 09aead изменились только docs/bybit-p2p-tjs.md и Windows parser supervisor. Сверка текущего ручного image `sha256:1fc0dbc558b9cd11854f69bc2936aea2078edd8a8d1b322340800f07723c68e1`: 399 проверенных файлов без несовпадений. 314 совпавших tracked files прежнего source ZIP также без отличий. Это независимая сверка фактических файлов, а не пересказ прошлого отчёта.

Wave4 исправления сохранены отдельными коммитами: d64b089 (Roll integrity), bebc767 (Linux read-only worker), 501490b (labels), 268cf44 (TJ Trader overview), 1e59f02 (Linux resource/health budget). Итоговый full source commit, image ID, source archive checksum и byte comparison находятся в `RELEASE-METADATA.json`. Отчётный commit может добавлять только документы: application components отдельно сверяются с прошедшим QA состоянием, а packaged runner проверяет bytes source ZIP против image до запуска.

Ручной стенд владельца на 18088 и его image/БД сохранены. Проверенный Wave4 runtime запускался отдельно на синтетических данных, без bind исходников; QA-копии удаляются после формирования пакета. Не выдавать старый ручной runtime за автоматически обновлённый Wave4.

## C. Независимость Roll

Funding/confirmation не требуют существования Trader/Aggregator или их collateral. Подтверждены случаи отсутствующих и нулевых кошельков, неизменность foreign wallets/holds, отсутствие фиктивной Deposit/комиссии/margin. Отдельный concurrent advance не финансирует старую платёжную заявку задним числом. Collateral topup Aggregator не уменьшает Merchant debt. Обычный Deposit без собственного доступного Trader collateral отклоняется; новая выдача Merchant Roll при этом проходит.

## D. Реальный контракт Roll

Платформа вручную регистрирует внешний USDT advance, Merchant подтверждает получение; debt возникает ровно один раз. Внутренний Merchant wallet не получает второй credit этого же аванса. Source/recipient/network/TXID/time зафиксированы, история отдельная. Подтверждение приложения не доказывает перевод в блокчейне. Подробнее — ROLL_FINANCIAL_CONTRACT.md.

## E. Формулы и oracle

Погашается net после snapshotted индивидуальной Merchant commission; новая Roll-ставка не вводится. При gross 1000 TJS, fee 10%, FX 10 net = 90 USDT: для advance 100 остаётся 10. Gross 1111,11 TJS даёт net 100 USDT и закрывает principal. Таким образом, действующая FIFO-математика учитывает комиссию и не занижает согласованный объём. Проверены 120 независимых Decimal-вариантов и persisted journals. Нет Roll +5 fee.

## F. FIFO и прямой маршрут

Существующий RU paid-Deposit FIFO сохранён для TJ: eligible funding confirmed до created_at, immutable sequence/fee/quote, partial/full recovery, overflow и inverse. Поздний topup не меняет eligibility старой заявки. Прямой Merchant→Trader/Aggregator перевод отдельным evidence не поддержан: BLOCKED до спецификации уникального внешнего события, FX/tariff/scope, доказательств, inverses и reconciliation. Fake PAID не создавались.

## G. Settlement TJ

USDT/TRC20, +5 сверху: 100 payout → 105 reserve/debit, recipient 100. Проверены lifecycle, idempotency, concurrency, invalid address/TXID, duplicate TXID, insufficient available, чужой owner, staff без 2FA, country queue, reconciliation и release. network_verified=false; TXID — ручная запись staff. Реальной выплаты не было. Детали — SETTLEMENT_TJ_AUDIT.md.

## H. Trader

Собственный USDT balance/hold, индивидуальный tariff, reserve/debit и refund не смешиваются с Merchant funding. Margin считается относительно полного Merchant net, включая recovery. Суммы показываются до 2 знаков, внутренние 8-place USDT сохранены. Недоступный готовый TJ overview открыт через меню с сохранением прежних section IDs; RU navigation не изменена.

## I. Aggregator

Независимый external executor, собственные tariff/collateral/journal, immutable version и order identity. Внутренний Trader для его заявок не создаётся и не получает debit/reward. 12 callback и 17 crash/unknown проверок — authenticated HTTP mock; реальный provider contract/API не предоставлен и не объявлен проверенным. Production route остаётся закрыт.

## J. Bybit LIVE

ReadOnly=1/Advertising подтверждены существующей DPAPI-парой; запросы — только permissions и ads. Живые свежие quote samples, first3 IDs/prices, независимый AVG3 и PG observation growth записаны. Был реальный 10006 и последующее штатное восстановление; при ошибке rate закрывался. Refresh30/TTL60, нет stale/PG financial fallback. Seller side1 сопоставлен с Buy/зелёной стороной по официальной документации и утверждённому владельцем порядку обычных API ads. Featured не входит в mean. Подробности и ограничения — BYBIT_AUDIT.md.

## K. Security / API

HMAC v2 raw bytes, timestamp, nonce/replay, business idempotency и revocation проверены отдельно независимым signer. Сохранены staff 2FA, Secure sessions, CSRF/Origin, role/country ownership, encrypted integration versions и четыре пустых protected API inputs. SSRF/TLS/DNS pinning и allowlists проверены по коду/negative cases. Host firewall и real external provider security не выдаются за локальный mock PASS.

## L. Country isolation

TJ: TJS/USDT, C2C от 10 TJS, Душанбе Сити/Корти Милли, QR закрыт. Нет fallback на RUB/Rapira/XE/Binance или чужой tariff/wallet. RU и KZ/GE проверены существующим suite и отдельным current engine acceptance. В legacy fixture неверный KZ c2c закономерно был отвергнут; правильный kz-ge прошёл без ослабления country guard.

## M. Конкуренция

Cancel/confirm lock inversion исправлен. Checked locked rows обновляются из DB. Concurrent payments/Settlement/refund/appeal покрыты suite и дополнительными acceptance; два signed callbacks создают один debit/recovery. Unknown external create не отправляется слепо повторно. При недоступном lookup сохраняются hold и manual review.

## N. Refund / Appeal / Reconciliation

Погашение отменяется компенсирующими consumption, восстанавливая debt; только фактический overflow сторнируется с Merchant wallet. Полная Deposit-инверсия отдельно возвращает собственный executor debit и margin. Недоступные активы не превращаются в отрицательный balance. Failed/reopened appeal сохраняют исходный snapshot; таймер не создаёт TJ paid. Проверены повторения и independent persisted conservation.

## O. Миграции

0038–0045 immutable; новая 0046. Fresh upgrade и фактический backup/restore, upgrade копии и repeat прошли. 75 таблиц/236 строк ручной QA сохранены. Legacy 0042→0046 сохранил 76 таблиц/87 строк, RU/KZ Settlement, credentials, Roll и legacy HMAC headers. FK/orphans/index validity проверены. Downgrade с финансовой историей fail-closed. Production major/volume/precision bounds до будущего deploy ещё необходимо измерить. См. MIGRATION_AUDIT.md.

## P. Регрессия

Доступны исходные 393 актуальных cases и 156 новых, всего 549. Дополнительно 128 именованных PostgreSQL/API checks, persisted oracle, Linux lifecycle, migrations, browser и static/secret scans. Финальный full result: **549 passed, 2 warnings, 742.77 s**. Фактический результат заменяется только после завершения; ранние failed/interrupted fixture runs сохранены и описаны. Исторические 782 не выполнялись и не «восстановлены».

## Q. UI acceptance

Пройдены все текущие основные разделы Merchant/Trader/Aggregator TJ, desktop/mobile, wallets/holds/history/stats/API/security/appeals/Settlement/Roll, GET filter и hash/reload. Four integration inputs empty/readonly/disabled. Tariffs и старый TradeSpace brand в активном кабинете не показаны. Индикатор закрытого реального приёма сохранён как значимая бизнес-информация. Screenshot evidence — только synthetic данные, без owner credentials.

## R. RU/KZ/GE

Сохранены RU RUB/Rapira и прежние rounding rules, KZ/GE canonical BASE_USDT и собственные источники курса. Восемь current KZ/GE checks прошли; RU Roll и RU/KZ Settlement входят в regression/legacy migration. Это локальное доказательство совместимости, не изменение либо тестовая запись в настоящие Production кабинеты.

## S. Находки

P1: TJ advance precision; case-sensitive TRON duplicate evidence; mutable transfer evidence. P2: cancel/confirm lock order, невоспроизводимый прежний QA package, скрытый TJ Trader overview и Linux startup probe budget. P3: внутренние финансовые status codes в UI. P0 в проверенной области не обнаружены. Полная классификация/границы — SECURITY_FINDINGS.md.

## T. Исправлено

Однозначные дефекты исправлены небольшими изменениями, новой forward migration и воспроизводимыми проверками. Рабочие RU/TJ FIFO, Settlement и executor engines не переписаны; collateral обычных операций не отключён. Исходная история и credentials не пересчитывались.

## U. BLOCKED / NOT MEASURED

Реальные TJ money flows, прямое transfer-based Roll repayment и real external Aggregator API — BLOCKED. Blockchain confirmation не реализован. Actual Timeweb metrics/version/grants/backup/firewall/maintenance duration — NOT MEASURED и являются gates Wave5. Эти ограничения не маскируются synthetic PASS.

## V. GitHub

Поставка — feature/tj-local-payments, существующий [Draft PR #2](https://github.com/Veyra-Technologies/ozark-pay/pull/2). Main merge запрещён и не выполнен. Финальный remote/PR head, Draft status и local cleanliness проверяются после push и сохраняются в RELEASE-METADATA.json.

## W. ZIP / SHA256

`OzarkPay-Wave4-source.zip` — полный tracked source без tests/demo/private runtime/DB/uploads/secrets. `OzarkPay-Wave4-QA.zip` — воспроизводимые synthetic tests/factory/oracle и обезличенные evidence. Exact names, byte counts и SHA256 — SHA256SUMS. Full diff и commit trace приложены. ZIP не содержит .git history или Production backups.

## X. Timeweb

Подготовлены TIMEWEB_DEPLOYMENT_PLAN.md и ROLLBACK_PLAN.md: actual inventory/resource gates, encrypted backup и isolated restore, preparation/pinning/secrets, quarantine writers, copy upgrade, RU/KZ acceptance, closed TJ admission, reconciliation, controlled restore. Новый сервер/платная услуга не создавались. Deployment в этой Wave не выполнялся.

## Y. Итоговый вердикт

| Gate | Результат |
|---|---|
| FINANCIAL INTEGRITY | PASS в проверенной QA-области |
| ROLL INDEPENDENCE | PASS |
| SETTLEMENT TJ | PASS в QA; Production закрыт |
| BYBIT LIVE | PASS наблюдаемого read-only fetch/recovery; outage 10006 отражён |
| SECURITY | PASS в указанной области; real provider/firewall отдельные gates |
| REGRESSION | PASS: 549 pytest + 128 дополнительных checks |
| MIGRATION SAFETY | PASS на проверенных копиях; Production preflight обязателен |
| READY FOR WAVE 5 | YES — controlled preflight с закрытыми TJ real payments |
| TJ LIVE PAYMENTS READY | NO |

GO на Wave5 означает возможность начать контролируемый preflight/подготовку обновления существующего OzarkPay с закрытым TJ admission. Это не разрешение на реальные TJ-платежи или автоматический deploy без проверки server backup/ресурсов и решения владельца.
