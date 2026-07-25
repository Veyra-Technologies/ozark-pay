# Ozark Pay production deployment

This runbook deploys the Ozark Pay and Casino release candidate produced by the
Stage 3 package builder. It does not authorize a production deployment.

1. Verify `SHA256SUMS` before extracting or copying any artifact.
2. Provision a dedicated Linux host with Docker Engine and Docker Compose v2.
3. Copy `.env.production.example` to a secrets-managed location outside Git,
   replace every placeholder, and restrict the file to the service operator.
4. Restore the verified encrypted PostgreSQL backups into isolated volumes.
5. Validate both production Compose files with `docker compose config --quiet`.
6. Start PostgreSQL and Redis, apply Alembic migrations, then start application
   services and the authenticated reverse proxy.
7. Execute `SMOKE_TEST.md`; do not create a payment without owner approval.
8. Keep the previous images, Compose files, encrypted backup, and Git metadata
   until the release is accepted.

Never copy a development environment file, local log, plaintext dump, private
backup identity, or workstation credential to the server.
