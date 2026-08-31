# Anke Money Production Deployment Plan

Status: Validated

## Objective

Prepare and, after validation, deploy the Anke Money Cloud API to one isolated
Production Azure environment for the single-maintainer release path.

## Scope

- One Production Azure Functions app.
- One independent Production Cosmos DB account/database/containers.
- One Production Key Vault with managed-identity access.
- One Production Application Insights / Azure Monitor setup.
- Production Clerk instance and APNs provider credentials.
- Vercel hosts the public website and carries only the minimal Clerk Frontend
  API forwarding route required by the current Production Clerk instance;
  the website has no user login state.
- No Staging environment, multi-region failover, blue/green deployment, or
  third-party crash SDK in this release.

## Non-goals

- No deletion of Development resources.
- No migration of real user data.
- No credentials in Git, iOS binaries, or this plan.
- No automatic account, subscription, or signing-profile changes.

## Architecture decisions

- `ANKE_ENVIRONMENT=prod` is required for the deployed Function App.
- Function App uses managed identity for Azure resource access where supported.
- Secrets are stored in Key Vault and referenced from Function App settings.
- Production data is isolated from Development data and Clerk issuer/configuration.
- The API verifies the Clerk Production issuer/JWKS through the existing public
  Clerk proxy URL. If a future custom domain replaces the current website host,
  only these two non-secret URLs and the proxy URL need to change.
- Production deployment uses a pinned Cloud revision and a separate iOS Release
  configuration.
- The existing Azure subscription is reused, while the Production resource group,
  Function App, Cosmos account, Key Vault, managed identity, Storage account,
  Log Analytics workspace, and Application Insights component are all new.
- East Asia is selected to match the existing Development deployment and passed
  the current regional-availability and quota checks for the planned resource set.

## Azure context

- Subscription: `Azure subscription 1`
  (`a1187bf2-2e2f-4e05-aaea-407163a009f5`)
- Tenant: `5ba85645-4c35-4efd-93ea-11c0890472d8`
- Resource group: `anke-money-prod`
- Location: `eastasia`
- Provisioning: standalone Bicep with Azure CLI `az deployment group`

## Infrastructure artifacts

- `infra/main.bicep` — isolated Production resources and least-privilege role
  assignments.
- `infra/main.parameters.json` — non-secret Production defaults only.
- Cosmos containers: `anke_entities` (`/householdId`), `anke_identities`
  (`/uid`), and `anke_sync_leases` (`/id`).
- Cosmos backup: Continuous 7-day backup; Storage blob/container soft delete is
  enabled for the deployment storage account.
- Function hosting: Linux Flex Consumption `FC1`, Python 3.11, HTTPS-only,
  managed-identity storage, and Application Insights diagnostics.
- Alert: valid Flex Consumption CPU saturation metric alert; email action group is
  enabled only after an alert recipient is supplied outside source control.

## Required inputs

- Production Clerk issuer, JWKS URL, audience, and backend secret.
- APNs team ID, key ID, topic, and private key.
- Alert recipient and an authorized Azure identity.
- The exact Flex Consumption hostname after infrastructure provisioning, used to
  set `ANKE_MCP_ALLOWED_HOSTS` and the iOS Release API URL.
- Production Clerk publishable key for the iOS Release configuration.
- Formal Apple Distribution profile/certificate and a physical device for APNs.
- The Azure subscription and target region are fixed above; generated names use a
  deterministic resource token and are emitted as deployment outputs.

## Validation evidence required before deployment

- Local Cloud test suite and compile checks pass.
- Infrastructure validation passes with no unresolved warnings that affect
  identity, secret access, data isolation, backup, or monitoring.
- Production settings contain no Development endpoint or test Clerk issuer.
- A documented synthetic-account smoke test exists for auth, isolation,
  idempotency, deletion, and push-token registration.

## Validation steps

- All validation checks pass
  - Core Validation (CLI, auth, build, validate, what-if) — run the Bicep
    `validate-deployment` script.
  - Linting (optional).
  - Azure Policy Validation.
- Build `infra/main.bicep` with the Bicep CLI and validate
  `infra/main.parameters.json` as JSON.
- Run the Cloud Python unit suite, compile check, and `git diff --check`.
- Review all managed-identity RBAC assignments and verify their resource scopes.
- Run a non-mutating Azure deployment `what-if` for `anke-money-prod`.
- Do not deploy secrets or real user data during infrastructure validation.

## Functional verification

- Status: Local backend, infrastructure, deployed runtime, backup restore,
  signed-archive, final TestFlight candidate, physical-device Production
  authentication, and physical-device Production APNs verification passed.
- Backend: `108` tests passed with `.venv/bin/python -m unittest discover -s test
  -p 'test_*.py'`; `.venv/bin/python -m compileall -q app test function_app.py`
  passed.
- UI: iOS Release configuration, fail-closed checks, and a signed Apple
  Distribution Archive with `aps-environment=production` passed. App Store
  Connect build `0.1.0 (3)` is complete, export compliance is recorded, the
  build is in the existing `anke test` internal group, and TestFlight build 3
  is installed on the connected physical device.
- Cloud: deployed route boundary, Production Clerk authentication, authenticated
  data flow, backup, monitoring, and physical-device Production APNs delivery
  checks passed.

## Post-deployment evidence required

- `/ping` reports `environment=prod` without sensitive values.
- Unauthenticated protected routes return `401`.
- Synthetic Production identities can authenticate and are cleaned up.
- Cross-household access is rejected.
- Exact idempotent replay does not duplicate data.
- Account deletion succeeds and is safe to retry.
- APNs Production delivery is verified on a real device.
- Backup policy and one isolated restore are verified.
- Function/Cosmos monitoring is configured; `/ping` and the App Insights
  ConfigurationError query passed after deployment. The CPU alert was not
  artificially triggered, so the live threshold was not disturbed.

## Deployment evidence (2026-08-25)

- Infrastructure deployment succeeded in `anke-money-prod` using the validated
  Bicep revision. The Production Function is
  `azfzepi6zekanwh2.azurewebsites.net`; Cosmos, Key Vault, Storage, Application
  Insights, Log Analytics, and the managed identity are independent resources.
- Production Cloud package upload completed after the Production settings and
  Key Vault reference identity were fixed. `GET /ping` returned HTTP `200` with
  `environment=prod`; protected `GET /api/v1/me` returned `401`; and an invalid
  Clerk exchange returned `401`. Recent Application Insights queries showed no
  `ConfigurationError` rows after the fix.
- Production Clerk uses the matching Production instance through
  `https://anke-money-website.vercel.app/__clerk` for issuer and JWKS. The
  website has no user login state; Vercel hosts the public site and carries only
  the minimal Clerk Frontend API forwarding route required by this Clerk
  Production instance for native authentication.
- The final synthetic Production smoke used two temporary Clerk users through
  the Vercel proxy and the deployed Function: both Clerk exchanges and `/me`
  returned `200`; bootstrap, migration stage/activation, and Production APNs
  token registration returned `200`; exact mutation and delete replays returned
  the original `accepted` result without duplicate changes; pull showed one
  entity and one tombstone; a cross-household device returned
  `deviceNotRegistered`; account deletion returned `204`, its retry returned
  `204`, post-delete sync returned `409`, and the final Clerk query returned
  zero synthetic users. No synthetic credentials or tokens were retained.
- Key Vault runtime access is assigned through the Production managed identity
  with `Key Vault Secrets User`, and the Function uses that identity for app
  setting references. The new Clerk secret version is enabled; the previously
  entered version is disabled and retained for recovery/audit. Key Vault's
  supported delete operation is whole-secret scoped, so version-level disable is
  the safe retirement action here.
- Initial iOS Production Archive: `/tmp/anke-money-production-distribution-20260825.xcarchive`.
  `scripts/verify_release_archive.sh` passed with Apple Distribution signing,
  arm64, dSYM UUID, and signed `aps-environment=production`, using the formal
  `AnkeMoney TestFlight 2026-08-12` Distribution profile.
- App Store Connect/Xcode upload completed for Anke Money `0.1.0 (2)`. App Store
  Connect processing completed, the export-compliance declaration was recorded,
  and the build is `Testing` in the existing `anke test` internal group.
- Physical-device Production authentication was completed on 2026-08-26 with a
  production-configured local Debug build. The original build 2 configured the
  Production publishable key but did not pass the configured
  `https://anke-money-website.vercel.app/__clerk` proxy URL to ClerkKit, so the
  SDK attempted an unavailable direct FAPI host. The fix wires
  `Clerk.Options(proxyUrl:)` through `ClerkProxyURL`. The email flow now falls
  back from sign-in to sign-up only for Clerk's account-not-found codes, matching
  the product promise that a new email account is created after verification.
- A second physical-device issue incorrectly classified a freshly created,
  account-scoped replica as conflicting because startup had already inserted
  deterministic system channels, categories, and default settings. The attach
  guard now permits only those system seeds and still rejects custom reference
  data, ledger/asset data, account metadata, cutover markers, outbox mutations,
  tombstones, and deletion markers. Read-only device-container verification
  confirmed one attached workspace/user/household/device/connection, matching
  account scoping, with no ledger, asset, outbox, tombstone, or blocked-conflict
  rows. The previous account's replica remained isolated and unchanged.
- Final iOS Production Archive:
  `/tmp/anke-money-production-final-20260826.xcarchive`. The archive verifier
  passed for Anke Money `0.1.0 (3)` with Apple Distribution signing, arm64,
  email-code-only Production authentication, the formal
  `AnkeMoney TestFlight 2026-08-12` profile, and signed
  `aps-environment=production`. Xcode uploaded build 3 successfully; App Store
  Connect reports the upload `Complete`, records export compliance, and lists
  `anke test` as an associated internal group. The connected iPhone reports
  installed bundle `app.ankemoney.ios` version `0.1.0 (3)`.
- Build 3 registered a 64-character APNs token with the Production environment.
  The local APNs provider key's public-key fingerprint matched the Production
  Key Vault `apns-private-key`; the Key ID and Team ID also matched the deployed
  Production settings. With Anke Money moved to the background by foregrounding
  Settings, a data-free notification containing only
  `content-available=1` and `reason=changesAvailable` was sent to the Production
  APNs endpoint at `2026-08-26T02:55:18.884213Z`. Apple returned HTTP `200` and
  an APNs ID. Application Insights then recorded the physical device's
  authenticated Production `POST /api/v1/sync/pull` at
  `2026-08-26T02:55:20.5360111Z`, HTTP `200`, 1.65 seconds after the send. This
  background correlation excludes the foreground polling path and closes the
  physical-device Production APNs gate.

## Admin directory fallback update (2026-08-31)

- Commit `884f831` adds a server-side Clerk Backend API directory projection to
  admin account search and detail reads. Accounts without an Anke
  `identityMembership` are shown as `householdReady=false`; manual grants still
  require the server-owned identity/household to be ready.
- The code-only Flex OneDeploy to `azfzepi6zekanwh2` completed as deployment
  `25b8157b-da12-436a-a997-e72214a6d9ce` with Azure status `4`.
- Post-deployment `GET /ping` returned `environment=prod`,
  `GET /openapi.json` returned `200`, and unauthenticated admin search returned
  `401`. No real account lookup, Cosmos write, or entitlement mutation was
  performed during verification.

## Backup and monitoring evidence (2026-08-25)

- Production Cosmos reports Continuous backup with the `Continuous7Days` tier,
  local key authentication disabled, and Session consistency.
- An isolated point-in-time restore of `anke-money-prod` was created as
  `azrankeprodverify0825` at a valid restore timestamp. The restored account
  reached `Succeeded` and contained database `anke_money_prod` plus the three
  expected containers and partition paths. Azure subsequently confirmed the
  isolated restore account was `ResourceNotFound`; the source Production
  account was not modified.
- Azure Monitor alert `azazepi6zekanwh2` is enabled for Function
  `CpuPercentage > 85` averaged over 5 minutes, severity 2, and routes to the
  enabled action group `azagzepi6zekanwh2` for the configured maintainer email.
  `/ping` was also checked after deployment with HTTP `200`; no alert was
  artificially triggered.
- The alert action-group binding was reattached and then reproduced through the
  validated Bicep parameters using deployment
  `anke-money-prod-alert-binding-20260825`; the final alert resource reports the
  enabled action group and the recent App Insights ConfigurationError count is
  `0`.

## Role Assignment Verification

- Status: Verified by static Bicep review.
- Identity: one Production user-assigned managed identity attached to the Function
  App.
- Roles confirmed: Cosmos DB Built-in Data Contributor; Storage Blob Data Owner
  and Contributor; Storage Queue Data Contributor; Storage Table Data Contributor;
  Monitoring Metrics Publisher; Key Vault Secrets User.
- Scope confirmed: Cosmos role is scoped to the Production Cosmos account; Storage
  roles to the Production Storage account; Metrics Publisher to Production App
  Insights; Key Vault role to the Production vault. No role assignment is scoped
  to the subscription or resource group.
- Note: Blob Owner is retained because the Flex deployment rules require it;
  Key Vault access is limited to the read-only Secrets User role required for
  resolving app-setting references. The identity is otherwise dedicated to
  this one Production Function App.

## Rollback

- Stop serving the new iOS Release build.
- Re-deploy the last known-good pinned Cloud package.
- Do not delete Production resources or data as a rollback action.

## Validation Proof

- 2026-08-28 subscription update: `.venv/bin/python -m compileall -q app test`
  passed and `.venv/bin/python -m unittest discover -s test` passed all `111`
  tests, including Apple entitlement binding, cross-account protection, and
  notification-driven expiration.
- 2026-08-28 Azure validation: the azure-validate Bicep workflow passed Azure
  CLI authentication, Bicep compilation, resource-group template validation,
  and what-if against subscription `a1187bf2-2e2f-4e05-aaea-407163a009f5`
  and resource group `anke-money-prod`. The what-if reported Create `8`, Modify
  `31`, Delete `27`; therefore this release must use code-only Function package
  deployment and must not apply the infrastructure template.
- 2026-08-28 static RBAC review reconfirmed the dedicated Production managed
  identity and resource-scoped Cosmos data, Storage data, Metrics Publisher,
  and Key Vault Secrets User assignments. No RBAC change is required for Apple
  signed-data verification.

- Bicep CLI `0.38.5` lint and build passed for `infra/main.bicep`.
- `infra/main.parameters.json` passed JSON validation.
- Secret scan found no Clerk keys, APNs private key, or session secret in `infra/`.
- `git diff --check` passed.
- Azure regional availability and quota checks passed for East Asia; the planned
  resource types reported no constrained quota except Storage, which had 249
  available account slots.
- Resource group `anke-money-prod` was created empty in the confirmed
  subscription and region.
- The initial Azure deployment `what-if` passed with exactly `25` planned
  `Create` changes and no `Modify` or `Delete` changes. No Function code, secret
  value, or user data was written during that preview.
- The first ARM attempt was stopped by Azure's Flex Consumption validation because
  the deprecated `FUNCTIONS_WORKER_RUNTIME` setting was present. It was removed
  together with the deprecated `FUNCTIONS_EXTENSION_VERSION` setting; the runtime
  remains declared in `functionAppConfig.runtime` as required by Flex.
- After aligning the Production Clerk issuer/JWKS with its existing Vercel
  proxy route, the Bicep validation workflow was rerun: CLI/auth, Bicep build,
  deployment validation, and what-if all passed. The resource-ID what-if showed
  25 resources to deploy and 2 ignored existing resources, with no resource
  deletion; the helper's property-level +/- count is not a resource deletion
  count.
- The Production proxy endpoints returned HTTP `200` for both
  `/__clerk/.well-known/jwks.json` and `/__clerk/v1/client`; no Clerk secret
  value was read or written to source control.
- The Cloud suite was rerun after the configuration change: `108` tests passed;
  compile, JSON, and `git diff --check` also passed.
- After the Flex fix, a second `what-if` passed with `3 Create`, `16 Modify`, and
  `6 NoChange` changes and no top-level `Delete`. The modifications are limited
  to the partially provisioned resource defaults, the database autoscale setting,
  and managed-identity RBAC principal propagation; no Cosmos container or user
  data deletion is planned.
- The second ARM attempt exposed that Flex Consumption does not publish an
  `Http5xx` site metric. The alert now uses the verified `CpuPercentage` metric
  at an 85% average threshold; `/ping`, the action-group binding, and the
  App Insights failure query were rechecked after the final deployment.

- 2026-08-30 admin entitlement release validation: the Azure validation workflow
  completed with Azure CLI authentication, Bicep compilation, resource-group
  template validation, and what-if against subscription
  `a1187bf2-2e2f-4e05-aaea-407163a009f5` and resource group `anke-money-prod`.
  The what-if reported Create `8`, Modify `31`, Delete `27`; this release uses
  code-only Function package deployment and does not apply the infrastructure
  template.
- 2026-08-30 Cloud gates: `.venv/bin/python -m unittest discover -s test -p
  'test_*.py'` passed all `118` tests; compileall, Bicep build, parameters JSON
  validation, and `git diff --check` passed.
- 2026-08-30 static RBAC review reconfirmed one dedicated user-assigned
  Production identity with resource-scoped Cosmos DB Built-in Data Contributor,
  Storage Blob Owner/Contributor, Storage Queue/Table Contributor, Monitoring
  Metrics Publisher, and Key Vault Secrets User role IDs. No subscription- or
  resource-group-scoped data role was introduced by this release.
- 2026-08-30 deployed-route smoke found that Azure Functions reserves `/admin/*`
  for host management, so the admin API was moved to `/internal/admin/v1/*` and
  the BFF, contract, IA, and tests were updated. Focused admin and billing tests
  (`10`) plus compileall and secret scan passed after the route change.
