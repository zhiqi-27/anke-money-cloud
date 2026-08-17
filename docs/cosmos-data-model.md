# Cosmos data model

Status: A0 frozen
Primary API: Azure Cosmos DB for NoSQL

## Resource layout

| Resource | Partition key | Responsibility |
| --- | --- | --- |
| `anke_entities` | `/householdId` | Household ledger, reference data, assets, operations, grants, and audit events |
| `anke_identities` | `/uid` | Minimal Anke user ID to household membership lookup |

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
  "actor": { "type": "user", "id": "Anke user ID" },
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
- `allocationSourceId`, `allocationIndex`, `allocationCount`, and
  `allocationStartMonth`: optional all-or-none metadata for a read-only monthly
  allocation derived from an original transaction expense. The original entry
  remains stored; clients exclude it from ledger totals while allocations exist.
- `note`: optional user text with API length limit

A new `operationId` always creates a new ledger entry even if every business field
matches an earlier entry.

### Reference definitions

`paymentChannel` and `category` documents use stable, namespaced cloud IDs and
editable display metadata. Channels use `channel:{localId}`, ledger categories use
`category:{localId}`, and asset categories use `asset-category:{localId}`. Ledger
and asset payloads retain the matching namespaced reference ID while the iOS replica
maps it to its unchanged local definition ID. Deleting a definition first moves
affected entries to the non-deletable matching fallback according to the iOS
product decision. Migration validation rejects duplicate `entityId` values across
all entity types before any staged write because Cosmos `id` is unique within the
whole household partition, not within an entity type.

### Assets

`assetAccount` represents metadata and the current materialized amount.
`assetSnapshot` is an immutable dated observation. Ledger writes never mutate an
asset account or create an asset snapshot.

### `memberProfile`

An owner-managed reference record for a person a ledger entry or asset snapshot may
concern. It has no Apple credential, login session, role, invitation, grant, or collaboration
authority. Agent Cloud 1.0 still has one authenticating owner.

### `operation`

The Agent operation document ID is namespaced from the request idempotency key:
`operation:{idempotencyKey}`. It stores request hash, source, scope, action, result
entity ID, verified connection actor, accepted revision, and redacted change
summary. Creating this document in the same transactional batch is the idempotency
claim. A replay must match its original request hash.

### `auditEvent`

The Agent audit document ID is `audit:{idempotencyKey}`. It stores scope, actor,
source, action, target type/ID, outcome, revisions, idempotency key, and a redacted
difference summary.
It must not store authorization tokens, full notes, or whole before/after documents.

### Agent API Key

`agentAPIKey` is the single credential document for a workspace. Its deterministic
connection identity binds the owner and household, and its fixed capabilities are
`ledger:read`, `ledger:create`, `assets:read`, `assets:update`, `categories:read`,
and `channels:read`. The document stores only a SHA-256 key hash and display prefix;
plaintext is returned only by create/reset and is never persisted. It has no expiry
or refresh credential. Reset replaces the hash, revocation changes lifecycle state,
and either action is enforced on the next request.

New plaintext keys use the compact URL-safe form
`ank_<compact-household-id>.<192-bit-random-secret>`. The persisted document shape
is unchanged: only the SHA-256 hash and display prefix are stored. Authentication
continues to accept previously issued full-capability keys until reset or revocation.

Lifecycle metadata includes nullable `lastUsedAt`, a 60-second request window
start/count, and a five-minute failed-auth window start/count plus the last
deduplicated anomaly threshold. Creating or resetting the API Key also removes any
legacy `agentConnection` credential documents in that household partition.
Rate-limit and suspicious-authentication events are ordinary append-only redacted
audit documents in the same household partition.

### Push device

`pushDevice` uses the deterministic ID `push-device:{deviceId}` in `anke_entities`
and the existing `/householdId` partition. It stores the APNs token, sandbox or
production environment, bundle topic, app version, owner UID, revision timestamps,
and nullable `disabledAt`. Re-registration replaces the token for that device;
APNs `Unregistered`, `BadDeviceToken`, and `DeviceTokenNotForTopic` responses disable
the document without deleting audit or financial data.

The Change Feed processor uses a separate `anke_sync_leases` container partitioned
by `/id`. It contains only Azure Functions lease/checkpoint documents and no Anke
financial or identity records. Infrastructure provisions it before deployment;
runtime code does not create it.

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
  "uid": "Anke user ID",
  "provider": "apple",
  "providerSubject": "stable provider subject",
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
