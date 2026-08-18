# Install the Anke Money Agent Skill

The Skill is installed in the user's Agent host. It contains instructions and
the Remote MCP dependency only; it never contains an Anke Money access token,
Apple credential, Anke session token, Cosmos credential, or household ID.

## Create the connection in Anke Money

1. Open API Key management in the signed-in Anke Money app.
2. Create the workspace's API Key.
3. Copy the API Key when it is shown.

The API Key grants six fixed scopes through seven Anke Skill tools and remains valid until
the owner resets or revokes it. The service stores only a hash and a display
prefix; plaintext is returned only after creation or reset.

## Install in an Agent host

Import or copy the repository directory
`skills/anke-money-agent` using the host's normal Skill installation mechanism.
The host reads `agents/openai.yaml`, which declares the `anke-money` Streamable
HTTP MCP dependency.

Configure that MCP connection with:

- URL: the Skill's declared MCP URL;
- authorization: `Bearer <apiKey>` in the host's protected credential store.

Never paste the API Key into `SKILL.md`, `openai.yaml`, a prompt template, a
shell command, logs, or source control. If the host cannot protect a bearer
credential, do not connect it.

## Use and revoke

Invoke `$anke-money-agent` or make a matching money request. Reads use the
granted scopes. The Skill must show the exact proposed ledger or asset change and
obtain explicit confirmation immediately before a write. A bill document remains
in the Agent host: after one confirmed summary, the Skill may send only normalized
entries in unchanged chunks of at most 25 through `ledger_create_batch`.

The owner can reset or revoke the API Key in Anke Money at any time. Either
change is checked on the next MCP request. Resetting immediately invalidates the
previous Key.
