# Anke Money Cloud repository contract

This file applies to the whole repository.

## Sources of truth

Read these before changing behavior, persistence, authentication, or public API:

1. `docs/a0-architecture.md` — frozen A0 architecture decisions.
2. `docs/cosmos-data-model.md` — persisted document and partition contract.
3. `docs/security-boundary.md` — authentication, authorization, secrets, and live-data rules.
4. `docs/definition-of-done.md` — required evidence before claiming a slice complete.
5. `docs/verification.md` — checks that actually ran, separated by evidence class.

The iOS product decisions remain authoritative for product behavior in the sibling
`anke-money-ios/docs/product-decisions.md`. If a server change would alter a frozen
product decision, amend the iOS decision first; do not silently redefine it here.

## Service contract

- Python 3.11 is the Azure and CI runtime contract. Python 3.12 may be used for
  local compatibility checks.
- `function_app.py` is a thin Azure Functions ASGI entry point.
- `app/main.py` constructs FastAPI and registers routers.
- Route -> service -> storage is the allowed dependency direction.
- Firebase establishes identity only. Household membership and every permission
  are server-owned authorization decisions.
- Clients and MCP tools never connect directly to Cosmos DB.
- The primary Cosmos container uses `/householdId` as its partition key.
- Money is integer fen. Floating-point currency is prohibited.
- Remote ledger creation is append-only and idempotent by `operationId`.
- A ledger write, idempotency record, and audit event must share one transactional
  batch in the same container and household partition.
- Runtime code must never create, rename, repartition, or delete production
  Cosmos resources.

## Development workflow

1. Inspect the worktree and preserve unrelated changes.
2. Update contracts, models, implementation, and tests together.
3. Use injected fakes for the normal unit suite; it must require no credentials or
   network access.
4. Run `python -m unittest discover -s test -p 'test_*.py'`.
5. Run `python -m compileall -q app function_app.py scripts` and
   `git diff --check`.
6. Live Development smoke tests are opt-in and follow the safeguards in
   `docs/security-boundary.md`.

## Security and operational rules

- Never inspect, print, paste, log, or commit credentials, Firebase service-account
  JSON, JWTs, Cosmos keys, connection strings, or private financial payloads.
- Prefer Azure managed identity for deployed Cosmos access. A local Cosmos key is
  a development fallback only and must remain in ignored settings.
- Log request IDs, operation IDs, actor IDs, entity IDs, status, and error types;
  do not log authorization headers, notes, amounts, or complete documents.
- Never trust a client-supplied `uid`, actor, revision, or authorization scope.
- Never run live writes unless the exact account, database, container, environment,
  and opt-in flag have been checked first.
- Live tests may write only synthetic, visibly tagged smoke documents. They must
  not read, update, or delete real user documents.
- Do not deploy, push, change Azure resources, rotate secrets, or promote schemas
  without explicit user authorization.

## Git rules

- This repository is independent from `anke-money-ios` and
  `chatget-source-functions`; do not add either as a submodule.
- Recap is read-only reference material. Never modify or commit its files from an
  Anke Money task.
- Keep Development and Production configuration separate.
- Do not commit generated secrets or a populated `local.settings.json`.
