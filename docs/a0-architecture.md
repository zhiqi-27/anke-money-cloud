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

Anke Cloud verifies the end-user identity at its Clerk exchange boundary and signs
its own session token. The Anke user ID in that token is not an authorization grant
and not the ledger partition key. The server resolves the ID to server-owned
membership and authorization records before any household access.

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

Every remote ledger create appends a distinct entry. Repeating the exact request
with the same `idempotencyKey` returns the original result and cannot create a
second entry. Reusing the key for different content is rejected. Repeating the
same business data with a new key creates a new entry.

The first remote scope set is deliberately narrow:

- `ledger:read`
- `ledger:create`
- `assets:read`
- `assets:update`
- `categories:read`
- `channels:read`

Permanent delete, authorization changes, other-household reads, bank/payment bill
imports, and unconfirmed bulk asset updates are excluded. `assets:update` only
appends one dated snapshot to one account; it cannot replace history.

HTTP API, Remote MCP, and the Anke Money Skill share one service and storage
implementation. Every Agent write records the verified connection identity, exact
scope, stable idempotency key, operation source, structured before/after change,
and owner-visible audit event in one household transaction.

Operation source is an immutable `api`, `mcp`, or `skill` integration selected
when the connection is created. The server derives it from the authenticated
connection and rejects use through another transport; write payloads cannot set it.

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

Development and Production use separate Function Apps, Cosmos accounts, databases,
credentials, monitoring, and deployment configuration. They may share the
Zero24-owned Azure subscription, but not data-plane identities or account keys.

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
application settings. The Clerk secret key and Anke session signing secret are
ignored files or protected environment variables locally and never enter Git.

## A0-10 · Delivery shape

The initial backend is a modular monolith: one private Git repository, one FastAPI
application hosted through Azure Functions, injected storage/auth adapters, and one
versioned API contract. API, Remote MCP, Skill, and infrastructure code remain in
this repository until operational evidence justifies a split.

## Approved amendment · 2026-08-06 · A3 lifecycle visibility and A4 abuse controls

Agent connection documents add optional `lastUsedAt` and fixed-window request and
failed-auth counters. These are additive lifecycle metadata, not financial records
or new authority. Owner-only pause/resume endpoints preserve the original immutable
scopes, integration, token hashes, and grant expiry; revoke remains irreversible.

Every valid connection is limited to 120 authenticated requests per minute across
HTTP API, Remote MCP, and Skill. Excess requests and a five-in-five-minute known-
connection authentication anomaly append audit events. Audit events remain outside
the Agent capability surface and can be removed only by the existing retention or
separately authorized workspace/privacy erasure process.

## Approved amendment · 2026-08-06 · MCP 2026 transport compatibility

Remote MCP's canonical revision is `2026-07-28`: each request is self-describing,
the primary client flow performs no initialize handshake, and the server keeps no
protocol session. To avoid excluding host products whose embedded MCP client has
not yet adopted that breaking revision, the same stateless endpoint also accepts
the `2025-11-25` initialize-handshake wire format.

Compatibility is confined to transport negotiation. Both revisions call the same
six tools and the same authorization, service, storage, idempotency, rate-limit,
and audit code. There is no data-model or migration impact and no dual business
API. The legacy adapter may be removed after post-launch interoperability evidence
shows the supported Agent products use `2026-07-28`; rollback before that point is
to retain the adapter, not to downgrade the canonical protocol or data contract.

## Approved amendment · 2026-08-10 · Two-authority storage and Local-only migration

Anke Money has exactly two storage authorities: signed-out Local storage and the
signed-in Anke service. This amendment supersedes A0-02's three-mode table and
removes iCloud / CloudKit as an app storage mode, capability, migration source,
and release gate. Completing authentication starts the existing staged,
digest-verified, idempotently resumable Local-to-cloud migration; the service
accepts only `local` as migration provenance.

A new migration session is accepted only while the server-owned household status
is `empty`. An active workspace may be attached by an empty Local store, but an
independent non-empty Local store cannot be uploaded or silently merged into it.
This is enforced by both the client and service.

There is no Cosmos container, partition, or financial-document migration. Existing
migration-session provenance remains historical audit metadata and is not rewritten
or deleted. No public client used the removed `cloudkit` API value, so no API
compatibility window is required. Rollback would require a new product amendment
and restoration of the removed client capability and API enum; it cannot introduce
CloudKit and the Anke service as concurrent writers.

## Approved amendment · 2026-08-13 · Reference entity ID namespaces

Cloud reference definitions use collision-free logical IDs in every migration and
sync payload: `channel:{localId}`, `category:{localId}`, and
`asset-category:{localId}`. Ledger and asset payload references use the same cloud
IDs; the iOS replica maps them back to its unchanged local definition IDs. This is
required because Cosmos `id` uniqueness applies across every entity type inside one
household partition, while the local product intentionally has overlapping fallback
and legacy IDs such as `other` and `business`.

The service rejects a migration containing duplicate `entityId` values before any
staged write. Existing unique legacy IDs remain readable; no container or partition
change is involved. Development synthetic partitions were already cleaned and no
real Local migration had activated before this amendment, so no data backfill is
required. Rollback before activation is the previous client mapping; after
activation, rollback must retain read compatibility for the namespaced IDs.

## Approved amendment · 2026-08-13 · Single full-capability Agent API Key

This amendment supersedes A0-05's expiring, scoped grant, refresh credential,
transport-bound integration, pause, and resume contract. Each active workspace has
one long-lived API Key with all six frozen Agent capabilities. The same key is
accepted by direct Agent HTTP and Remote MCP and remains valid until reset,
revocation, account deletion, or workspace deletion. Plaintext is returned only on
create/reset; storage retains a SHA-256 hash and display prefix. Reset and revoke
take effect on the next request.

The business capability boundary is unchanged: delete, ledger-history mutation,
member/settings/migration/export/auth-management/audit-management operations,
imports, and unconfirmed bulk changes remain unavailable to Agents. Existing
idempotency, redacted audit, rate-limit, anomaly, and household isolation rules
remain in force.

The old owner connection endpoints and Agent refresh endpoint are removed rather
than maintained as a compatibility window. Their token format is rejected
immediately, and creating/resetting the new API Key deletes legacy credential
documents while retaining append-only audit history. There is no financial-data or
container migration. Rollback requires a new product amendment and a deliberate
credential migration; old limited credentials must not silently become valid.

## Approved amendment · 2026-08-13 · Compact full-capability API Key format

New full-capability keys encode the household UUID as a compact URL-safe locator
and pair it with a 192-bit random secret. The plaintext length is fixed at 59
characters, while Cosmos continues to store only its SHA-256 hash and display
prefix. Previously issued long full-capability keys remain valid until reset or
revocation; removed limited-access credentials remain rejected.

This changes neither the deterministic connection identity nor any capability,
partition, document, rate-limit, audit, or revocation rule. No stored document or
financial-data migration is required. Rollback may resume issuing the longer
full-capability format while retaining both parsers until all compact keys have
been reset or revoked.

## Approved amendment · 2026-08-14 · Clerk-managed multi-provider authentication

Firebase Authentication is removed from the product. ClerkKit now owns Apple,
Google, and email-code sign-in on iOS. iOS sends a short-lived Clerk session token
to `POST /api/v1/auth/clerk/exchange`. Anke Cloud verifies the Clerk JWT signature,
issuer, optional audience, and expiry, maps the stable Clerk subject to a new Anke
user ID, and signs its own session token.

The session token is the only credential sent to protected Anke APIs. ClerkKit
refreshes its short-lived session token; Anke stores its own session token in the
iOS Keychain with a bounded lifetime. Household membership and account deletion
remain server-owned. No Firebase user, Firebase token, or old-account data
migration is retained. A future WeChat provider will use the same
provider-subject-to-Anke-user mapping and session issuance boundary.

This is a breaking authentication change with no compatibility window because the
current product has no supported legacy-account contract. Existing Firebase-only
accounts cannot sign in or recover data through the new product. Rollback requires
a new dated amendment and a deliberate reintroduction of Firebase; it cannot be
done by silently accepting old Firebase tokens.

## Approved amendment · 2026-08-17 · Change-driven client synchronization

An authenticated iOS device registers its APNs token under its existing household
partition. The `anke_entities` Change Feed trigger groups changed synchronized
entities by household and sends one collapsed background notification per household.
The notification is a data-free hint; clients still authorize and pull changes from
the canonical sync API, so APNs is neither a data transport nor a new writer.

The trigger uses a separately provisioned lease container and never creates Azure
resources at runtime. If APNs or background delivery is unavailable, active clients
fall back to the existing cursor pull every 30 seconds and BGAppRefresh provides an
additional best-effort opportunity. Rollback disables the Change Feed trigger and
token registration route; ordered push/pull and cursor compatibility are unchanged.
