# Anke Money Cloud

Private backend for Anke Money: Clerk-authenticated FastAPI on Azure Functions,
Azure Cosmos DB persistence, synchronization, audit, and Remote MCP/Skill
adapters.

The service coordinates the signed-in cloud workspace. The iOS app has two storage
authorities: signed-out Local storage and the signed-in Anke service.

## Repository map

```text
function_app.py            Azure Functions ASGI entry
app/main.py                FastAPI construction and routes
app/auth/                  Clerk verification and Anke session tokens
app/models/                API and persisted contracts
app/storage/               In-memory and Cosmos adapters
docs/                      Frozen architecture, model, security, and DoD
scripts/cosmos_smoke.py    Opt-in Development-only synthetic write/read check
test/                      Credential-free unittest suite
```

Read `AGENTS.md` and the contracts under `docs/` before implementation work.
Executed checks and unresolved cloud gates are recorded in `docs/verification.md`.

## Runtime

- Python 3.11 for Azure and CI
- FastAPI
- Azure Functions Python v2 programming model
- PyJWT with cryptography for Clerk and Anke token verification
- Azure Cosmos DB for NoSQL

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s test -p 'test_*.py'
python -m uvicorn app.main:fastapi_app --host 127.0.0.1 --port 3002
```

Copy `local.settings.json.example` to ignored `local.settings.json` only when
using Azure Functions locally. Set a random `ANKE_SESSION_SIGNING_SECRET` with
at least 32 bytes; deployed values belong in Key Vault.

## Authentication boundary

The iOS app uses ClerkKit for Apple, Google, and email-code sign-in. Clerk
keeps the provider flow and session on the device. The app sends a short-lived
Clerk session token to `POST /api/v1/auth/clerk/exchange`. Anke Cloud verifies
the Clerk JWT against the configured JWKS and issuer, maps the Clerk subject to
an Anke user and household, then issues an Anke session token. Protected Anke
APIs accept only that Anke session token.

## Local endpoints

- `GET /ping` — public process health
- `GET /openapi.json` — API contract
- `POST /api/v1/auth/clerk/exchange` — verify Clerk and issue an Anke session
- `GET /api/v1/me` — return the verified Anke identity
- `PATCH /api/v1/me` — update the signed-in owner's display name
- `POST /api/v1/bootstrap` — resolve or create the server-owned owner,
  household, device, and connection records
- `POST /api/v1/sync/push` — accept ordered, idempotent device mutations
- `GET /api/v1/sync/pull` — return changes after an opaque cursor
- `GET /api/v1/audit` — return redacted owner-visible mutation outcomes
- `DELETE /api/v1/account` — idempotently erase the authenticated owner's
  complete Agent Cloud workspace and identity membership
- `POST /api/v1/migrations` — stage a verified Local snapshot
- `POST /api/v1/migrations/activate` — activate a staged manifest
- `GET|POST|DELETE /api/v1/agent-api-key` — manage the single workspace API Key
- `/agent/v1/*` — six fixed Agent capabilities
- `POST /mcp` — Remote MCP Streamable HTTP endpoint backed by the same service

The household is always resolved from the verified Anke user ID. Sync and
migration payloads cannot choose a household or actor. Normal entity deletion and
privacy erasure are separate operations.

Agent endpoints use one workspace API Key as a separate bearer boundary. Plaintext
is returned only on creation or reset; the service retains only its SHA-256 hash
and display prefix. Direct Agent HTTP and Remote MCP share the same six capability
checks, rate limits, idempotency, and audit behavior.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANKE_ENVIRONMENT` | yes | `local`, `dev`, or `prod` |
| `CLERK_JWKS_URL` | auth | HTTPS Clerk JWKS endpoint |
| `CLERK_ISSUER` | auth | HTTPS Clerk issuer URL |
| `CLERK_AUDIENCE` | auth | Optional Clerk JWT audience |
| `CLERK_SECRET_KEY` | account deletion | Clerk Backend API secret, stored outside source control |
| `CLERK_BACKEND_API_URL` | account deletion | HTTPS Clerk Backend API base URL |
| `ANKE_SESSION_SIGNING_SECRET` | auth | At least 32 random bytes; use Key Vault in Azure |
| `ANKE_SESSION_TTL_SECONDS` | no | Anke session lifetime; default 30 days |
| `ANKE_COSMOS_ENDPOINT` | Cosmos | Cosmos account endpoint |
| `ANKE_COSMOS_DATABASE` | Cosmos | Database name |
| `ANKE_COSMOS_ENTITIES_CONTAINER` | Cosmos | Primary `/householdId` container |
| `AZURE_CLIENT_ID` | Azure Cosmos | Function App managed identity client ID |
| `ANKE_COSMOS_KEY` | local fallback | Ignored local account key |
| `ANKE_COSMOS_EXPECTED_ACCOUNT_NAME` | smoke | Exact Development account name guard |
| `ANKE_COSMOS_ALLOW_SMOKE_WRITE` | smoke | Explicit `true` opt-in |
| `ANKE_AGENT_REQUESTS_PER_MINUTE` | Agent security | Authenticated request limit; default `120` |
| `ANKE_AGENT_FAILED_AUTH_THRESHOLD` | Agent security | Invalid-token anomaly threshold; default `5` |
| `ANKE_MCP_ALLOWED_HOSTS` | MCP | Host patterns accepted by rebinding protection |
| `ANKE_MCP_ALLOWED_ORIGINS` | browser MCP | Optional trusted browser origins |

## Development Cosmos smoke

The test deliberately writes one synthetic item and reads that exact item back. It
does not delete it and cannot run in Production.

```bash
ANKE_ENVIRONMENT=dev \
ANKE_COSMOS_ALLOW_SMOKE_WRITE=true \
python scripts/cosmos_smoke.py
```

Before running it, read `docs/security-boundary.md`. A local/unit pass is not
cloud evidence; record the exact Development target and result separately.

## Development deployment

The current Development endpoint is:

```text
https://func-anke-money-dev-zq01-a0btadd7fsfkc6cj.eastasia-01.azurewebsites.net
```

Cosmos access uses the Function App managed identity. The Clerk secret key and
Anke session signing secret must use protected Key Vault references. Do not put
credentials or session tokens in source, logs, command arguments, or smoke output.

A monitored Azure timer runs daily at 03:00 UTC. It removes recoverable payloads
from tombstones older than 30 days and deletes redacted audit events older than
365 days; both operations are idempotent.
