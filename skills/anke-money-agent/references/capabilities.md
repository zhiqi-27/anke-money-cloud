# Anke Money capability reference

The MCP connection uses the revocable, long-lived API Key created by the Anke Money
owner. The server derives the connection ID, household ID, all six capabilities,
and `skill` operation source from that credential. Never send a household ID or
source as a tool argument.

## Read tools

| Tool | Required scope | Purpose |
| --- | --- | --- |
| `ledger_read` | `ledger:read` | List ledger entries. |
| `assets_read` | `assets:read` | List asset accounts and dated snapshots. |
| `categories_read` | `categories:read` | List ledger categories. |
| `channels_read` | `channels:read` | List payment channels. |

Each read tool accepts an optional `limit` from 1 through 500. Use the smallest
limit practical for the task.

## `ledger_create`

Required scope: `ledger:create`.

Append exactly one entry. Required arguments are `id`, `idempotency_key`,
`kind`, `direction`, `occurred_at`, `month_start`, `category_id`, and
`amount_in_fen`. `channel_id` is required for an expense and absent for income.
`note` is optional. UUIDs must be new for a new entry. Timestamps include a time
zone, `month_start` is the first calendar day, and the amount is positive integer
fen.

## `assets_update`

Required scope: `assets:update`.

Append one dated snapshot to exactly one existing asset account. Required
arguments are `account_id`, `snapshot_id`, `idempotency_key`, `amount_in_fen`,
and timezone-aware `observed_at`. `member_profile_id` is optional. Resolve the
account with `assets_read`; never guess it. A separate confirmed update needs a
new snapshot ID and idempotency key.

## Write receipt and errors

A successful result includes `replayed`. `false` means the write was accepted;
`true` means the exact request already succeeded. The server binds an
idempotency key to the full request and connection identity, so changing any
argument while reusing the key is an error.

Invalid or revoked credentials, validation failures, and attempts to update another
household are terminal for that call. Explain the constraint without retrying under
a different tool or credential.
