# Anke Money capability reference

The MCP connection uses the revocable, long-lived API Key created by the Anke Money
owner. The server derives the connection ID, household ID, all six scopes,
and `skill` operation source from that credential. Never send a household ID or
source as a tool argument.

## Read tools

| Tool | Required scope | Purpose |
| --- | --- | --- |
| `ledger_read` | `ledger:read` | List ledger entries for an optional date interval. |
| `assets_read` | `assets:read` | List asset accounts and dated snapshots for an optional date interval. |
| `categories_read` | `categories:read` | List ledger categories. |
| `channels_read` | `channels:read` | List payment channels. |

`ledger_read` and `assets_read` accept optional inclusive `start_date` and
`end_date` values, an optional opaque `cursor`, and `limit` from 1 through 500.
Their result includes `nextCursor` and `hasMore`. For a complete export or report,
keep the same date interval and pass each returned cursor until `hasMore` is false.
Asset-account metadata is included even when its dated snapshots fall outside the
requested interval. `categories_read` and `channels_read` accept `limit` only.

## `ledger_create`

Required scope: `ledger:create`.

Append exactly one entry. Required arguments are `id`, `idempotency_key`,
`kind`, `direction`, `occurred_at`, `month_start`, `category_id`, and
`amount_in_fen`. The legacy argument name always carries an integer amount in the
workspace accounting currency's ISO minor unit; the service records that currency
automatically. `channel_id` is required for an expense and absent for income.
`note` is optional. UUIDs must be new for a new entry. Timestamps include a time
zone, `month_start` is the first calendar day, and the amount is a positive integer
minor-unit value.

## `ledger_create_batch`

Required scope: `ledger:create`.

Append 1 through 25 entries after one explicit confirmation covering the complete
proposed batch. `entries` contains the same fields as `ledger_create`; every entry
requires a unique entity UUID and idempotency UUID. Larger documents use multiple
unchanged chunks. Retrying an identical chunk returns each entry as created or
replayed without duplication. The raw source document is not a tool argument and
must remain in the Agent host.

## `assets_update`

Required scope: `assets:update`.

Append one dated snapshot to exactly one existing asset account. Required
arguments are `account_id`, `snapshot_id`, `idempotency_key`, `amount_in_fen`,
and timezone-aware `observed_at`. The amount uses the workspace accounting
currency's integer minor unit. `member_profile_id` is optional. Resolve the
account with `assets_read`; never guess it. A separate confirmed update needs a
new snapshot ID and idempotency key.

## `assets_create`

Required scope: `assets:update`.

Create exactly one asset account and its initial dated snapshot after explicit
confirmation. First use `assets_read` to avoid accidental duplicates and
`categories_read` to resolve an active compatible asset category. Required
arguments are `account_id`, `snapshot_id`, `idempotency_key`, `name`, `kind`,
`category_id`, `amount_in_fen`, and timezone-aware `observed_at`.

`kind` is `asset` or `liability`. Assets require `asset_group` from `financial`,
`living`, `interest`, or `receivable`; liabilities omit it. Financial assets also
require `money_bucket` from `flexible`, `stable`, or `risk`; every other account
omits it. `member_profile_id` is optional. Amounts are non-negative integer
minor-unit values in the workspace accounting currency.
The category must be active and have the same asset group, or liability scope, as
the proposed account.

## `assets_create_batch`

Required scope: `assets:update`.

Create 1 through 25 new asset accounts after one explicit confirmation covering
the complete proposed batch. `accounts` contains the same fields as
`assets_create`; every item requires unique account, snapshot, and idempotency
UUIDs. Larger documents use multiple unchanged chunks. Retrying an identical
chunk returns each account as created or replayed without duplication. This tool
does not update accounts that already exist.

## Write receipt and errors

A successful single-write result includes `replayed`. Asset creation also returns
the created account and initial snapshot. `false` means the write was
accepted; `true` means the exact request already succeeded. A batch result includes
per-entry results plus `createdCount` and `replayedCount`. The server binds an
idempotency key to the full request and connection identity, so changing any
argument while reusing the key is an error.

Invalid or revoked credentials, validation failures, and attempts to update another
household are terminal for that call. Explain the constraint without retrying under
a different tool or credential.
