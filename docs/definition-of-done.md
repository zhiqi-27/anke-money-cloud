# Definition of Done

## A0 foundation

- [x] A0 architecture, Cosmos model, and security boundary are internally consistent.
- [x] FastAPI imports without network access or credentials.
- [x] Azure Functions exposes the same FastAPI application through ASGI.
- [x] `/ping` returns a non-sensitive health payload.
- [x] `/openapi.json` exposes the intended public contract.
- [x] Protected routes reject missing, malformed, expired, revoked, or invalid Firebase tokens.
- [x] Verified token identity comes from Firebase, never a client UID field.
- [x] Primary Cosmos storage requires `/householdId` on every document and point read.
- [x] Ledger create, operation claim, and audit event are modeled as one household
  transactional batch.
- [x] Normal tests use fakes and require no Azure/Firebase/network access.
- [x] Development Cosmos smoke creates and reads one synthetic probe without touching
  real user documents.
- [x] `python -m unittest discover -s test -p 'test_*.py'`, compile verification,
  and `git diff --check` pass.

## Evidence classes

- Local: imports, unit tests, TestClient, compile checks.
- Azure Functions local host: Functions routing/runtime evidence.
- Development cloud: configured cloud resources and Development Cosmos/Firebase
  service evidence. Deployed endpoint evidence is recorded separately when code is
  actually deployed.
- Production: independently authorized deployment and production checks.

One evidence class never proves another.

## Development deployment

- [x] `anke_identities` exists with the exact `/uid` partition key.
- [x] Firebase Admin credentials are stored in Development Key Vault and exposed
  to the Function App only through a managed-identity Key Vault reference.
- [x] The committed Function project is deployed with a Python remote build.
- [x] Deployed `/ping` and `/openapi.json` return 200.
- [x] Deployed `/api/v1/me` returns 401 without a valid token and 200 for a real
  Firebase ID token whose UID is returned unchanged.

Development deployment evidence does not prove Sign in with Apple or physical-device
behavior.

## A1 local implementation checkpoint

- [x] Owner bootstrap persists user, household, device, connection, and UID membership records.
- [x] Ordered push and cursor pull contracts are versioned under `/api/v1`.
- [x] Bootstrap returns a full-replica cursor plus the server-owned next outbox
      sequence so a restored device cannot create a permanent sequence gap.
- [x] Mutation replay is idempotent and an outbox sequence gap is rejected.
- [x] Stale revisions return the server entity as an explicit conflict.
- [x] Accepted deletion produces a revisioned tombstone.
- [x] Accepted, rejected, and conflicted writes create a redacted audit event.
- [x] Local/iCloud migration uses a stable resumable session, verified counts, and a canonical SHA-256 digest.
- [x] Cosmos writes batch the accepted entity, operation result, audit event, and device sequence in one household partition.
- [x] Owner APIs create, list, and revoke scoped Agent connections without storing plaintext tokens.
- [x] Remote Agent ledger writes are idempotent, audited, and visible to the app's next cursor pull.
- [x] Revoked, expired, or invalid tokens for a known Agent connection are rejected and audited.
- [x] Access tokens rotate within 15 minutes and refresh cannot outlive or bypass the parent grant.
- [x] All five initial Agent scopes have separate enforced routes; insufficient-scope attempts are rejected and audited.
- [x] Remote asset snapshots validate their account, batch entity/operation/audit writes, and replay idempotently.
- [x] Empty or staging workspaces reject normal sync writes and Agent authorization until migration activation.
- [x] The daily retention function idempotently purges 30-day tombstone payloads and deletes 365-day audit events in local adapter tests.
- [x] The new A1 API has been deployed to Development and exercised against real Cosmos.
- [ ] A real local/iCloud dataset has completed staged upload, activation, reconnect pull, and source-archive verification.
- [ ] Tombstone payload purge and 365-day audit retention have operational timer evidence.

Checked items above are local contract, unit, and adapter evidence only. They do
not promote the Development deployment recorded earlier in this file.
