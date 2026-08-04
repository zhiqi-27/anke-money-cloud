# Verification evidence

Evidence is appended only after the corresponding check runs. Local evidence does
not prove Development cloud or Production behavior.

| Date | Class | Gate | Evidence | Result |
| --- | --- | --- | --- | --- |
| 2026-08-04 | Local | Credential-free unit suite | Python 3.12 virtual environment; Firebase, API, model, in-memory storage, Cosmos adapter, and smoke guard tests | Passed; 31 tests in completion audit, including explicit expired and revoked Firebase token cases |
| 2026-08-04 | Local | Uvicorn HTTP | `GET /ping` 200; `GET /openapi.json` 200 with HTTP Bearer scheme; missing and invalid token `GET /api/v1/me` 401 | Passed |
| 2026-08-04 | Azure Functions local host | ASGI routing | Core Tools 4.12.1 on port 3013; `/ping` 200, `/openapi.json` 200, unauthenticated `/api/v1/me` 401 | Passed; local storage health was unavailable and no storage binding is used |
| 2026-08-04 | Local integration | Firebase client project alignment | iOS `GoogleService-Info.plist` identifies project `anke-money` and bundle `app.ankemoney.ios`; backend credential and examples use the same project ID | Passed; Firebase Admin initialized locally |
| 2026-08-04 | Development Firebase | Real ID-token API request | A random `smoke-backend-` custom token was exchanged through Firebase Authentication for a real ID token; the real verifier accepted `GET /api/v1/me` with status 200 | Passed; UID `smoke-backend-a08836ad-4dda-4294-8610-dfd4f3236830`; synthetic Firebase user deleted in `finally` |
| 2026-08-04 | Development Azure | Managed identity and Cosmos RBAC | Function App `func-anke-money-dev-zq01` has user-assigned identity client configuration; principal `db301095-08da-4b40-93d7-193bab250f33` has Cosmos NoSQL built-in Data Contributor scoped to `/dbs/anke_money_dev` | Passed; no Cosmos account key is required in deployed app settings |
| 2026-08-04 | Development Cosmos | Partition contract and synthetic write/read | Account `cosmos-anke-money-dev-zq01`, database `anke_money_dev`, container `anke_entities`; confirmed partition path `/householdId`; created and point-read one tagged synthetic probe | Passed; create 201, point read 200; run `9e7b7285-37ce-44d8-8fa6-228ff1e3dd9a`, item `smoke-9e7b7285-37ce-44d8-8fa6-228ff1e3dd9a`; retained for auditable cleanup |

## Remaining evidence boundaries

- The Function App exists and is running, but this repository has not been deployed
  to it; there is no deployed API endpoint verification in this baseline.
- Sign in with Apple, an Apple-issued credential through Firebase, and physical-device
  behavior remain unverified. The synthetic Firebase smoke is not evidence for them.
- `anke_identities` is part of the frozen model but was not present in the inspected
  Development Cosmos database. Provisioning it belongs to the deployment/schema step,
  not this primary `anke_entities` smoke.
- Production deployment, secrets, schema promotion, and production data were not
  accessed or changed.
