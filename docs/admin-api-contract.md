# Anke Money Admin API contract

Status: draft v0.1 · 2026-08-30  
Owner: `anke-money-cloud`  
Related client: future `anke-money-admin` web application

This document defines the first administrative contract for granting and
revoking Anke Money Pro access for one account at a time. It is a contract and
implementation boundary, not a production deployment or a permission change.

## 1. Scope

The administrative API provides a narrow, auditable operator surface for:

- finding an Anke account by Anke UID, Clerk subject, email, or display name;
- reading the account's effective Pro entitlement and its contributing sources;
- creating a time-bounded or explicitly lifetime manual Pro grant;
- revoking a manual grant without changing Apple transaction evidence;
- reading administrative audit events and small operational counters.

It does not provide access to ledger entries, assets, notes, household contents,
API keys, Clerk credentials, or arbitrary Cosmos documents. It never accepts a
caller-selected `householdId` as an authorization input.

The user API remains the public application boundary:

- `POST /api/v1/billing/apple/verify` verifies a signed Apple transaction;
- `GET /api/v1/billing/entitlement` returns the signed-in account's effective
  entitlement;
- `POST /api/v1/billing/apple/notifications` processes App Store Server
  Notifications V2.

The existing Apple subscription documents remain provider evidence. Manual
access is stored separately and is never represented by a fabricated Apple
transaction ID.

## 2. Authorities and terminology

| Term | Contract meaning |
| --- | --- |
| `uid` | Stable Anke identity ID, currently `clerk:<Clerk subject>`. |
| `householdId` | Server-resolved owner workspace ID. It is never supplied by the browser for authorization. |
| Apple entitlement | A server-verified `appleSubscription` record derived from StoreKit/App Store Server data. |
| Manual grant | An operator-created `manualProGrant` record with an explicit source, period, reason, and audit trail. |
| Effective entitlement | The union of currently active Apple entitlements and currently active manual grants. |
| Admin identity | A verified Clerk identity that passes the server-side admin allowlist/role check. |

The Azure service remains authoritative for cloud synchronization and Anke Skill
access. The iOS `isPro` or StoreKit state is not an authorization authority.

## 3. Administrative authentication

### Request credential

Every `/admin/v1/*` request requires:

```http
Authorization: Bearer <short-lived Clerk session token>
```

The cloud verifies the Clerk JWT signature, issuer, audience (when configured),
and expiry using the existing Clerk verifier. It then checks the verified
Clerk subject against the production admin policy before reading or mutating any
account.

The first single-maintainer implementation uses an exact subject allowlist in a
protected Function App setting/Key Vault reference, for example
`ANKE_ADMIN_CLERK_SUBJECTS`. An empty or missing allowlist fails closed. The
allowlist is separate for Development and Production. A future multi-operator
version may replace the allowlist with a Clerk Organization role or Microsoft
Entra ID role, but the endpoint shape does not change.

The regular Anke session token is not sufficient by itself for administration.
The admin web application must keep the Clerk session in an HttpOnly server-side
session/BFF boundary and must never expose a Clerk secret key or Cosmos
credential to the browser.

### Authentication responses

- `401 admin_auth_required`: missing, malformed, expired, or invalid Clerk
  session token.
- `403 admin_forbidden`: valid Clerk identity, but not an approved admin.
- `429 admin_rate_limited`: operator or IP request limit exceeded; include
  `Retry-After`.

Responses do not reveal whether an arbitrary UID exists when the caller is not
an admin.

## 4. Common protocol rules

- Base path: `/admin/v1`.
- JSON request and response bodies use the existing camelCase convention.
- Timestamps are ISO-8601 UTC strings with an explicit `Z` suffix.
- `limit` defaults to 25 and is capped at 100. `cursor` is opaque and must not
  be interpreted by the client.
- Mutating requests require a UUID `Idempotency-Key` header. Replaying the same
  key and request body returns the original result; reusing it with a different
  body returns `409 idempotency_conflict`.
- The existing `X-Request-ID` response header remains the correlation ID. The
  request ID, operator UID, target UID, action, outcome, and reason may be
  logged; tokens, financial data, and full request bodies may not.
- The API is intended to be called by the admin web BFF, not directly from an
  arbitrary browser origin. CORS is denied by default; if a deployed web origin
  is later allowed, it must be an explicit environment setting.

## 5. Resource model

### 5.1 `manualProGrant`

Manual grants are stored in the existing `anke_identities` container, whose
partition key is `/uid`. The document has its own ID and does not share the
`appleSubscription` entity type:

```json
{
  "id": "pro-grant:7a3bd1f0-4d7b-4e7a-a084-8a2ee3221d41",
  "entityType": "manualProGrant",
  "uid": "clerk:user_123",
  "householdId": "server-resolved-household-uuid",
  "source": "manual",
  "grantType": "fixedTerm",
  "startsAt": "2026-08-30T00:00:00Z",
  "expiresAt": "2026-09-29T23:59:59Z",
  "revokedAt": null,
  "reason": "Beta tester access",
  "createdBy": "clerk:admin_456",
  "createdAt": "2026-08-30T06:00:00Z",
  "updatedAt": "2026-08-30T06:00:00Z"
}
```

Rules:

- `uid`, `householdId`, `createdBy`, timestamps, and the stable grant ID are
  server-owned.
- `grantType` is `fixedTerm` or `lifetime`. A lifetime grant has
  `expiresAt: null` and requires a separate confirmation in the UI.
- `startsAt` must not be before the account's creation/known identity time and
  must include a timezone. For `fixedTerm`, `expiresAt` must be after
  `startsAt`; the initial maximum term is 366 days.
- `reason` is required, 1–240 Unicode characters, and is displayed in the
  administrative history. It must not contain secrets or financial notes.
- A grant is append-only. Extension creates another grant; revocation only sets
  `revokedAt` and records a new audit event. Existing Apple documents are never
  edited by an admin action.
- Account deletion removes manual grants together with the identity and Apple
  subscription records, subject to the existing deletion contract.

### 5.2 Effective entitlement

The service calculates effective Pro access at read time:

```text
active Apple subscription
OR active manualProGrant
```

A source is active only when it is not revoked and its start time has passed. An
expiration is inclusive of neither the exact expiration instant nor later time:
`now < expiresAt`. A manual grant must not be overwritten or deactivated by an
Apple webhook. The same aggregation is used by:

- `GET /api/v1/billing/entitlement`;
- `CloudService` Pro checks;
- `AgentAccessService` Pro checks;
- all administrative read endpoints.

The current `ProEntitlementView` can remain backward-compatible while gaining an
optional `source`/`sources` field. Admin responses always include the source
breakdown.

## 6. Endpoints

### 6.1 `GET /admin/v1/overview`

Returns small, non-financial operational counters:

```json
{
  "activeProAccounts": 12,
  "activeManualGrantAccounts": 3,
  "manualGrantsExpiringWithinDays": 2,
  "recentAdminActions": 8,
  "generatedAt": "2026-08-30T06:00:00Z"
}
```

The counts are accounts/identities, not ledger or household money metrics.

### 6.2 `GET /admin/v1/users`

Searches identity metadata only. It never returns ledger or asset content.

Query parameters:

| Parameter | Required | Rule |
| --- | --- | --- |
| `q` | yes | 1–120 characters; exact UID/Clerk subject matches are preferred, then normalized email/display-name matches. |
| `status` | no | `all`, `pro`, `free`, or `manualGrant`. Default `all`. |
| `limit` | no | 1–100; default 25. |
| `cursor` | no | Opaque continuation cursor. |

Response:

```json
{
  "items": [
    {
      "uid": "clerk:user_123",
      "displayName": "Anke friend",
      "email": "person@example.com",
      "provider": "clerk",
      "createdAt": "2026-08-01T10:00:00Z",
      "effectiveEntitlement": {
        "active": true,
        "sources": ["manualGrant"],
        "expiresAt": "2026-09-29T23:59:59Z"
      }
    }
  ],
  "nextCursor": null
}
```

The identity membership projection must retain normalized email and display
name when available so the admin search is deterministic. If a user has not
yet completed Anke identity initialization, the endpoint may return a Clerk
identity without a household and that account cannot receive a grant until the
server identity is ready (`409 target_not_ready`).

### 6.3 `GET /admin/v1/users/{uid}`

Returns one account profile and a compact entitlement summary:

```json
{
  "uid": "clerk:user_123",
  "displayName": "Anke friend",
  "email": "person@example.com",
  "provider": "clerk",
  "householdReady": true,
  "createdAt": "2026-08-01T10:00:00Z",
  "effectiveEntitlement": {
    "active": true,
    "sources": ["apple", "manualGrant"],
    "expiresAt": "2026-09-29T23:59:59Z"
  }
}
```

`uid` is URL-encoded by the client. The server resolves the target household
from the identity membership and never trusts a household ID from the path or
body.

### 6.4 `GET /admin/v1/users/{uid}/entitlement`

Returns the full provider/source breakdown needed by the detail page:

```json
{
  "uid": "clerk:user_123",
  "effective": {
    "active": true,
    "sources": ["manualGrant"],
    "expiresAt": "2026-09-29T23:59:59Z"
  },
  "appleSubscriptions": [],
  "manualGrants": [
    {
      "id": "pro-grant:7a3bd1f0-4d7b-4e7a-a084-8a2ee3221d41",
      "grantType": "fixedTerm",
      "startsAt": "2026-08-30T00:00:00Z",
      "expiresAt": "2026-09-29T23:59:59Z",
      "revokedAt": null,
      "reason": "Beta tester access",
      "createdBy": "clerk:admin_456",
      "createdAt": "2026-08-30T06:00:00Z"
    }
  ]
}
```

Apple transaction IDs are shown only to an authenticated admin and are not
accepted as input to any manual-grant operation.

### 6.5 `POST /admin/v1/users/{uid}/manual-pro-grants`

Creates one manual grant. Required header:

```http
Idempotency-Key: 2c3e7ae7-bdd1-4781-b8de-86f0b0d9c8b9
```

Request:

```json
{
  "grantType": "fixedTerm",
  "startsAt": "2026-08-30T00:00:00Z",
  "expiresAt": "2026-09-29T23:59:59Z",
  "reason": "Beta tester access"
}
```

For a lifetime grant:

```json
{
  "grantType": "lifetime",
  "startsAt": "2026-08-30T00:00:00Z",
  "expiresAt": null,
  "reason": "Founder account"
}
```

Response `201 Created`:

```json
{
  "grant": {
    "id": "pro-grant:7a3bd1f0-4d7b-4e7a-a084-8a2ee3221d41",
    "uid": "clerk:user_123",
    "grantType": "fixedTerm",
    "startsAt": "2026-08-30T00:00:00Z",
    "expiresAt": "2026-09-29T23:59:59Z",
    "revokedAt": null,
    "reason": "Beta tester access",
    "createdBy": "clerk:admin_456",
    "createdAt": "2026-08-30T06:00:00Z"
  },
  "effectiveEntitlement": {
    "active": true,
    "sources": ["manualGrant"],
    "expiresAt": "2026-09-29T23:59:59Z"
  },
  "replayed": false
}
```

Errors include `404 target_not_found`, `409 target_not_ready`,
`409 idempotency_conflict`, `422 invalid_grant_period`,
`422 invalid_grant_reason`, and `503 entitlement_storage_unavailable`.

### 6.6 `POST /admin/v1/users/{uid}/manual-pro-grants/{grantId}/revoke`

Revokes one manual grant. This is idempotent: a replay of the same revocation
returns the existing revoked record and does not create duplicate audit events.

Request:

```json
{
  "reason": "Support request completed"
}
```

Response `200 OK`:

```json
{
  "grant": {
    "id": "pro-grant:7a3bd1f0-4d7b-4e7a-a084-8a2ee3221d41",
    "revokedAt": "2026-08-30T06:30:00Z"
  },
  "effectiveEntitlement": {
    "active": false,
    "sources": [],
    "expiresAt": null
  },
  "replayed": false
}
```

If an Apple subscription remains active, `effectiveEntitlement.active` remains
`true` and its source is reported as `apple`; revoking a manual grant never
revokes an Apple subscription.

### 6.7 `GET /admin/v1/audit`

Lists administrative actions, newest first. Query parameters are `uid`,
`action`, `outcome`, `from`, `to`, `limit`, and `cursor`.

```json
{
  "items": [
    {
      "id": "admin-audit:...",
      "action": "manualProGrant.create",
      "outcome": "accepted",
      "targetUid": "clerk:user_123",
      "grantId": "pro-grant:7a3bd1f0-4d7b-4e7a-a084-8a2ee3221d41",
      "actorUid": "clerk:admin_456",
      "reason": "Beta tester access",
      "requestId": "...",
      "createdAt": "2026-08-30T06:00:00Z"
    }
  ],
  "nextCursor": null
}
```

Audit records are append-only, redacted, and do not include tokens, full
request bodies, financial content, or credentials. The storage location may be
the existing `anke_entities` audit stream partitioned by resolved
`householdId`, with a separate operator metadata projection if cross-household
queries are needed. This choice must not weaken the existing household
partition boundary.

### 6.8 `GET /admin/v1/entitlements`

Lists manual Pro grants for operational follow-up. Query parameters are
`status` (`active`, `expired`, or `revoked`), `expiringWithinDays`, `limit`, and
`cursor`. The response uses the same `items` and `nextCursor` envelope as the
other cursor-based lists and includes the target identity summary, grant
period, reason, creator, and current status. Apple subscription rows are not
included in this operational list; the account detail endpoint remains the
source breakdown for troubleshooting Apple and manual access together.

## 7. Entitlement storage and compatibility notes

The implementation should introduce a small `ManualGrantStorage` protocol and
an entitlement aggregation service rather than teaching the Apple verifier to
create fake transactions. The existing `AppleBillingService` can remain the
provider-specific adapter.

The current Cosmos identities container is partitioned by `/uid`. Any
`manualProGrant` and `appleSubscription` document written there must use the
account UID as its partition value. Provider-transaction lookup must therefore
use a cross-partition query or a lookup that already knows the owning UID; an
item ID is not itself the `/uid` partition value.

The following existing paths must be updated together when the contract is
implemented:

- `app/models/billing.py`: optional source breakdown and admin models;
- `app/services/billing.py`: effective entitlement aggregation;
- `app/storage/protocols.py`, `app/storage/cosmos.py`, and
  `app/storage/in_memory.py`: manual-grant CRUD, expiry, revoke, and deletion;
- `app/dependencies.py`: admin authentication and service dependencies;
- `app/main.py`: `/admin/v1/*` routes and error mapping;
- `test/test_billing.py`, new admin API tests, and storage tests.

No iOS client UID, local `isPro`, Clerk metadata copied by the browser, or
hidden endpoint may become an entitlement authority.

## 8. Security and operational requirements

- Production admin configuration is separate from Development and is stored in
  Key Vault/protected Function App settings.
- Missing admin configuration fails closed at startup or on the first admin
  request; there is no default admin, query-string secret, or emergency bypass.
- The admin API never returns ledger, asset, note, or API-key content.
- User search and admin detail responses are PII-minimized and rate-limited.
- A grant, extension (new grant), and revoke each create one auditable action.
- Account deletion removes manual grants and their identity linkage.
- Apple Server Notifications update only Apple records and cannot overwrite a
  manual grant.
- Development tests use synthetic Clerk identities and Development Cosmos only.
  Production live-data mutation remains a separately authorized operation.
- Deployment must retain the existing managed-identity Cosmos boundary and
  must not introduce a Cosmos account key into the web application.

## 9. Contract acceptance gates

### Static and unit gates

- Python compile and focused billing/admin/storage tests pass.
- OpenAPI exposes only the documented admin routes and schemas.
- `git diff --check` passes.
- Expiry, UTC validation, lifetime confirmation, revoke idempotency, changed
  idempotency body, Apple-plus-manual aggregation, and deletion are covered.

### Authorization gates

- Missing/invalid Clerk token returns `401`.
- A valid ordinary Anke user returns `403`.
- An allowlisted admin can read and mutate only through `/admin/v1/*`.
- Caller-supplied household IDs, arbitrary Cosmos IDs, and Apple transaction
  impersonation are rejected.

### Development runtime gates

- A synthetic account can be found, granted a fixed-term Pro period, read back
  through both the admin endpoint and `/api/v1/billing/entitlement`, and
  revoked.
- The iOS/cloud Pro-protected operation observes the grant and then observes
  its expiry/revocation.
- An Apple webhook for the same account does not alter the manual grant.
- The audit list contains exactly one accepted create and one accepted revoke
  for the test idempotency keys.

### Production gate

Production deployment, configuration, and any real-account mutation require a
separate explicit authorization. A successful Development test is not
Production evidence.
