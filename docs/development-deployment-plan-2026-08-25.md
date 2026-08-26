# Azure Development Deployment Plan (Preserved)

This historical Development deployment plan was preserved when the active
`.azure/deployment-plan.md` was repurposed for the Production deployment.

Status: Validated

## Objective

Deploy the prepared Anke Money Development Azure Functions backend update for
ISO minor-unit amounts, original transaction currencies, accounting-currency
settings, and backward-compatible CNY synchronization.

## Scope

- Target: existing Development Function App `func-anke-money-dev-zq01`.
- Environment: Development only; no Production deployment.
- Data safety: no account, Cosmos data, or device-local data deletion.
- Follow-up: rebuild and install the matching iOS app on the connected physical
  device, then verify launch against the Development configuration.

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
Key Vault, managed identity, or Production resources.

## Validation proof

- Local Python unit suite: 104 tests passed on 2026-08-25.
- Local compile and `git diff --check` passed.
- Existing `GET /ping`: HTTP 200 with `environment=dev`.
- Development One Deploy ID: `ca4ded81-bd3d-4079-991c-b1beea194e85`.
- Development deployment status: `4` (successful).
- Post-deployment `GET /ping`: HTTP 200 with `environment=dev`.
- Post-deployment `GET /openapi.json`: HTTP 200 with the sync route present.
- Physical device launch and Development sync were recorded separately in the
  original release evidence.

