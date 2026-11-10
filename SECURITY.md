# Security boundaries

This is an independently configured self-hosted application, not an accepted public production service. Default binding is loopback; use a dedicated PostgreSQL17 database/role, unique secrets and one supported application worker. Remote hosting requires HTTPS, Secure cookies, exact host/origin configuration, reviewed ingress, operational hardening and your own provider credentials. Never trust identity or connecting-IP headers from an arbitrary request. Private operator and account-login proof-of-concept tooling is excluded.

Email sessions must be active and non-revoked. Private owner actions additionally require the matching current player-ownership relationship; selecting a public tag is not authority. Public sharing is explicit. Mutations require configured same-origin requests. Outbound collection/media are default-off and opt-in; database, job and provider budgets remain part of the supported deployment contract. Multi-worker/distributed collection is unsupported.

The optional model format may execute code when deserialized: use only trusted, integrity-verified artifacts. No model weights are shipped. No telemetry hooks or author-owned service endpoints should be added without review.

Report vulnerabilities privately to the repository owner. Never submit credentials, emails, player/account dumps, invitation/share tokens, real screenshots or session material in issues. See docs/VERIFICATION.md for exact tested scope and unverified browser/provider/production boundaries.
