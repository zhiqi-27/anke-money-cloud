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
