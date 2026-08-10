# Anke Money Cloud

Private backend for Anke Money Agent Cloud: Firebase-authenticated FastAPI on Azure
Functions, Azure Cosmos DB persistence, synchronization, audit, and future Remote
MCP/Skill adapters.

The service coordinates the signed-in cloud workspace. The iOS app has two storage
authorities: signed-out Local storage and the signed-in Anke service.

## Repository map

```text
function_app.py            Azure Functions ASGI entry
app/main.py                FastAPI construction and routes
app/auth/                  Firebase identity verification
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
- Firebase Admin SDK
- Azure Cosmos DB for NoSQL

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s test -p 'test_*.py'
python -m uvicorn app.main:fastapi_app --host 127.0.0.1 --port 3002
```

Copy `local.settings.json.example` to ignored `local.settings.json` only when using
Azure Functions locally. Do not populate or commit the example.

## Local endpoints

- `GET /ping` — public process health; does not prove Firebase or Cosmos health
- `GET /openapi.json` — API contract
- `GET /api/v1/me` — requires a valid Firebase ID token
- `POST /api/v1/bootstrap` — resolves or creates the server-owned owner,
  household, device, and connection records
- `POST /api/v1/sync/push` — accepts ordered, idempotent device mutations
- `GET /api/v1/sync/pull` — returns changes after an opaque cursor
- `GET /api/v1/audit` — returns redacted owner-visible mutation outcomes
- `DELETE /api/v1/account` — idempotently erases the authenticated owner's
  complete Agent Cloud workspace and UID membership during the in-app privacy
  deletion flow
- `POST /api/v1/migrations` — stages a verified Local snapshot
- `POST /api/v1/migrations/activate` — atomically activates a staged manifest
- `POST /api/v1/agent-connections` — creates a scoped owner-authorized Agent
  connection and returns its short-lived access token once
- `GET /api/v1/agent-connections` — lists owner-visible Agent connections
- `DELETE /api/v1/agent-connections/{connection_id}` — immediately revokes a
  connection and its outstanding token
- `POST /agent/v1/token/refresh` — rotates the short-lived access token while
  the parent grant remains active
- `GET /agent/v1/ledger/entries` — reads ledger entries with `ledger:read`
- `POST /agent/v1/ledger/entries` — accepts an idempotent, scoped remote Agent
  ledger write with `ledger:create` and publishes it to the incremental stream
- `GET /agent/v1/assets` — reads asset accounts and snapshots with `assets:read`
- `PATCH /agent/v1/assets/{account_id}` — updates one asset by appending an
  idempotent dated snapshot with `assets:update`
- `GET /agent/v1/categories` — reads categories with `categories:read`
- `GET /agent/v1/channels` — reads payment channels with `channels:read`
- `POST /mcp` — Remote MCP Streamable HTTP endpoint exposing the same six
  capabilities through the shared application service; `2026-07-28` is the
  canonical stateless protocol, with a temporary `2025-11-25` transport adapter
  for host products whose embedded MCP client has not yet upgraded

The household is always resolved from the verified Firebase UID. Sync and
migration payloads cannot choose a household or actor. A registered device uses
one ascending outbox sequence; a repeated mutation ID returns its stored result.
Revision mismatches return explicit conflicts, and accepted deletes publish
tombstones rather than physically erasing the entity.

Normal entity deletion and privacy erasure are separate operations. The account
endpoint is owner-authenticated and deletes the whole household partition plus
the UID membership record; it is not an Agent scope and is safe to retry after a
partial client-side Apple/Firebase account-deletion flow.

Agent endpoints use a separate bearer-token boundary from Firebase owner APIs.
Only the token hash is retained. Read-only grants are capped at 7 days, grants
with create access at 24 hours, and access tokens at 15 minutes. The separately
hashed refresh credential can rotate access tokens only until the parent grant
expires. Rejected
authentication attempts for a known connection are
recorded in the owner-visible audit stream. Remote Agent entries use their own
idempotency key, so a retry cannot duplicate a ledger entry while the app is
offline. Reusing a key with different content is rejected. The same rule applies
to remote asset updates. Every Agent write atomically stores the connection actor,
exact scope, source (`api`, `mcp`, or `skill`), idempotency claim, redacted
before/after difference, and owner-visible audit event.
Source is selected once when the connection is created and is enforced by the
server; callers cannot relabel a write or use the credential on another transport.
An owner can pause, resume, or irrevocably revoke a connection without changing
its scopes or expiry. Each connection is limited to 120 authenticated requests
per rolling fixed 60-second window by default. Repeated invalid tokens for a
known connection produce a deduplicated owner-visible anomaly event; they do not
silently widen access or permanently lock the valid credential.
See [Skill installation](docs/skill-installation.md) for the user-side connection
flow and credential boundary.

The app imports without Firebase credentials or a Cosmos connection. External
clients initialize lazily only when an authenticated or explicit storage operation
requires them.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `ANKE_ENVIRONMENT` | yes | `local`, `dev`, or `prod` |
| `ANKE_FIREBASE_PROJECT_ID` | auth | Expected Firebase project/audience |
| `ANKE_FIREBASE_WEB_API_KEY` | auth smoke | Firebase client API key used only to exchange a synthetic custom token |
| `ANKE_FIREBASE_ALLOW_SYNTHETIC_USER` | auth smoke | Explicit `true` opt-in; the smoke deletes its random test user |
| `ANKE_FIREBASE_SMOKE_BASE_URL` | auth smoke | Optional HTTPS Development deployment target |
| `ANKE_FIREBASE_SMOKE_EXPECTED_HOST` | auth smoke | Exact host guard required with a remote smoke target |
| `GOOGLE_APPLICATION_CREDENTIALS` | local auth | Path to ignored Firebase credential JSON; deployed environments should use protected configuration |
| `ANKE_FIREBASE_CREDENTIALS_JSON` | Azure auth | Key Vault-backed service-account JSON; never commit or log it |
| `ANKE_FIREBASE_CHECK_REVOKED` | no | Verify revocation on every request; defaults true outside local |
| `ANKE_COSMOS_ENDPOINT` | Cosmos | Cosmos account endpoint |
| `ANKE_COSMOS_DATABASE` | Cosmos | Database name |
| `ANKE_COSMOS_ENTITIES_CONTAINER` | Cosmos | Primary `/householdId` container |
| `AZURE_CLIENT_ID` | Azure Cosmos | Client ID of the Function App's user-assigned managed identity |
| `ANKE_COSMOS_KEY` | local fallback | Ignored local account key; prefer managed identity in Azure |
| `ANKE_COSMOS_EXPECTED_ACCOUNT_NAME` | smoke | Exact Development account name guard |
| `ANKE_COSMOS_ALLOW_SMOKE_WRITE` | smoke | Explicit `true` opt-in |
| `ANKE_AGENT_REQUESTS_PER_MINUTE` | Agent security | Per-connection authenticated request limit in each 60-second window; default `120` |
| `ANKE_AGENT_FAILED_AUTH_THRESHOLD` | Agent security | Known-connection invalid-token attempts in five minutes before one anomaly event; default `5` |
| `ANKE_MCP_ALLOWED_HOSTS` | MCP | Comma-separated exact host patterns accepted by DNS-rebinding protection; deployed host must be listed |
| `ANKE_MCP_ALLOWED_ORIGINS` | browser MCP | Optional comma-separated trusted browser origins; empty for non-browser clients |

## Azure Functions local host

Install Azure Functions Core Tools separately, then:

```bash
func start --verbose
```

The default local port in the example settings is 3002. A successful Uvicorn run
does not prove the Functions host integration.

## Development Cosmos smoke

The test deliberately writes one synthetic item and reads that exact item back. It
does not delete it and it cannot run in Production.

```bash
ANKE_ENVIRONMENT=dev \
ANKE_COSMOS_ALLOW_SMOKE_WRITE=true \
python scripts/cosmos_smoke.py
```

Before running it, read `docs/security-boundary.md`. A local/unit pass is not cloud
evidence; record the exact Development target and smoke result separately.

The Development Function App uses its attached user-assigned managed identity via
`AZURE_CLIENT_ID`. Do not add a Cosmos account key to deployed settings when this
identity has the required Cosmos data-plane role.

## Development Firebase auth smoke

After the independent Development Firebase project and protected Admin credential
are configured, validate a real short-lived ID token without placing it in shell
history or process arguments:

```bash
ANKE_ENVIRONMENT=dev \
ANKE_FIREBASE_PROJECT_ID=your-development-project-id \
python scripts/firebase_auth_smoke.py
```

The script prompts for the token with hidden input and prints no token or decoded
claims. It reports only the configured project ID and verified UID.

For a credential-backed end-to-end Development check without retaining a test
account, set the two opt-in smoke variables and run:

```bash
ANKE_ENVIRONMENT=dev \
ANKE_FIREBASE_ALLOW_SYNTHETIC_USER=true \
python scripts/firebase_e2e_smoke.py
```

To exercise the deployed Development Function instead of the in-process app, also
set `ANKE_FIREBASE_SMOKE_BASE_URL` and the exact
`ANKE_FIREBASE_SMOKE_EXPECTED_HOST`. The script rejects HTTP, host mismatches, and
base URLs containing a path.

The script creates a random `smoke-backend-` Firebase user through custom-token
exchange, calls the real Firebase verifier on `/api/v1/me`, and deletes that exact
synthetic user in a `finally` block. It does not validate Sign in with Apple or a
physical device.

## Development deployment

The current Development endpoint is:

```text
https://func-anke-money-dev-zq01-a0btadd7fsfkc6cj.eastasia-01.azurewebsites.net
```

Azure stores Firebase Admin JSON in Key Vault secret
`firebase-admin-credentials-json`. The Function App setting contains only a Key
Vault reference; its user-assigned managed identity has the minimum secret-reader
role. Cosmos access also uses managed identity. Do not replace either path with a
committed or plain-text account key.
Normal sync writes and Agent authorization remain disabled while a newly
bootstrapped workspace is empty or staging migration data. Migration activation
is the only transition that makes the Agent Cloud workspace writable.

A monitored Azure timer runs daily at 03:00 UTC. It removes recoverable payloads
from tombstones older than 30 days and deletes redacted audit events older than
365 days; both operations are idempotent.
