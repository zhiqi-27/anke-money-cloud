# Install the Anke Money Agent Skill

The Skill is installed in the user's Agent host. It contains instructions and
the Remote MCP dependency only; it never contains an Anke Money access token,
Firebase credential, Cosmos credential, or household ID.

## Create the connection in Anke Money

1. Open Agent authorization in the signed-in Anke Money app.
2. Choose `Skill` as the immutable integration.
3. Select only the scopes needed by the Agent and choose an expiry.
4. Create the authorization and copy the one-time connection package.

The package for a Skill connection contains `mcpUrl`, short-lived
`accessToken`, token expiry, `refreshToken`, and grant expiry. It intentionally
does not contain an API base URL because Skill credentials are accepted only by
Remote MCP.

## Install in an Agent host

Import or copy the repository directory
`skills/anke-money-agent` using the host's normal Skill installation mechanism.
The host reads `agents/openai.yaml`, which declares the `anke-money` Streamable
HTTP MCP dependency.

Configure that MCP connection with:

- URL: the copied `mcpUrl`;
- authorization: `Bearer <accessToken>` in the host's protected credential
  store;
- refresh credential: store `refreshToken` only if the host supports the Anke
  token-refresh flow.

Never paste either token into `SKILL.md`, `openai.yaml`, a prompt template, a
shell command, logs, or source control. If the host cannot protect a bearer
credential, do not connect it.

## Use and revoke

Invoke `$anke-money-agent` or make a matching money request. Reads use the
granted scopes. The Skill must show the exact proposed ledger or asset change and
obtain explicit confirmation immediately before a write.

The owner can revoke the connection in Anke Money at any time. Revocation is
checked on the next MCP request. Changing scopes or switching between API, MCP,
and Skill requires a new connection; an existing grant is never widened or
relabelled.
