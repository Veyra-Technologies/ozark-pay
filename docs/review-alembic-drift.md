# Alembic schema drift review

This review compares three immutable points:

- `origin/main`;
- `origin/release/v2.0.0-rc1` at
  `a4a5506ed8fe56ea9bc2f3ba1a9fb1e41de176e2`;
- `hardening/v2.0.0-rc2`, including migrations `0021` through `0024`.

The comparison was performed against clean PostgreSQL 16 databases. The
hardening checkout now reports:

```text
0024_required_schema_indexes (head)
No new upgrade operations detected.
```

No raw autogenerate output was applied. Migration
`0024_required_schema_indexes` contains only the missing lookup indexes listed
below. It does not alter rows, financial columns, unique constraints, foreign
keys, balances, Rolling, TeamLead accounting, or settlements.

## Classification

The requested categories mean:

- **A** — an object exists in migrations and matches the model;
- **B** — safe naming or index-representation drift;
- **C** — a required constraint or index is actually absent;
- **D** — an unsafe raw-autogenerate recommendation;
- **E** — a reviewed migration is required;
- **F** — intentional model-only metadata needed to describe an existing
  database object.

### Existing uniqueness and companion indexes

The following tables already had both a unique constraint and, where listed in
the historical migration, a companion non-unique lookup index:

- `api_keys.api_key`;
- `refresh_tokens.token_hash` and `refresh_tokens.jti`;
- `kyc_profiles.merchant_id`;
- `aggregator_accounts.api_key` and `aggregator_accounts.name`;
- `aggregator_payments.platform_payment_id`;
- `trader_antiscam_settings.trader_id`;
- `requisite_antiscam_settings.requisite_id`.

Before hardening these were **B** representation differences. Alembic proposed
removing the constraint and replacing the companion index with a unique index.
Those remove/add pairs were **D** because they could create a uniqueness gap and
discard the schema representation established by migrations `0001`, `0002`,
`0007`, and `0008`.

Resolution: the existing constraints and indexes remain unchanged, and the ORM
now names both objects explicitly (**F**, now resulting in **A**). No migration
was needed.

`sms_messages.provider_message_id` is intentionally represented only by its
existing unique constraint. The model no longer requests an unnecessary second
index.

### Actually missing lookup indexes

These sixteen indexes were **C** and **E**. Migration `0024` creates them with
`CREATE INDEX CONCURRENTLY IF NOT EXISTS`:

1. `ix_api_request_logs_merchant_id`
2. `ix_api_request_logs_request_id`
3. `ix_appeal_messages_appeal_id`
4. `ix_appeals_operation_id`
5. `ix_audit_logs_action`
6. `ix_audit_logs_actor_id`
7. `ix_blacklist_kind`
8. `ix_blacklist_value`
9. `ix_deposits_external_id`
10. `ix_fee_rules_merchant_id`
11. `ix_ledger_entries_merchant_id`
12. `ix_ledger_entries_operation_id`
13. `ix_limit_rules_merchant_id`
14. `ix_payouts_external_id`
15. `ix_requisites_trader_id`
16. `ix_webhook_events_merchant_id`

The migration uses an Alembic autocommit block because PostgreSQL forbids
concurrent index creation inside a transaction. Each statement is idempotent,
so an operator may perform a reviewed retry after diagnosing a partial failure.
An automatic blind retry is still prohibited.

### Financial and security-critical objects

| Area | Database evidence | Classification and resolution |
|---|---|---|
| Operation fee snapshot | `uq_operation_fee_snapshots_deposit_id` plus unique `ix_operation_fee_snapshots_deposit_id` exist since `0010` | Raw constraint removal was **D**. Both objects are now explicit model metadata (**F** → **A**). One immutable snapshot per Deposit remains enforced. |
| Fee rules | `ix_fee_rules_entity_scope` and partial unique `uq_fee_rules_active_exact` exist since `0011` | Raw removal was **D**. Indexes and the three `0011` entity checks are now explicit metadata (**F** → **A**). |
| Platform wallet | `ix_platform_crypto_wallets_created_at` exists since `0018` | Raw removal was **D**. It is now explicit metadata (**F** → **A**). |
| Merchant idempotency | Merchant/external and merchant/idempotency unique constraints exist; `0022` adds immutable request fingerprints | **A**. No drift operation remains. |
| Merchant settlement | Pending-settlement and completed network/tx-hash enforcement comes from `0004`/`0021` | **A**. No drift operation remains. |
| Rolling | Constraints, indexes, immutable triggers, and confirmation-flow objects come from `0015`, `0016`, and `0020` | **A**. No drift operation remains. |
| TeamLead | Assignment, accrual, settlement, ledger constraints, and partial unique pending index come from `0017` | **A**. No drift operation remains. |
| Webhook delivery | Lease/due indexes, unique attempt number, and signing-key indexes come from `0023`; merchant lookup comes from `0024` | **A**. No drift operation remains. |

Alembic does not fully compare PostgreSQL trigger/function bodies. Their
presence and downgrade guards must therefore remain covered by migration and
PostgreSQL tests rather than inferred from `alembic check`.

## Local migration evidence

The following isolated PostgreSQL 16 paths succeeded:

- clean database `base -> 0024`;
- `0020_rolling_confirmation_flow -> 0024`;
- allowed `0024 -> 0023` downgrade;
- repeat `0023 -> 0024` upgrade;
- one Alembic head;
- `alembic current` at `0024_required_schema_indexes`;
- `alembic check` with no new operations.

All temporary Compose projects used an `ozark-pay-test-` prefix and were
removed with volumes and orphans after each run.

## Production schema-clone gate remains open

No schema-only clone from the real server was available. Therefore this review
does **not** claim that the production migration is validated. Before any
production migration, an authorized operator must create a read-only schema
export outside this repository:

```bash
export PGOPTIONS='-c default_transaction_read_only=on'
pg_dump --schema-only --format=custom --no-owner --no-privileges \
  --no-comments --no-security-labels \
  --dbname="$OZARK_PRODUCTION_READONLY_URL" \
  --file=/secure/offline/ozark-production-schema.dump
pg_restore --list /secure/offline/ozark-production-schema.dump
sha256sum /secure/offline/ozark-production-schema.dump
```

Restore only into an isolated PostgreSQL instance:

```bash
createdb --dbname="$OZARK_SCHEMA_CLONE_ADMIN_URL" ozark_schema_clone
pg_restore --schema-only --no-owner --no-privileges --exit-on-error \
  --dbname="$OZARK_SCHEMA_CLONE_URL" \
  /secure/offline/ozark-production-schema.dump
DATABASE_URL="$OZARK_SCHEMA_CLONE_ASYNC_URL" \
SYNC_DATABASE_URL="$OZARK_SCHEMA_CLONE_SYNC_URL" \
  alembic current
DATABASE_URL="$OZARK_SCHEMA_CLONE_ASYNC_URL" \
SYNC_DATABASE_URL="$OZARK_SCHEMA_CLONE_SYNC_URL" \
  alembic upgrade head
DATABASE_URL="$OZARK_SCHEMA_CLONE_ASYNC_URL" \
SYNC_DATABASE_URL="$OZARK_SCHEMA_CLONE_SYNC_URL" \
  alembic check
```

The read-only role must have no write privileges. URLs and dump contents must
never be printed, committed, attached to CI artifacts, or placed in the
repository. Production promotion stays blocked until this clone test and the
financial reconciliation gate are independently approved.
