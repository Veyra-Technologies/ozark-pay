# Alembic drift classification

`alembic check` was run against clean PostgreSQL 16 databases for both the
`origin/main` baseline and this checkout. The detected operations are identical.
Therefore every item below is category **A** (it existed before this branch) and
no item is category **B** (introduced by this branch).

The labels used below are:

- **C**: can affect migrations 0014/0015 or their financial invariants;
- **D**: safe as a target, provided the stated online strategy is used;
- **E**: requires a separately reviewed migration before rollout if the ORM
  model is intended to be canonical.

Do not apply the raw autogenerate output. In particular, a remove/add pair for a
unique index must not create a window without uniqueness.

## Unique-constraint versus unique-index representation

All rows: **A=yes, B=no, C=no, D=conditional, E=yes**. The target uniqueness is
reasonable, but migration must validate duplicates, build the replacement
unique index concurrently, and only then remove redundant enforcement.

1. `aggregator_accounts`: remove `uq_aggregator_accounts_api_key`; change
   `ix_aggregator_accounts_api_key` from non-unique to unique.
2. `aggregator_accounts`: remove `uq_aggregator_accounts_name`; change
   `ix_aggregator_accounts_name` from non-unique to unique.
3. `aggregator_payments`: remove
   `uq_aggregator_payments_platform_payment_id`; change
   `ix_aggregator_payments_platform_payment_id` from non-unique to unique.
4. `api_keys`: remove `api_keys_api_key_key`; change `ix_api_keys_api_key`
   from non-unique to unique.
5. `kyc_profiles`: remove `kyc_profiles_merchant_id_key`; change
   `ix_kyc_profiles_merchant_id` from non-unique to unique.
6. `refresh_tokens`: remove `refresh_tokens_jti_key`; change
   `ix_refresh_tokens_jti` from non-unique to unique.
7. `refresh_tokens`: remove `refresh_tokens_token_hash_key`; change
   `ix_refresh_tokens_token_hash` from non-unique to unique.
8. `requisite_antiscam_settings`: remove
   `uq_requisite_antiscam_settings_requisite_id`; change
   `ix_requisite_antiscam_settings_requisite_id` from non-unique to unique.
9. `sms_messages`: remove `sms_messages_provider_message_id_key`; add unique
   `ix_sms_messages_provider_message_id`.
10. `trader_antiscam_settings`: remove
    `uq_trader_antiscam_settings_trader_id`; change
    `ix_trader_antiscam_settings_trader_id` from non-unique to unique.

## Missing non-unique indexes

All rows: **A=yes, B=no, C=no, D=yes with `CREATE INDEX CONCURRENTLY`,
E=yes**. These are performance/schema-alignment changes and do not belong in
0014/0015.

11. add `ix_api_request_logs_merchant_id`.
12. add `ix_api_request_logs_request_id`.
13. add `ix_appeal_messages_appeal_id`.
14. add `ix_appeals_operation_id`.
15. add `ix_audit_logs_action`.
16. add `ix_audit_logs_actor_id`.
17. add `ix_blacklist_kind`.
18. add `ix_blacklist_value`.
19. add `ix_deposits_external_id`.
20. add `ix_fee_rules_merchant_id`.
21. add `ix_ledger_entries_merchant_id`.
22. add `ix_ledger_entries_operation_id`.
23. add `ix_limit_rules_merchant_id`.
24. add `ix_payouts_external_id`.
25. add `ix_requisites_trader_id`.
26. add `ix_webhook_events_merchant_id`.

## Fee-rule indexes absent from the ORM model

Both rows: **A=yes, B=no, C=no, D=no, E=yes**. Blind removal can weaken fee
selection performance and, for the partial unique index, active-rule
enforcement. Reconcile the intended model first.

27. remove `ix_fee_rules_entity_scope`.
28. remove partial unique `uq_fee_rules_active_exact`.

## Operation fee snapshot uniqueness

29. remove `uq_operation_fee_snapshots_deposit_id`:
    **A=yes, B=no, C=yes, D=no, E=yes**. Rolling depends on one immutable fee
snapshot per Deposit. Any drift migration must preserve that invariant; the
raw removal must not be applied.

## Platform wallet history index

30. remove `ix_platform_crypto_wallets_created_at`:
    **A=yes, B=no, C=no, D=no, E=yes**. This drift belongs to the existing
    `0018_platform_crypto_wallet` schema/model representation and is outside
    the Rolling confirmation refactor. The raw removal must not be folded into
    `0020`.

## Summary by requested category

- **A:** all 30 logical drift rows above (40 Alembic detector messages when
  remove/add index changes are counted separately).
- **B:** none.
- **C:** operation-fee snapshot uniqueness only; no actual 0014/0015 table,
  column, constraint, or index appears in the drift.
- **D:** the sixteen missing non-unique indexes are safe with concurrent
  creation; uniqueness representation changes are only conditionally safe;
  fee-rule and operation-snapshot removals are unsafe.
- **E:** all drift should be handled in one or more separate migrations after
  model intent is confirmed. It must not be folded into 0014/0015.

No anonymized schema-only staging/production clone was available. No production
database connection or write was made.
