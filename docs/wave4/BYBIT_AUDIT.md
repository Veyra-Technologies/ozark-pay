# Bybit P2P USDT/TJS: read-only аудит

Официальный endpoint: `POST /v5/p2p/item/online`. Документация определяет side=0 как buy и side=1 как sell: [Get Ads](https://bybit-exchange.github.io/docs/p2p/ad/online-ad-list). Пользователь покупает USDT у продавцов, поэтому зелёный Buy UI соответствует seller ads side=1. Канонический источник владельца — первые три обычных объявления официального API в исходном порядке, без отдельной рекламной Featured-карточки, дополнительных фильтров и пересортировки.

Код [bybit_p2p.py](../../app/services/bybit_p2p.py) отправляет tokenId USDT, currencyId TJS, side 1, page 1, size 10. Mean считается Decimal из первых трёх items. Невалидное/дублирующееся объявление не пропускается ради четвёртого; источник становится unavailable. Отдельный featuredItems не участвует. Совпадение рекламной персонализированной выдачи сайта с API не обещается.

## Реальный транспорт

Существующая DPAPI-пара использована только для GET query-api и чтения объявлений. Подтверждены readOnly=1 и доступ Advertising. Ни orders, ни сделки, ни переводы не создавались. В реальном прогоне наблюдался provider code 10006, затем штатное восстановление свежей котировки. Сохранённые обезличенные evidence содержат ID, три исходные цены, fetched/expires timestamps, configuration/version, PostgreSQL observation count и независимое среднее. Никнеймы, ключи и полный ответ провайдера не публикуются.

Живые samples: 9.33 / 9.33 / 9.34 → 9.333333333333 TJS за USDT, PG observations 375 → 377. На публичной Buy USDT/TJS странице независимо проверены зелёный Buy (rgb 6/193/103), отдельный Featured 9.35 и первые обычные цены 9.33 / 9.33 / 9.34. Никнеймы/контакты в UI evidence не сохранены. UI и API наблюдались в разные моменты, поэтому это не обещание неизменных цен/рекламного ранжирования. Это примеры с конкретным UTC-временем в evidence, не действующий курс для будущего платежа. Ошибка 10006 не считается PASS доступности: финансовая котировка на этот момент была недоступна, что отражено в логах.

## Cache, immutable history и отказ

Refresh 30 s, TTL 60 s. Redis lease сериализует refresh. Cached quote повторно проверяется по dimensions, точному AVG3, ID/order, contract, config и времени. PostgreSQL observations immutable; last_success_at служит диагностикой и не подставляется вместо свежей котировки.

12 дополнительных проверок на настоящих Redis/PostgreSQL с synthetic HTTP transport: восемь concurrent refresh → один fetch, normal order/Featured exclusion, Decimal/TTL, persisted observation, запрет SQL tampering, stale rejection, immediate cache removal при rate limit, сохранение last-success только для диагностики, retry recovery, настоящий connection refusal Redis, отсутствие PG financial fallback при утрате cache и новый fetch после восстановления.

Финансовые payment QA используют специально synthetic курс 10.8/10.9/11.0. Он не выдается за Bybit LIVE и не попадает в Production. Наличие живого инфраструктурного парсера не открывает TJ financial admission.

## Секреты и запуск

Windows: owner DPAPI → stdin worker; raw pair отсутствует в Docker Config.Env, argv, Git, образе и архивах. Task Scheduler запускается под owner, Interactive/Limited, без Windows password; supervisor имеет mutex, bounded Docker control и проверяет точную service label. Provider outage даёт unhealthy/retry, а не удаление работающего контейнера.

Linux подготовлен, но на Timeweb не установлен: [file-secret entrypoint](../../scripts/bybit_quote_worker_linux.py), [compose](../../docker-compose.bybit-linux.yml). Проверяются owner UID, 0700 directory, owner-only regular files до 4096 bytes, отсутствие symlink/FIFO/невалидных значений. Файлы открываются через checked dirfd/O_NOFOLLOW. Секреты загружаются в память процесса, не в environment.

Linux QA: non-root 1000, read-only rootfs и secret mount, cap_drop ALL, no-new-privileges, pids 96, без опубликованных портов, ограниченные CPU/RAM, finite restart policy. Проверены write denial на secret mount, Docker healthy, fresh quote и SIGTERM exit 0 без restart. Транспорт в этом Linux-прогоне synthetic; реальные DPAPI-ключи в Linux не переносились.

Стартовые probes получили достаточный бюджет: 384 MiB, до 1 CPU, interval/start_interval 30 s, timeout 30 s, start_period 90 s. Меньший бюджет с частыми probes не был принят как successful health evidence. На сервере 1 vCPU/2 GB нужен отдельный capacity preflight; локальные измерения не являются серверными.

## Условия будущей установки

Использовать существующий OzarkPay backend, выделенные защищённые файлы UID 1000/0400, их родительскую папку 0700, защищённый runtime config и digest image. DPAPI нельзя копировать как Linux secret. Передавать действующую пару только через защищённый административный канал после решения владельца, без raw CLI/env/log values. Compose bind-mount должен быть read-only; нельзя полагаться только на uid/mode remapping file-based Compose secrets: [Docker guidance](https://docs.docker.com/compose/how-tos/use-secrets).

Сетевое разрешение — DNS, собственные PG/Redis и HTTPS к одобренным Bybit hosts. Код ограничивает destination allowlist/TLS/no redirects; host egress firewall/proxy на Timeweb ещё NOT CONFIGURED и относится к Wave5. Не добавлять широкие inbound порты. При 10006, stale, недоступном Redis или несовпадении config финансовый курс закрывается. После трёх startup crashes требуется разбор и явный restart, а не бесконечный restart loop.
