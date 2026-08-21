# Azure Deployment Plan

Status: Validated

## Objective

Deploy the prepared Anke Money Development Azure Functions backend update that
allows expense ledger entries without a concrete channel ID to represent “其他”.

## Scope

- Target: existing Development Function App `func-anke-money-dev-zq01`.
- Environment: Development only; no Production deployment.
- Data safety: no account, Cosmos data, or device-local data deletion.
- Follow-up: rebuild and install the matching iOS app on the connected physical
  device, then verify synchronization.

## Azure context

- Subscription: `Azure subscription 1`
  (`a1187bf2-2e2f-4e05-aaea-407163a009f5`)
- Tenant: `5ba85645-4c35-4efd-93ea-11c0890472d8`
- Resource group: `anke-money-dev` in `eastasia`
- Function App: `func-anke-money-dev-zq01`
- Endpoint: `https://func-anke-money-dev-zq01-a0btadd7fsfkc6cj.eastasia-01.azurewebsites.net`

## Deployment path

Use a non-destructive Python remote-build ZIP/One Deploy update of the existing
Azure Functions app. Do not provision a new resource group or alter Cosmos,
Key Vault, managed identity, or production resources.

## All validation checks pass

- Local Python unit suite: run `python -m unittest discover -s test -p 'test_*.py'`.
- Local compile: run `python -m compileall -q app function_app.py scripts`.
- Repository hygiene: run `git diff --check`.
- Azure account and subscription: confirm the authenticated default subscription
  is `a1187bf2-2e2f-4e05-aaea-407163a009f5`.
- Azure resource group and Function App: confirm `anke-money-dev` and
  `func-anke-money-dev-zq01` exist in East Asia.
- Existing endpoint preflight: use GET `/ping` and record the current response
  before deployment.
- Deployment package: include only `app/`, `function_app.py`, `host.json`, and
  `requirements.txt`; deploy with `az functionapp deployment source config-zip
  --build-remote true`.

## Validation Proof

- `python -m unittest discover -s test -p 'test_*.py'`: passed, 101 tests.
- `python -m compileall -q app function_app.py scripts`: passed.
- `git diff --check`: passed.
- Azure CLI account and resource checks: passed for subscription
  `a1187bf2-2e2f-4e05-aaea-407163a009f5`, resource group `anke-money-dev`, and
  Function App `func-anke-money-dev-zq01`.
- Existing `GET /ping`: HTTP 200, `environment=dev` before deployment.
- Live RBAC: Function managed identity principal
  `db301095-08da-4b40-93d7-193bab250f33` has the existing Cosmos SQL role
  assignment scoped to `/dbs/anke_money_dev`, plus Key Vault, storage, package
  container, and Application Insights access. No RBAC changes are required.
- Deployment ZIP: 31 source files, no `__pycache__`, SHA-256
  `439a4240b3ac525be62e7864821a06ffcc76b6ea1548104db6247fb5d7ed25bc`.

## Deployment evidence

- Azure One Deploy ID: `ca4ded81-bd3d-4079-991c-b1beea194e85`.
- Azure deployment status: `4` (successful).
- Post-deployment `GET /ping`: HTTP 200 with `environment=dev`.
- Post-deployment `GET /openapi.json`: HTTP 200; 19 paths exposed and
  `/api/v1/sync/push` present.
- Physical device install: bundle `app.ankemoney.ios` installed and launched
  successfully on iPhone Air `C4D8864F-54D5-5487-A447-AFA40069787B`.
- Device sync proof after deployment: last sync success at
  `2026-08-21 10:34:36 UTC`; local cloud outbox count `0`; sync cursor `162`;
  active local ledger entries `60`.
