# AI Office discovery (public summary)

The review performed only static, read-only discovery. No AI Office integration
was implemented, no service was started or restarted, and no production request,
migration, or write was performed.

The inspected source describes a separate web application with persistent
storage and asynchronous infrastructure. The locally available runtime metadata
was not sufficient to verify the health, ownership, production identity, or
external availability of any deployment. A stopped local environment must not
be treated as evidence of a working production AI Office.

The current Ozark Pay branch intentionally contains no runtime connection to AI
Office. A future integration requires a separately reviewed change set with:

- a versioned machine API rather than browser session routes;
- service authentication, request signing, replay protection, and scoped
  authorization;
- immutable event and command identifiers with idempotent processing;
- explicit schemas for payment lifecycle and reconciliation events;
- bounded retries, dead-letter visibility, rate and payload limits, and
  security-safe logging;
- independent storage and an outbox boundary; neither product may write to the
  other product's database.

AI Office availability must never block Deposit lifecycle, trader settlement,
Rolling recovery, webhooks, or aggregator callbacks. Any command flowing back
to Ozark Pay must pass through an authenticated, authorized, idempotent, and
audited Ozark API.

Operational discovery details are intentionally excluded from this public
document. They must be kept in an approved private operations repository or
another access-controlled system, never in public Git history.
