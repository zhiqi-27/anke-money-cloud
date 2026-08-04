# Cosmos data model

Status: A0 frozen
Primary API: Azure Cosmos DB for NoSQL

## Resource layout

| Resource | Partition key | Responsibility |
| --- | --- | --- |
| `anke_entities` | `/householdId` | Household ledger, reference data, assets, operations, grants, and audit events |
| `anke_identities` | `/uid` | Minimal Firebase UID to household membership lookup |

Runtime application startup does not create these resources. Infrastructure setup
must create `anke_entities` with the exact `/householdId` path and
`anke_identities` with `/uid`; a different path is a breaking migration.

## Common household document envelope

Every `anke_entities` document contains:

```json
{
  "id": "stable UUID or namespaced ID",
  "entityType": "ledgerEntry",
  "householdId": "stable household UUID",
  "schemaVersion": 1,
  "revision": 1,
  "createdAt": "2026-08-04T00:00:00Z",
  "updatedAt": "2026-08-04T00:00:00Z",
  "deletedAt": null,
  "deletion": null,
  "actor": { "type": "user", "id": "firebase uid" },
  "operationId": "client-generated UUID",
  "lastAcceptedMutationId": "client-generated UUID"
}
```

Server code owns `revision`, timestamps, `actor`, and authorization-derived fields.
Input models must not accept them as trusted client values.

Mutable synchronized documents additionally retain the accepted base/server
revision relationship and deletion metadata needed for explicit conflicts and
tombstone propagation. Device ID and per-device outbox order belong to mutation
records; client clocks never determine acceptance order.

## Entity types

### `ledgerEntry`

- `kind`: `transaction` or `monthlySummary`
- `direction`: `expense` or `income`
- `occurredAt`: exact UTC instant for a single transaction; normalized period
  anchor for a monthly record
- `monthStart`: canonical first day of the selected calendar month
- `channelId`: required for expense; absent for income
- `categoryId`: stable reference identifier
- `amountInFen`: positive signed 64-bit integer; currency is never floating point
- `note`: optional user text with API length limit

A new `operationId` always creates a new ledger entry even if every business field
matches an earlier entry.

### Reference definitions

`paymentChannel` and `category` documents use stable IDs and editable display
metadata. Entries retain the stable reference ID. Deleting a definition first moves
affected entries to the non-deletable matching fallback according to the iOS
product decision.

### Assets

`assetAccount` represents metadata and the current materialized amount.
`assetSnapshot` is an immutable dated observation. Ledger writes never mutate an
asset account or create an asset snapshot.

### `memberProfile`

An owner-managed reference record for a person a ledger entry or asset snapshot may
concern. It has no Firebase UID, login, role, invitation, grant, or collaboration
authority. Agent Cloud 1.0 still has one authenticating owner.

### `operation`

The operation document ID is namespaced from the request operation ID:
`operation:{operationId}`. It stores request type, status, result entity ID, actor,
and accepted revision. Creating this document in the same transactional batch is
the idempotency claim.

### `auditEvent`

The audit document ID is `audit:{operationId}`. It stores scope, actor, action,
target type/ID, outcome, request correlation ID, and a redacted difference summary.
It must not store authorization tokens, full notes, or whole before/after documents.

### Agent authorization

`agentConnection` stores connection identity and lifecycle. `authorizationGrant`
stores only the frozen scopes (`ledger.read`, `ledger.entry.create`, `assets.read`,
`assets.snapshot.create`, and `reference-data.read`), expiry, revocation, and
connection reference. Read-only grants default to 24 hours and are capped at 7
days; grants containing a create scope default to 1 hour and are capped at 24
hours. Access tokens are capped at 15 minutes and cannot outlive the grant. Tokens
are stored only as hashes or encrypted references; raw bearer tokens are never
persisted.

### Tombstones and retention

Deleted synchronized data stores actor, accepted mutation ID, server time, revision,
reason, and purge deadline. Recoverable payload is retained for 30 days, after which
only minimum identity and audit linkage remain to prevent resurrection. Authorization
and mutation audit events are append-only for 365 days and exclude free-text notes.
Workspace deletion and legal/privacy erasure override these normal periods.

## Identity document

`anke_identities` contains only the minimum mapping needed before a household
partition is known:

```json
{
  "id": "membership:{householdId}",
  "uid": "firebase uid",
  "householdId": "stable household UUID",
  "role": "owner",
  "status": "active",
  "createdAt": "2026-08-04T00:00:00Z"
}
```

Every subsequent point read or query includes the resolved `householdId`. Cross-
partition scans of household financial data are prohibited in request paths.

## Atomic ledger creation

One transactional batch in `anke_entities` and one `/householdId` value creates:

1. `operation:{operationId}`
2. the new `ledgerEntry`
3. `audit:{operationId}`

If any item conflicts or fails, the whole batch fails. A retry first point-reads the
operation document and returns its stored result. Cross-container atomicity is not
assumed.

## Query and indexing baseline

Request paths prefer point reads. Household list queries always constrain
`householdId` and `entityType`, then optionally `monthStart`, `direction`, or
`updatedAt`. Index policy tuning follows measured query metrics; A0 does not create
speculative composite indexes.

## Evolution and migration

- Additive optional fields are preferred.
- Readers tolerate known older `schemaVersion` values.
- Destructive shape changes require a versioned backfill, dry run, metrics,
  rollback, and compatibility window.
- Partition key changes require new containers and verified data migration; they
  are never performed in place.
