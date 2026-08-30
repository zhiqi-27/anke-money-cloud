# Anke Money Admin information architecture

Status: draft v0.1 · 2026-08-30  
Product: future `anke-money-admin`  
Audience: Anke Money operator/support administrator

This document defines the first formal management web experience for account
lookup and Pro entitlement operations. It is intentionally narrower than a
general household dashboard: administrators manage service access, not user
financial content.

## 1. Product intent

Anke Money Admin should make one sensitive action safe and legible:

> 找到一个账户，确认它当前为什么是 Pro，然后在有理由、有期限、有记录的情况下授予或撤销 Pro。

The web app is an operational workspace, not a marketing page and not a copy of
the iOS four-tab navigation. It uses Anke Money's calm, warm visual language
while giving the operator the density and traceability needed for support work.

### Non-goals for v1

- browsing or editing ledger entries, assets, notes, or household data;
- changing Apple products, prices, or App Store configuration;
- bulk Pro grants or CSV imports;
- managing Clerk users, passwords, or social-provider credentials;
- analytics dashboards about spending or revenue;
- a public customer account portal.

## 2. Deployment and ownership shape

The management UI is a separate `anke-money-admin` Next.js application. The
public `anke-money-website` remains public-site-only; its repository scope
already separates public pages from the iOS app and cloud services.

The UI is deployed independently from the Python Azure Function API:

```text
Anke Money Admin (Next.js, Vercel default HTTPS host)
        │ Clerk administrator session
        │ Next.js server proxy / HttpOnly cookie
        ▼
Anke Cloud Admin API (Azure Function, /admin/v1/*)
        │ Managed identity, no Cosmos key in browser
        ▼
Cosmos manualProGrant + redacted admin audit
```

No custom domain is needed for local work or the first protected preview. The
Vercel preview/default host is an access address, not a security boundary; the
admin allowlist and backend authorization remain mandatory. A later custom
domain changes DNS and environment values, not the information architecture.

## 3. Primary navigation

Desktop is the primary layout; mobile remains usable for a quick support action.
The shell has a compact left rail/sidebar and a quiet top bar:

```text
Anke Money mark

概览             /
账户             /users
Pro 权限         /entitlements
操作记录         /audit

环境：Development / Production
管理员账户       退出登录
```

The environment badge is always visible and uses a non-ambiguous label. A
Production badge is not a decorative color choice and cannot be hidden by the
operator.

### Route tree

```text
/sign-in
/                         -> redirect to /overview after auth
/overview                 -> operational counters and recent actions
/users                    -> account search/list
/users/[uid]              -> account detail
/users/[uid]/entitlement  -> source breakdown and grant history
/entitlements             -> manual-grant list, filters, expiring items
/audit                    -> redacted administrative audit log
/settings                 -> admin access/help, no user-data controls in v1
```

The first implementation may merge `/entitlements` into `/users` and expose it
as a filter if that keeps the navigation lighter. The route contract remains
stable so a dedicated list can be introduced later.

## 4. Page specifications

### 4.1 Sign-in

Purpose: establish the administrator identity before loading any account data.

Content:

- Anke Money mark and the short message “管理 Anke Money 服务权限”;
- Clerk sign-in for the invited administrator account;
- a calm, explicit “此入口仅供 Anke Money 管理员使用” notice;
- generic failure states; do not reveal whether an email is an admin.

After Clerk authentication, the server proxy obtains an admin-authorized session
for the cloud API. A non-admin reaches a clear forbidden state and cannot see
the shell or search results.

### 4.2 Overview (`/overview`)

Purpose: answer “现在有没有需要处理的 Pro 权限问题？” without presenting a
financial SaaS dashboard.

Layout:

1. Page title: `概览` / `Overview`.
2. Four quiet metric cards:
   - active Pro accounts;
   - active manual grants;
   - grants expiring in 7 days;
   - recent admin actions.
3. “最近操作” list with actor, target, action, result, and time.
4. One primary action: `查找账户`.

Empty state: explain that no manual grants exist yet and point to account
search. Never show fake chart data or a zero-value financial chart.

### 4.3 Accounts (`/users`)

Purpose: find one user quickly and make the current entitlement understandable.

Layout:

- wide search field with examples: email, Clerk UID, Anke UID;
- status filters: `全部`, `Pro`, `免费`, `手动赠送`;
- result count and a compact result table/list;
- rows show display name, masked email when appropriate, UID suffix, Pro source,
  expiry, and a `查看` action.

Search behavior:

- exact UID/Clerk subject match wins;
- normalized email/display-name search follows;
- no results state offers spelling guidance, not an invitation to create a
  user;
- search and list requests are cursor-based and do not load financial data.

Desktop table columns:

| Column | Meaning |
| --- | --- |
| 账户 | display name, provider, shortened UID |
| 当前权限 | `Pro` / `免费` plus source |
| 到期 | date, `永久`, or `—` |
| 最近操作 | latest admin action or `无` |
| 操作 | open detail |

On narrow screens the row becomes a stacked card; no horizontal scrolling is
required for the primary action.

### 4.4 Account detail (`/users/[uid]`)

Purpose: provide one complete, safe decision surface before a grant/revoke.

Top area:

- breadcrumb `账户`;
- account display name and provider;
- copyable-but-explicitly-labelled Anke UID (never a secret);
- email and identity creation time;
- a compact status card: `当前为 Pro` / `当前为免费`.

Entitlement card:

- effective status and expiry;
- source chips: `Apple 订阅` and/or `手动赠送`;
- link to `查看权限明细`;
- no Apple transaction mutation controls.

Action area:

- `赠送 Pro` primary action;
- `查看操作记录` secondary action;
- `撤销手动赠送` appears only for an active manual grant;
- no bulk or destructive account controls.

The page must not display ledger totals, assets, notes, device tokens, API keys,
or raw provider credentials.

### 4.5 Grant flow

Entry: `赠送 Pro` from account detail.

Use a focused modal or side sheet with four steps visible in one short form:

1. `授权类型`: fixed term (default) or lifetime;
2. `开始时间`: UTC-backed local date/time display;
3. `结束时间`: required for fixed term;
4. `原因`: required short explanation.

Before submission, show an explicit summary:

```text
将为 person@example.com
赠送 Anke Money Pro
2026-08-30 00:00 — 2026-09-29 23:59 UTC
原因：Beta tester access
```

The operator must click `确认赠送`. A lifetime grant has a stronger warning and
the confirm label becomes `确认永久赠送`. The API receives one idempotency key;
double-clicking cannot create two grants.

Success returns to account detail, refreshes the effective entitlement, and
shows the new audit entry. Failure preserves the form values and explains
whether the problem is authorization, validation, target readiness, or service
availability.

### 4.6 Revoke flow

Entry: active manual grant's `撤销` action.

The confirmation presents the exact grant source, target, and effective time of
revocation. A reason is required. The action uses the semantic destructive color
only for the final button. If an Apple subscription remains active, the result
explicitly says that Apple access remains and the account may still be Pro.

### 4.7 Entitlements (`/entitlements`)

Purpose: operational follow-up, not a second user directory.

Filters:

- active / expired / revoked;
- manual only;
- expires within 7 / 30 days;
- created by administrator;
- cursor pagination.

Rows show target account, grant period, reason, creator, status, and detail link.
The default view excludes Apple transaction rows unless the operator chooses
`显示 Apple 来源` for troubleshooting.

### 4.8 Audit (`/audit`)

Purpose: answer “谁在什么时候给谁做了什么？”

The audit list is newest-first with filters for target UID, action, outcome, and
date range. Each row shows actor, target, action, reason, timestamp, and request
ID. It never shows secrets, complete request bodies, ledger content, or raw
tokens. A detail drawer may show the before/after entitlement summary, not the
whole Cosmos document.

### 4.9 Settings (`/settings`)

V1 is intentionally small:

- current environment and API health;
- current administrator identity;
- sign-out;
- a short support/runbook link.

Admin allowlist editing, user impersonation, and credential rotation are not
performed inside this web app in v1; they remain Azure/Clerk operations.

## 5. Core interaction states

Every data surface must specify loading, empty, error, and stale states:

| State | UI behavior |
| --- | --- |
| Loading | Skeleton rows/cards that preserve layout; do not flash fake counts. |
| No search result | “没有找到匹配账户” plus accepted search formats. |
| Target not ready | Explain that the account must finish cloud initialization; no grant button submission. |
| Free | Neutral status card with one primary `赠送 Pro` action. |
| Apple Pro | Source is explicit; manual actions do not mutate Apple records. |
| Manual Pro | Show grant period, reason, creator, and revoke action. |
| Apple + manual | Show both sources and explain that revoking manual access may leave Apple access. |
| Expired/revoked | Keep history visible; never erase the audit trail. |
| API unavailable | Generic service error with request ID and retry; do not expose stack traces. |
| Forbidden | Leave the admin shell inaccessible to non-admin users. |

## 6. Visual and brand direction

### 6.1 What should feel like Anke Money

The admin web should share the product's colors, typography, restraint, and
clarity. It must not become a generic banking dashboard or an enterprise
control-room theme.

Use the established palette:

| Role | Token / value | Use |
| --- | --- | --- |
| Cream Canvas | `#FBFAF7` | page background and breathing room |
| Surface | `#FFFFFF` | opaque content cards and table surfaces |
| Deep Ocean Blue | `#195B8F` | primary action, active navigation, links |
| Ocean Dark | `#123D5C` | headings, high-contrast operational labels |
| Ocean Light | `#D8E9F2` | selected filters and source containers |
| Ocean Mist | `#EEF5F8` | quiet table/card grouping |
| Golden Sand | `#E8B86D` | Pro marker, small completion/high-value accent |
| Primary Text | `#17202A` | readable content |
| Secondary Text | `#667085` | metadata and helper copy |
| Income / success | `#3E8E63` | success only |
| Expense / destructive | `#B86B5A` | revoke or blocking negative state only |

Use the web tokens already established in `anke-money-website/tokens.css` as the
starting point. Display text may use Bricolage Grotesque; body/UI text uses
Geist with PingFang fallback. Titles are compact (roughly 24–26px), table text
is comfortable (13–15px), and no page uses oversized marketing typography.

### 6.2 Layout language

- Desktop sidebar: approximately 224–248px, with a calm cream/surface boundary.
- Main content: max-width approximately 1180–1240px, generous but not empty.
- Card radius: 12–20px; table rows: at least 56px high.
- Controls and primary actions: minimum 44px hit height.
- Borders are hairline ocean-tinted separators; shadows are soft and sparse.
- Content cards stay opaque. Glass/material effects are limited to floating
  navigation, toolbars, sheets, and primary actions, consistent with the app
  contract.
- Golden Sand is a signal, not a background theme. Do not color every Pro row
  gold.
- The otter is a small brand reference at most (mark/sign-in empty state), not a
  repeated financial mascot or a decorative admin illustration.

### 6.3 Dark mode

The admin should follow the system preference and offer a manual light/dark
choice later if needed. Dark mode uses the existing deep blue-black canvas,
non-black surfaces, lifted ocean text, and the same semantic colors. Never
mechanically invert the cream page or introduce neon status colors.

## 7. Accessibility and responsive behavior

- Keyboard navigation is complete: sidebar, search, filters, table rows, dialogs,
  confirm/cancel, and toast announcements.
- Focus rings are visible and use a high-contrast ocean focus color.
- Every icon-only action has an accessible name; destructive actions have text,
  not color alone.
- Date and status values are text-readable and not conveyed only by chips/color.
- At 200% browser zoom and narrow widths, grant/revoke confirmation remains
  reachable without clipping.
- Tables collapse to stacked account/grant cards below the desktop breakpoint.
- Error and success announcements use an ARIA live region, while request IDs
  remain copyable for support.

## 8. Copy direction

Default locale is Simplified Chinese with English parity from the first UI
contract. Keep copy operational and calm:

| Situation | Chinese | English |
| --- | --- | --- |
| Product title | `Anke Money 管理` | `Anke Money Admin` |
| Overview | `概览` | `Overview` |
| Search | `查找账户` | `Find an account` |
| Grant | `赠送 Pro` | `Grant Pro` |
| Revoke | `撤销手动赠送` | `Revoke manual grant` |
| Apple source | `Apple 订阅` | `Apple subscription` |
| Manual source | `手动赠送` | `Manual grant` |
| Lifetime warning | `永久赠送会持续生效，直到手动撤销。` | `A lifetime grant remains active until it is revoked manually.` |
| No results | `没有找到匹配账户` | `No matching account found` |

Avoid engineering terms such as `entityType`, `partitionKey`, `JWT`, `Cosmos`,
or `source` in primary UI copy. They may appear in a developer-only diagnostic
drawer later, not in the operator's main task.

## 9. V1 delivery sequence

1. **Shell and auth** — sign-in, environment badge, protected layout, and
   forbidden state.
2. **Read-only account operations** — overview counters, account search, detail,
   entitlement source breakdown, and audit list.
3. **Safe write flow** — fixed-term grant, lifetime warning, revoke, idempotent
   feedback, and audit refresh.
4. **Operational polish** — expiring-grant filters, dark mode, keyboard/zoom
   acceptance, bilingual copy, and request-ID support affordance.
5. **Deployment evidence** — local, Development preview, and then separately
   authorized Production deployment. The default Vercel/Azure hosts are valid
   technical staging addresses; they are not proof of public brand launch.

## 10. IA acceptance gates

- Every route and primary action in the route tree has a loading, empty, error,
  forbidden, and success behavior.
- An administrator can complete search → inspect source → grant → read back →
  revoke without leaving the account detail context.
- The UI never requests or displays ledger/asset content.
- Grant and revoke summaries show target, source, effective period, reason, and
  final result before/after the write.
- Double-submit does not create duplicate grants or audit events.
- Ordinary authenticated users cannot load the shell or call the admin API.
- Light/dark, keyboard, narrow-width, and 200% zoom states retain readable
  hierarchy and reachable actions.
- Screenshots used as evidence are captured from the running admin app, not
  synthetic CSS mockups.
