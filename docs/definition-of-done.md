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
