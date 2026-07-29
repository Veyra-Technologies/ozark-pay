# Ozark Pay

Ozark Pay is a FastAPI payment-processing platform for merchant deposits,
settlements, trader operations and audited finance workflows. This repository
contains the `2.0.0-rc2` release candidate for independent review. It is not a
production release and must not be deployed without a separate security,
financial and operational approval.

## Release candidate highlights

- settle is the base merchant accounting model;
- merchant-confirmed Rolling funding with immutable evidence and FIFO recovery;
- immutable Deposit lifecycle and expiry handling;
- TeamLead attribution, accrual, settlement and platform-expense accounting;
- strict live Rapira ask quotes for Rolling and merchant settle;
- canonical Merchant API HMAC v2 with replay protection and retry-safe
  idempotency;
- leased webhook delivery, terminal dead letters, and independent rotating
  signing keys;
- versioned display-only USDT TRC20 wallet and QR rendering;
- disabled-by-default AI Office settings;
- PostgreSQL migrations through `0024_required_schema_indexes`;
- release preflight, smoke checks and PostgreSQL regression tests.

See [Ozark Pay 2.0.0-rc2 release notes](docs/release-v2.0.md) for the complete
review scope and known limitations.

## Architecture

At a high level, the application consists of:

- a FastAPI API and server-rendered Jinja cabinets;
- PostgreSQL for transactional state and immutable financial ledgers;
- Redis and Celery for queues and scheduled lifecycle work;
- optional nginx and monitoring services;
- Alembic for versioned schema changes.

AI Office integration is outbound-only in this release candidate, is disabled
by default, and cannot issue payment commands.

## Local review

Prerequisites:

- Docker with Docker Compose;
- Git;
- PowerShell on Windows or a POSIX shell on Linux/macOS.

Create a local, untracked environment file from
`.env.production.example`. Replace every `CHANGE_ME` placeholder with a new
local-only value, set the environment for local use, and never reuse example or
test credentials outside an isolated development environment.

The production Compose definition may be reused only with isolated local Docker
volumes during review. Do not point it at an existing database. Start the local
dependencies, run the explicit migration job, optionally bootstrap the first
Superadmin, and then run the API on port 8000 without nginx:

```bash
docker compose \
  -f docker-compose.production.yml \
  --env-file .env.production \
  up -d db redis

docker compose \
  -f docker-compose.production.yml \
  --env-file .env.production \
  --profile migrate run --rm migrate

docker compose \
  -f docker-compose.production.yml \
  --env-file .env.production \
  run --rm api python -m scripts.bootstrap_superadmin

docker compose \
  -f docker-compose.production.yml \
  --env-file .env.production \
  run --rm -p 8000:8000 api
```

The normal `api`, `worker`, and `beat` commands never run migrations or seed
data. `scripts.seed` is restricted to local/development/test demo data and is
never part of application startup.

After startup, verify `/version`, `/health` and `/ready`. The expected version is
`2.0.0-rc2`.

## Tests

The test workflow creates an isolated Compose project and always tears it down:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_tests_workflow.ps1
```

or:

```bash
bash ./scripts/run_tests_workflow.sh
```

Useful read-only validation commands:

```bash
python -m compileall -q app scripts
alembic heads
git diff --check
```

The expected single Alembic head is
`0024_required_schema_indexes`. Run migrations only against an explicitly
selected disposable or approved local database. Migration execution against
staging or production is outside the scope of this repository publication.

## Environment variables

The safe template is `.env.production.example`. Important variable names
include:

- `APP_VERSION`, `ENV`, `DEBUG`;
- `SECRET_KEY`, `ENCRYPTION_KEY`;
- `DATABASE_URL`, `SYNC_DATABASE_URL`;
- `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`;
- `SUPERADMIN_EMAIL`, `SUPERADMIN_PASSWORD`, `SUPERADMIN_2FA_SECRET`;
- `CORS_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `TRUSTED_HOSTS`;
- `RAPIRA_RATES_ENABLED`, `RAPIRA_RATES_URL`;
- `DOCS_ENABLED`, `OPENAPI_ENABLED`, `METRICS_ENABLED`;
- `GRAFANA_ADMIN_PASSWORD`.

Values from example and test files are placeholders or test-only credentials.
They must never be used as real secrets.

## Review documentation

- [Rolling confirmation and FIFO accounting](docs/rolling.md)
- [TeamLead accounting](docs/teamlead.md)
- [Manual QA checklist](docs/manual-qa-v2.0.md)
- [Alembic schema alignment](docs/review-alembic-drift.md)
- [Merchant integration](docs/merchant-integration.md)
- [Merchant HMAC v2](docs/merchant-hmac-v2.md)
- [Webhook integration](docs/webhook-integration.md)
- [Security policy](SECURITY.md)

No backup, database dump, private discovery, local environment file or real
credential belongs in Git history. Follow the
[private vulnerability-reporting policy](SECURITY.md) rather than opening a
public issue with sensitive data.
