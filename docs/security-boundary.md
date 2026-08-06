# Security and live-data boundary

## Authentication

The client sends a Firebase ID token as `Authorization: Bearer <token>`. The API
verifies signature, issuer, audience/project, expiry, and revocation policy through
Firebase Admin. It extracts the verified `uid`; it never accepts a UID in a header
or body as proof of identity.

Firebase establishes identity only. Household membership, roles, scopes, device
status, agent grants, and resource ownership are server-side authorization checks.

## Authorization

- Every household request resolves active membership before storage access.
- Every storage point operation includes the resolved `householdId` partition key.
- Agent read and write scopes are separate, visible, expiring, and revocable. The
  only A2 scopes are `ledger:read`, `ledger:create`, `assets:read`,
  `assets:update`, `categories:read`, and `channels:read`.
- Revocation must be enforced on the next request, not only at token issuance.
- Internal smoke tooling is never an end-user API and cannot run in Production.

Read-only grants default to 24 hours and are capped at 7 days. A grant containing
either write scope defaults to 1 hour and is capped at 24 hours. Access tokens are
capped at 15 minutes and cannot refresh past the parent grant. Ledger history
updates, permanent delete, member management, settings, migration, export,
authorization management, and audit-log scopes are not issued in A2. Asset update
is limited to one appended snapshot. Remote MCP verifies the same revocable
connection credential on every request and never receives a client-selected
household ID.

Each connection is bound at creation to `api`, `mcp`, or `skill`. Agent HTTP
routes accept only API-bound tokens; Remote MCP accepts only MCP- or Skill-bound
tokens. The persisted write source comes from this server-loaded connection,
never from a write body or MCP tool argument.

Remote MCP prefers the stateless `2026-07-28` wire protocol and temporarily
accepts `2025-11-25` clients at the same endpoint. Protocol negotiation cannot
change credentials, scopes, household selection, rate limits, idempotency, tool
arguments, or audit behavior. The compatibility path owns no separate service or
storage implementation.

The owner may pause or resume an unexpired connection without changing its scopes,
integration, tokens, or expiry; revoked connections cannot resume. Every accepted
Agent request consumes the connection's shared 120-request, 60-second window.
Requests over the limit are rejected and audited. Five invalid-token attempts that
name a known connection within five minutes create one deduplicated anomaly audit
event. Invalid attempts never mutate scopes or permanently lock the valid grant.

## Secrets

Preferred deployed access is Function App managed identity with a least-privilege
Cosmos data-plane role. Firebase and other non-managed credentials use Key Vault or
protected Function App settings. Local development may use ignored files or shell
environment variables.

Never commit or log:

- Cosmos account keys or connection strings
- Firebase service-account JSON or private keys
- Firebase ID tokens, refresh tokens, or decoded full claims
- Agent bearer/refresh tokens
- Apple private keys
- raw financial notes or complete financial documents

## Logging and errors

Allowed structured identifiers: request ID, operation ID, actor UID, household ID,
entity ID/type, status code, latency, and exception type. Amounts, notes, tokens,
full request/response bodies, and credentials are excluded. Public 500 responses
are generic; detailed exceptions remain in protected logs.

## Development Cosmos smoke test

The smoke test is opt-in and may run only when all of these are true:

1. `ANKE_ENVIRONMENT=dev`.
2. `ANKE_COSMOS_ALLOW_SMOKE_WRITE=true`.
3. The configured endpoint account name exactly matches
   `ANKE_COSMOS_EXPECTED_ACCOUNT_NAME`.
4. Database and container names are explicitly configured.
5. The target container is confirmed to use `/householdId`.

The test uses a synthetic household ID prefixed `smoke-dev-` and an item with
`entityType=smokeProbe`, `isSynthetic=true`, and a random run ID. It creates that
single document and point-reads the same ID to verify persistence. It does not
query, update, or delete any other item and intentionally leaves the labeled probe
for auditable cleanup by a separately authorized operation.

The smoke script never prints credentials or full documents. It reports only the
account name, database/container names, run ID, created item ID, request status,
and read-back identity fields.

## Development Firebase smoke test

The Firebase smoke script runs only in Local or Development, reads a short-lived ID
token through hidden terminal input, and verifies it against the configured project.
The token must never appear in command arguments, shell history, committed files,
or logs. The script reports only the project ID and verified UID.

The opt-in end-to-end variant runs only in Development and requires
`ANKE_FIREBASE_ALLOW_SYNTHETIC_USER=true`. It creates a random UID prefixed
`smoke-backend-`, exchanges a custom token for a real Firebase ID token, calls
`/api/v1/me`, and deletes that exact synthetic user in a `finally` block. It never
prints either token or the client API key and it is not evidence for Apple login or
physical-device behavior.

## Production boundary

Production deployment, schema promotion, live-data mutation, container creation,
bulk migration, backup/restore, and secret rotation require separate explicit user
authorization and environment verification. Development success is not Production
evidence.
