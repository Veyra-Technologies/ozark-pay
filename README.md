# OzarkPay

OzarkPay is a FastAPI platform for merchant deposits and settlements, trader
operations, country-specific cabinets and audited financial ledgers. This source
snapshot is version **2.0.0-rc2**, with Alembic head
**0037_ge_bank_accounts**. It includes GE card and bank-account requisites,
KZ to GE crossborder operations, RU/KZ merchant USDT TRC20 settlement, and
Rapira/Binance rate parsers.

This is a locally reviewed release candidate. Real bank/payment integration and
production deployment require separate validation. Example settings and synthetic
tests do not establish production readiness.

## Architecture and stack

Python 3.12+, FastAPI/Starlette, Jinja, SQLAlchemy, PostgreSQL 16, Alembic,
Redis 7 and Celery. The API and HTML cabinets share services and server-side
validation. PostgreSQL stores transactional state, immutable financial ledgers
and encrypted bank requisites. Redis supports authentication/rate limits, queues
and Celery results. Worker and beat perform asynchronous and scheduled jobs.
Optional nginx and Prometheus/Grafana/Loki configurations are included.
Dependencies are pinned in `pyproject.toml` and `constraints.txt`.

## Source layout

- `app/api/`: authentication, merchant, trader/cabinet, admin and external APIs.
- `app/web/`, `app/templates/`, `app/static/`: web cabinets and UI assets.
- `app/models/`, `app/schemas/`, `app/db/`: models, contracts and database setup.
- `app/services/`, `app/core/`: ledger, settlement, crossborder, rates and security.
- `app/workers/`: Celery setup.
- `alembic/versions/`: migrations 0001 through 0037.
- `scripts/`: preflight, explicit migrations/bootstrap and backup tools.
- `branding/`, `nginx/`, `monitoring/`, `docs/`: resources and operational guides.

## Local requirements and private configuration

Install Git and Docker Desktop with Docker Compose and a Linux-container engine.
Allow enough RAM and disk space for PostgreSQL, Redis and the Python image. Use
PowerShell on Windows or a POSIX shell; host Python is optional for Docker use.
Run commands from this repository's root, not its enclosing audit workspace.

Copy `.env.example` to an untracked `.env.local`. It contains only placeholders:
replace every `CHANGE_ME` value before starting. Generate independent strong
passwords, a random signing secret, a valid Fernet encryption key and a Base32
TOTP secret. Do not reuse test passwords, API/HMAC keys, JWT secrets or 2FA data.
Keep database passwords consistent with both database URLs and Redis credentials
consistent with all Redis/Celery URLs. Set a local-only Superadmin email.

The existing `scripts/generate_production_env.ps1` generates random credentials
into a private env file. For local HTTP, adapt its generated settings to the
local values described in `.env.example`; do not run production settings blindly.
The `.env.production.example` template is for a separately approved deployment.

`ENV=local` and `DEBUG=true` in `.env.local` permit local HTTP session cookies.
Use `HSTS_ENABLED=false`, local CORS/CSRF/trusted hosts and
`APP_ENV_FILE=.env.local`. These settings must not be used for a public server.
Keep XE quota at zero until external credentials and a purchased quota have
been configured. No real secret belongs in an example file.

## Start a new isolated local installation

Use the explicit project name `ozarkpay-local` throughout. The commands below
create a fresh installation; do not point them at an existing working database
or use a different project's volumes. Keep any existing installation unchanged.

```bash
docker compose -p ozarkpay-local -f docker-compose.production.yml -f docker-compose.local.yml --env-file .env.local up -d --build db redis
docker compose -p ozarkpay-local -f docker-compose.production.yml -f docker-compose.local.yml --env-file .env.local --profile migrate run --rm migrate
docker compose -p ozarkpay-local -f docker-compose.production.yml -f docker-compose.local.yml --env-file .env.local run --rm api python -m scripts.bootstrap_superadmin
docker compose -p ozarkpay-local -f docker-compose.production.yml -f docker-compose.local.yml --env-file .env.local up -d --build api worker beat
```

The API binds only to `127.0.0.1:8000`. Visit `/staff/login`, `/trader/login`
or `/merchant/login` on `http://127.0.0.1:8000`. Bootstrap credentials and TOTP
come from the private env file and remain outside Git. Bootstrap is an explicit
initialization step, not part of normal startup. Demo scripts are excluded from
this publication; keep `SEED_DEMO_DATA=false`. Stop this named installation
with `docker compose ... stop`; deleting volumes would delete application data.

Check `/version`, `/health` and `/ready`. API, worker and beat do not run
migrations or seeds on startup. Demo seeding is disabled by default.

## Migrations

The single expected head is `0037_ge_bank_accounts`. Check `alembic heads` in
an environment with the application dependencies and private configuration.
Run migrations through the explicit `migrate` service or `python -m
scripts.migrate`. Back up and verify the target database before changing schema.
Never run migration commands against an ambiguous database URL.

Keep existing revisions immutable and add a new revision with the correct
`down_revision`. Test both a fresh database and an upgraded backup. Financial
history must not be silently rewritten. Migration 0037 permits GE bank accounts;
its downgrade refuses to discard newly created account requisites.

## Tests and lightweight checks

The complete final image regression preceding this publication passed **516 tests**. This is a recorded local result, not a claim that this migration reran
all integration tests or exercised real bank transfers. The publication changes
repository hygiene, local launch documentation and safe configuration examples.

At the owner's request this deployment-oriented publication excludes the entire
`tests` directory, synthetic fixtures/golden snapshots, demo seed scripts,
recovery rehearsal, mock services and test-only CI. They remain in the original
local QA workspace and are not copied to GitHub or into Docker build context.
The 516-test result above is the prior acceptance result for the same application
logic. No new full integration run was performed for the documentation-only
publication preparation.

With installed dependencies and private configuration, lightweight checks are:

```bash
python -m compileall -q app scripts alembic
alembic heads
```

Use an independently maintained, isolated QA environment for regression testing;
do not create test accounts or execute synthetic financial scenarios on the
server's working database.

## API and security contracts

JSON APIs use the `/api/v1` prefix. Authentication and role/ownership checks apply
on the server; cabinet visibility alone does not grant API permission. Merchant
payment requests use canonical HMAC v2 with timestamps, replay protection and
idempotency. GE bank-requisite create/edit validates country, currency, bank,
Luhn or IBAN/MOD97 and stores encrypted values. Lists display masked requisites;
full-value access requires a specific authorized action.

Enable `DOCS_ENABLED` and `OPENAPI_ENABLED` only in an appropriate private local
environment to inspect the route schema. Consult:

- [Merchant integration](docs/merchant-integration.md)
- [Merchant HMAC v2](docs/merchant-hmac-v2.md)
- [Crossborder contract](docs/crossborder-contract.md)
- [Webhook integration](docs/webhook-integration.md)
- [Environment variables](docs/ENVIRONMENT_VARIABLES.md)
- [Backup and restore](docs/BACKUP_RESTORE.md)
- [Encrypted backup policy](docs/ENCRYPTED_BACKUP_POLICY.md)
- [Security policy](SECURITY.md)
- [Git workflow and source recovery](docs/github-workflow.md)

Older release and audit documents describe their dated scope. The migration head
and feature set at the top of this README identify the current source snapshot.

## Repository hygiene and current limits

This repository is public by the owner's instruction. Never commit working env
files or real account credentials. Keep signing and encryption material,
Superadmin access files, TOTP values or QR codes, payment/person data, uploads,
database dumps, local databases, Docker volumes, virtual environments or QA
artifacts. `.gitignore` does not protect a secret already in Git history. Review
the staged file list and scan changes before pushing. Backups and encryption keys
are maintained privately and are required in addition to Git for data recovery.

Real bank transfers, ownership/existence of cards or IBANs, external payment
processor readiness and production operational controls remain outside the
local validation. AI Office is disabled by default and cannot issue payment
commands. A known pre-existing staff profile formatter can display a GEL
min/max range incorrectly; the stored numbers and server-side limits remain
correct. See the local acceptance evidence kept outside this source repository.
