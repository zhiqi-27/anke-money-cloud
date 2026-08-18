# Security and live-data boundary

## Authentication

The native iOS client uses ClerkKit for Apple, Google, and email-code sign-in. It
sends the short-lived Clerk session token to `POST /api/v1/auth/clerk/exchange`.
Anke Cloud verifies the Clerk JWT signature, issuer, optional audience, and expiry,
then signs an Anke session token. Protected requests send only that Anke session
token as `Authorization: Bearer <token>`.
The API never accepts a client UID or provider subject as proof of identity.

The Anke session issuer is the only token authority for protected APIs. Household
membership, roles, device status, Agent API Key state, and resource ownership are
server-side authorization checks. Clerk is the identity provider. Apple, Google,
and email-code sign-in are enabled in Clerk. Historical Firebase accounts are not
compatible with the current product.

## Authorization

- Every household request resolves active membership before storage access.
- Every storage point operation includes the resolved `householdId` partition key.
- The single workspace Agent API Key always has the fixed capabilities
  `ledger:read`, `ledger:create`, `assets:read`,
  `assets:update`, `categories:read`, and `channels:read`.
- Revocation must be enforced on the next request, not only at token issuance.
- Internal smoke tooling is never an end-user API and cannot run in Production.

The key does not expire and has no refresh credential. It stays valid until reset,
revocation, or workspace deletion. Ledger history updates, permanent delete,
member management, settings, migration, export, authorization management, and
audit-log operations remain outside the Agent surface. Asset update is limited to
one appended snapshot. Direct Agent HTTP and Remote MCP verify the same revocable
key on every request and never receive a client-selected household ID or source.

`ledger:create` may append a confirmed batch of at most 25 entries. Every entry is
validated and independently idempotent and uses the existing transactional
entity/operation/audit write. The server does not accept the raw source statement,
does not expose ledger update/delete, and does not add a bulk asset operation.
Date-ranged ledger and asset reads remain inside the same household and return an
opaque continuation cursor rather than an unrestricted cross-partition export.

New keys use a compact, URL-safe format containing a compact household locator and
a 192-bit random secret. Previously issued full-capability keys remain valid until
reset or revocation; removed limited-access credentials remain invalid. Neither
format is logged or persisted as plaintext.

Remote MCP prefers the stateless `2026-07-28` wire protocol and temporarily
accepts `2025-11-25` clients at the same endpoint. Protocol negotiation cannot
change credentials, scopes, household selection, rate limits, idempotency, tool
arguments, or audit behavior. The compatibility path owns no separate service or
storage implementation.

The owner may reset or revoke the key. Reset replaces the stored hash and
immediately invalidates the old plaintext; revocation cannot be resumed. Every
accepted Agent request consumes the key identity's shared 120-request, 60-second window.
Requests over the limit are rejected and audited. Five invalid-token attempts that
name the known key identity within five minutes create one deduplicated anomaly
audit event. Invalid attempts never widen capabilities or permanently lock the
valid key.

## Secrets

Preferred deployed access is Function App managed identity with a least-privilege
Cosmos data-plane role. The Clerk secret key and Anke session signing secret use
Key Vault or protected Function App settings. Local development may use ignored
files or shell environment variables.

Never commit or log:

- Cosmos account keys or connection strings
- Anke session signing secrets or session tokens
- Clerk session tokens, secret keys, or private keys
- Agent API Keys or their plaintext
- Apple private keys
- raw financial notes or complete financial documents

APNs signing credentials are protected Function App settings or Key Vault
references. Device tokens are delivery addresses, are never logged, and remain
inside the owner household partition. Background notification payloads contain only
`content-available` and a generic change hint; they exclude household IDs, entity
IDs, amounts, notes, categories, and all other financial content. A notification
never grants access: the app must still present its Anke session to pull changes.

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

## Development Clerk authentication smoke test

The Clerk authentication smoke test is opt-in and accepts a short-lived Clerk
session token through hidden input or a protected local runner. It must never put
session tokens, secret keys, or full user records in command arguments, shell
history, committed files, or logs. It reports only the verified Anke user ID.
Synthetic account creation and deletion are limited to Development and must
target an explicitly synthetic Clerk subject.

## Production boundary

Production deployment, schema promotion, live-data mutation, container creation,
bulk migration, backup/restore, and secret rotation require separate explicit user
authorization and environment verification. Development success is not Production
evidence.
