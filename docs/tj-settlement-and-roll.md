# TJ Settlement and existing Roll contract

TJ execution remains closed in production at both service and PostgreSQL levels.
Only the isolated financial QA dependency, named QA database, QA database role
and transaction marker can exercise these paths. No environment flag alone is
sufficient. There is no wallet SDK, automatic blockchain verifier or transfer
sender in these changes.

## Settlement

`app/services/settlements.py` reuses the RU/KZ country-scoped request lifecycle.
The Merchant requests USDT to a checksum-valid TRON address. Fee is exactly
5 USDT **in addition** to the requested payout: 100 payout reserves/debits 105.
An available balance of 104 cannot cover that request. No FX conversion applies.

Request -> pending -> approved -> sent -> completed. A sent payment can enter
reconciliation. Rejection before sending releases the entire reserve once;
verified-not-sent reconciliation requires its existing manual evidence.
Entering a 64-hex TXID records staff evidence, never blockchain confirmation.
Country, Merchant owner, business, balance, amounts, network and quote snapshots
remain fixed. Concurrent requests serialize on the same balance; one active
request and Merchant idempotency keys are also protected by database indexes.

Staff actions use existing Admin/SuperAdmin RBAC and verified staff sessions,
2FA, CSRF and audit events. TJ has its own staff queue. Merchant history contains
payout, fee, total reserve/debit, address, status and TXID. Display uses two
decimal places; journal precision is unchanged.

Forward migration `0044_tj_settlement` expands the existing USDT contract and
adds a separate production admission guard. Downgrade with TJ history is blocked.

## Roll

Owner approval for TJ: preserve the existing paid-Deposit FIFO contract.
There is **no additional Roll percentage** and no Settlement fee in Roll.

The reused entry points in `app/services/rolling.py` are:

- [`register_rolling_transfer`](../app/services/rolling.py#L349): staff records evidence of the platform advance;
  this creates pending confirmation, with no debt or Merchant balance credit.
- [`confirm_rolling_transfer`](../app/services/rolling.py#L488): only that Merchant confirms actual receipt;
  principal and outstanding obligation increase exactly once.
- [`_eligible_transfer_sequence`](../app/services/rolling.py#L678): only funding confirmed before Deposit creation
  is eligible; later topups cannot retroactively finance an earlier Deposit.
- [`apply_merchant_financing`](../app/services/rolling.py#L853): existing locks, FIFO transfer consumption,
  obligation reduction, overflow credit and immutable journal lifecycle.
- [`release_pending_allocation`](../app/services/rolling.py#L1200) / [`reopen_released_allocation`](../app/services/rolling.py#L1251): unsuccessful
  payment and appeal lifecycle retain the same eligibility snapshot.
- [`reverse_paid_allocation`](../app/services/rolling.py#L1352): compensating FIFO consumption restores the debt
  and debits only the overflow actually credited to the Merchant.
- [`reconcile_rolling_account`](../app/services/rolling.py#L1627): independent transfers, consumptions, account
  totals and local split reconciliation.

TJ [`create_tj_pending_allocation`](../app/services/rolling.py#L119) adapts the immutable TJS fee snapshot and
Bybit quote into that same allocation model. The percentage comes from
`OperationFeeSnapshot.merchant_rate_percent`, assigned through the existing
individual Merchant tariff by Admin/SuperAdmin. No country/default fallback or
Roll tariff is introduced.

For a paid Deposit, net TJS = amount minus the snapshotted Merchant fee.
Net USDT is the existing immutable eight-place financial snapshot. Recover
`min(net USDT, eligible remaining advance)` in FIFO order. Credit exactly
`net USDT - recovered USDT` to the Merchant's USDT balance. The local-currency
split is evidence; it does not replace that exact USDT conservation equation.
TJ quote precision remains twelve places, local evidence two, USDT eight.
Existing RU computations retain six-place USDT and their original RUB rounding.

`0045_tj_rolling_admission` adds explicit TJS evidence columns, widens storage
without changing existing values, and extends independent Trader/Aggregator
accounting to require **Merchant net credit + net Roll recovery = net payable**.
Deferred guards also compare account aggregates, confirmed funding journals,
transfer consumptions, paid splits and immutable fee/quote snapshots.

Funding alone creates no Deposit, no executor collateral movement, no Merchant
settlement credit and no platform margin. Trader and external Aggregator routes
repay only through an independently confirmed paid Deposit. A direct Merchant
transfer to a Trader/Aggregator is **not** sufficient repayment evidence; that
separate contract remains closed. Refund is an accounting inverse, not a bank
or blockchain transfer. Reversal cannot spend unavailable Merchant overflow.

## Deployment and rollback boundary

This branch is for independent Wave 4 review. Do not deploy or merge based on a
partial test result. Production TJ Deposit, Settlement and Roll admission remain
closed. No production data, users, credentials, RU/KZ tariff or parser assignment
is rewritten by these forward migrations.

Before any later deployment: verify the signed release/digest, create and restore
a database backup in isolation, compare credentials/HMAC dialect, bank details,
ledger and saved financial snapshots, then run migration and financial acceptance
against that copy. Schedule production changes separately after owner approval.

Application rollback must keep schema support and admission guards for any new
TJ history. Downgrade refuses TJ Settlement/Roll history or precision loss.
Restore a backup only as a coordinated database/application recovery, never as
an automatic destructive response to a failed healthcheck.
