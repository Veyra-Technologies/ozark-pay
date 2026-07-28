# Ozark Pay 2.0.0-rc1 release runbook

`2.0.0-rc1` is a release candidate. Do not label it `2.0.0` production
release until manual QA is signed off.

This runbook documents operator commands. It does not authorize automatic
backup, deployment, production/staging migration, or restart.

## Scope

- Rolling and strict Rapira behavior from migrations 0014–0016.
- TeamLead accounting from migration 0017.
- Versioned display-only platform USDT TRC20 wallet in migration 0018.
- Disabled-by-default AI Office configuration and manual GET health test in
  migration 0019.

The wallet does not watch TRON, detect deposits, credit balances, or assign
personal addresses. AI Office does not receive events, issue commands, access
the database, run antifraud, or mutate payments in this release candidate.

## New environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APP_VERSION` | `2.0.0-rc1` | Public application version returned by `/version` and `/health`. |

AI Office credentials are not environment variables. They are entered by a
2FA-authenticated Superadmin and stored encrypted with the existing
`ENCRYPTION_KEY`. Do not place them in source, fixtures, documentation, or
command-line arguments.

The canonical application environment variable remains `ENV`
(`local`, `staging`, or `production`). The preflight reports it as `APP_ENV`
without printing connection URLs.

## 1. Backup before migration

Use the existing encrypted backup workflow and the production environment
file selected by the release operator:

```bash
export BACKUP_ENCRYPTION_PASSPHRASE='set-through-the-secret-manager'
./scripts/backup_encrypted.sh
```

Never paste the passphrase into a ticket or terminal transcript. Record the
generated backup path, checksum, PostgreSQL version, operator, and timestamp in
the release record.

## 2. Verify the backup

Verify checksum and perform the documented isolated restore test:

```bash
sha256sum /approved/backup/path/ozark-pay-*.dump.enc
./scripts/verify_encrypted_backup_restore.sh /approved/backup/path/ozark-pay-*.dump.enc
```

The restore target must be an isolated disposable PostgreSQL instance, never
production or staging.

## 3. Pre-migration checks

Run the read-only preflight against the currently running service:

```bash
python -m scripts.preflight_v2 --base-url http://127.0.0.1:8000
```

It checks environment safety, DB/Redis connectivity, a single Alembic head,
current revision, required secrets without printing them, Rapira live ask,
AI configuration, active wallet count, writable directories, health and
readiness. It never runs migrations or creates payments.

Confirm separately:

```bash
alembic heads
alembic current
```

Before this release, both must report `0017_teamlead`.

## 4. Upgrade 0017 → 0018 → 0019

After backup approval and during the approved change window:

```bash
alembic upgrade 0018_platform_crypto_wallet
alembic current
alembic upgrade 0019_ai_office_config
alembic current
```

Expected final revision:

```text
0019_ai_office_config (head)
```

Do not use `alembic upgrade head` without first verifying that the repository
has exactly one expected head.

## 5. Service start order

Start or replace services in this order:

1. PostgreSQL and Redis; wait for their health checks.
2. API; wait for migration completion and `/health`.
3. Celery worker.
4. Celery beat.
5. nginx/reverse proxy.
6. Monitoring services only if their configuration changed.

Example Compose sequence; exact files and environment file must be selected by
the release operator:

```bash
docker compose up -d db redis
docker compose up -d api
docker compose up -d worker beat
docker compose up -d nginx
```

Do not use `--no-cache` during the release window unless a separately approved
incident procedure requires it.

## 6. Health and readiness

```bash
curl --fail --silent http://127.0.0.1:8000/health
curl --fail --silent http://127.0.0.1:8000/ready
curl --fail --silent http://127.0.0.1:8000/version
```

Expected version is `2.0.0-rc1`. Readiness must report both PostgreSQL and
Redis as `ok`.

## 7. Post-deploy GET-only smoke

```bash
python -m scripts.smoke_v2 --base-url http://127.0.0.1:8000
```

The script uses only GET for `/health`, `/ready`, `/version`, and
`/staff/login`. It checks `/openapi.json` only when `OPENAPI_ENABLED=true`.
Production should normally keep docs and OpenAPI disabled.

## 8. Celery verification

```bash
docker compose ps api worker beat
docker compose logs --since 10m worker beat
```

Confirm that worker and beat stay running, scheduled tasks execute, and no
finance invariant errors are introduced by the release.

## 9. Rapira verification

The preflight must report a fresh `USDT/RUB`, `side=ask`,
`source=rapira_live` quote. `provider_timestamp` may be null when
`freshness_basis=fetched_at`. No static, stale, last-success, bid, last, or
close fallback is valid for Rolling or TeamLead.

## 10. Webhook backlog

Use a read-only query:

```sql
SELECT status, count(*)
  FROM webhook_events
 GROUP BY status
 ORDER BY status;
```

Investigate an unexpected increase in queued/failed deliveries before
continuing. Do not bulk-retry during the release without a separate decision.

## 11. Rollback and forward-fix boundary

Schema downgrade to 0017 is allowed only before any wallet history or AI
configuration is created:

```bash
alembic downgrade 0017_teamlead
```

Migration 0018 refuses downgrade when any wallet history exists. Migration
0019 refuses downgrade when any AI Office configuration exists. These guards
prevent deleting operational configuration and audit history.

After Rolling or TeamLead financial data exists, do not destructively
downgrade migrations 0015–0017. Keep the additive 0018/0019 schema and use an
application forward-fix. The same forward-fix rule applies after wallet or AI
configuration has been saved.

Never bypass downgrade guards by dropping tables, editing `alembic_version`,
disabling triggers, or manually deleting records.

## 12. Release completion

Do not promote `2.0.0-rc1` to `2.0.0` until:

- automated PostgreSQL and regression suites pass;
- the checklist in `docs/manual-qa-v2.0.md` is signed off;
- backup restore evidence is attached;
- the release owner approves production activation;
- AI Office remains disabled in production until M2M authentication and
  permissions are separately approved.
