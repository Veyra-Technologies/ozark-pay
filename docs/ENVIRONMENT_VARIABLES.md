# Production environment variables

Use the supplied `.env.production.example` files as schemas only. Every
placeholder must be replaced through the production secrets manager or a
mode-`0600` environment file outside Git.

Required secret groups:

- application signing, refresh-token, and at-rest encryption keys;
- PostgreSQL and Redis credentials and connection URLs;
- superadmin bootstrap credentials and two-factor seed for Ozark Pay;
- payment and game callback secrets for Casino;
- provider credentials stored through the supported encrypted application
  configuration flow;
- metrics access token and Grafana administrator password when monitoring is
  explicitly enabled.

Required public configuration includes canonical HTTPS URLs, trusted hosts,
CORS origins, reverse-proxy ranges, exposed proxy ports, mail sender identity,
and operational limits. Production must keep debug, reload, source bind mounts,
demo seeding, sandbox keys, and mock providers disabled.
