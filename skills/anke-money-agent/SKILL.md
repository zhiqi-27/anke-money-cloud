---
name: anke-money-agent
description: Safely work with one user-authorized Anke Money household through its Remote MCP tools. Use when a user asks to read ledger entries, assets, categories, or payment channels, create one ledger entry, or update one asset balance. Enforces confirmation before writes, stable idempotency keys, and narrow non-destructive operations.
---

# Anke Money Agent

Use the connected Anke Money MCP server as the only data path. The connection
uses the owner's long-lived Anke Money API Key, which binds one identity and one
household and enables all six Agent capabilities. It remains valid until the owner
resets or revokes it.

## Workflow

1. Match the request to exactly one of the six tools in
   [capabilities.md](references/capabilities.md).
2. Use read tools to resolve stable category, channel, account, or ledger IDs
   when needed. Never guess an ID.
3. Before `ledger_create` or `assets_update`, show the proposed change and ask
   for explicit confirmation. Do not treat an earlier general request as that
   confirmation.
4. After confirmation, generate one UUID idempotency key. Reuse it only when
   retrying the exact same write, with every argument unchanged.
5. Report the returned result, including whether it was replayed. Tell the user
   the write is visible in Anke Money's remote-operation audit.

## Safety boundaries

- Use only the six tools exposed by the active connection.
- Never request, infer, or switch to another household.
- Never permanently delete, change authorization, import bank or payment
  statements, or perform an unconfirmed bulk asset update.
- `ledger_create` appends one entry. Do not represent it as editing history.
- `assets_update` changes exactly one account by appending one dated snapshot.
- Keep money in integer fen. Reject floating-point currency values.
- Do not put credentials, access tokens, private notes, or unrelated record
  payloads into prompts, summaries, or logs.
- If the API Key is invalid or revoked, stop and ask the user to create or reset it
  in Anke Money. Never work around it.

Load [capabilities.md](references/capabilities.md) when choosing a tool or
constructing its arguments.
