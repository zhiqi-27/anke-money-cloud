# A0 architecture decisions

Status: frozen for backend foundation
Frozen on: 2026-08-04
Change rule: add a dated amendment with rationale, compatibility impact, data
migration impact, and rollback plan before changing a decision below.

## A0-01 · Product and trust boundary

Anke Money Cloud supports the manual, local-first household ledger defined by the
iOS product contract. It does not ingest bank, Alipay, WeChat, or card statements,
infer balances, provide financial advice, or introduce an in-app conversational
assistant.

## A0-02 · Three mutually exclusive storage modes

The iOS app may operate in exactly one mode at a time:

1. Local: SwiftData is the only store.
2. iCloud: the isolated CloudKit-backed SwiftData store is used.
3. Agent Cloud: Anke Money Cloud is the synchronization coordinator and an
   isolated local SwiftData store is the offline replica.

CloudKit and Agent Cloud never concurrently synchronize the same dataset. Moving
from Local or iCloud into Agent Cloud is a one-time migration into a new, empty
Agent Cloud workspace. It is staged, idempotently resumable, and activates only
after manifest, record-count, stable-ID, and content-digest verification. For an
iCloud source, the iOS app uploads from its refreshed local CloudKit-backed replica;
CloudKit is never a server-side source or bridge. The former source store remains a
read-only archive until the owner explicitly deletes it. Returning to another mode
requires a separately designed export/import operation; it is not a runtime
dual-write or mode-toggle rollback.

## A0-03 · Identity and authorization

Firebase Authentication verifies the end-user identity. Firebase UID is not an
authorization grant and not the ledger partition key. The server resolves the UID
to server-owned membership and authorization records before any household access.

Agent Cloud 1.0 has one authenticating owner and one active workspace. Optional
`memberProfile` records let ledger entries and asset snapshots identify the
household member they concern. A member profile is owner-managed reference data,
not an identity, login, permission holder, invitation, or collaborator. Every
workspace-owned entity carries `householdId`; in 1.0 it is the stable storage key
for that single workspace.

## A0-04 · API is the only remote data boundary

iOS clients, Skills, and MCP clients use versioned Anke Money APIs. They never
receive Cosmos credentials and never query a Cosmos Data API directly. MCP tools
adapt the same application services used by the HTTP API; they do not own separate
business rules.

## A0-05 · Append-only and idempotent remote writes

Every remote ledger create appends a distinct entry. Repeating a request with the
same `operationId` returns the original result and cannot create a second entry.
Repeating the same month, direction, channel, and category with a new operation ID
creates a new entry.

The first remote scope set is deliberately narrow:

- `ledger.read`
- `ledger.entry.create`
- `assets.read`
- `assets.snapshot.create`
- `reference-data.read`

Update, delete, member management, settings, migration, export, authorization
management, audit-log access, cross-household access, bill import, and unconfirmed
bulk writes are excluded from 1.0.

Read-only grants default to 24 hours and may extend to at most 7 days. A grant
containing either create scope defaults to 1 hour and may extend to at most 24
hours. Access tokens last at most 15 minutes and cannot refresh beyond the parent
grant. The service checks grant state on every request; 1.0 has no permanent grant.

## A0-06 · Synchronization contract

Synchronization uses ordered mutation push and cursor-based pull. The server
assigns the accepted revision and server timestamps. A client submits stable entity
IDs, a globally unique mutation/operation ID, its base revision where relevant,
its device ID, and per-device outbox order. Server membership determines the
household; a payload cannot grant itself access.

Conflict handling is explicit. Repeating a mutation ID returns its original result;
reusing an entity ID with different create content is rejected. Stale updates or
deletes return the current server version as a conflict and never silently overwrite
a newer revision or use client-clock ordering. Ledger creation stays append-only;
offline retries are safe because mutation IDs are idempotent.

## A0-07 · Cosmos resource boundary

Development and Production use separate Firebase projects, Function Apps, Cosmos
accounts, databases, credentials, monitoring, and deployment configuration. They
may share the Zero24-owned Azure subscription, but not data-plane identities or
account keys.

The primary container is `anke_entities`, partitioned by `/householdId`. Documents
that must commit atomically are stored in this container and household partition.
Identity-to-household lookup is stored separately in `anke_identities`, partitioned
by `/uid`; identity records cannot contain ledger or asset data.

## A0-08 · Audit and deletion

Every external write produces an immutable audit event containing actor, granted
scope, operation ID, target, outcome, and a redacted change summary. Agents cannot
alter or delete audit events. User-visible deletion is modeled as a tombstone where
the product permits deletion; permanent ledger deletion is not exposed to agents in
1.0. Once a delete is accepted, the 10-second Undo for ledger and asset UI is a new
audited compensating mutation, not history rewriting. Channel and category deletion
keeps the separately frozen non-restorable fallback behavior.

Tombstones keep recoverable payload for 30 days, then retain only minimum identity
and audit linkage needed to prevent resurrection. User-visible authorization and
mutation audit events are append-only for 365 days and never copy free-text notes.
Workspace deletion and legal/privacy erasure override normal retention.

## A0-09 · Secrets and service identity

Deployed services use managed identity and least-privilege Cosmos data-plane RBAC.
Secrets that cannot use managed identity live in Azure Key Vault or protected
application settings. Local keys and Firebase credentials are ignored files or
environment variables and never enter Git.

## A0-10 · Delivery shape

The initial backend is a modular monolith: one private Git repository, one FastAPI
application hosted through Azure Functions, injected storage/auth adapters, and one
versioned API contract. API, Remote MCP, Skill, and infrastructure code remain in
this repository until operational evidence justifies a split.
