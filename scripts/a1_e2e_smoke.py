from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential
from firebase_admin import auth
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.auth import FirebaseTokenVerifier  # noqa: E402
from app.config import ConfigurationError, Settings  # noqa: E402
from app.models import MigrationItem  # noqa: E402
from scripts.firebase_e2e_smoke import (  # noqa: E402
    exchange_custom_token,
    validate_remote_smoke_target,
    validate_synthetic_auth_target,
)


EXPECTED_HOST = (
    "func-anke-money-dev-zq01-a0btadd7fsfkc6cj.eastasia-01.azurewebsites.net"
)
EXPECTED_COSMOS_ACCOUNT = "cosmos-anke-money-dev-zq01"
UID_PREFIX = "smoke-a1-"


def _load_local_settings() -> None:
    path = REPOSITORY_ROOT / "local.settings.json"
    values = json.loads(path.read_text())["Values"]
    for name, value in values.items():
        if isinstance(value, str):
            os.environ.setdefault(name, value)


def _firebase_web_api_key() -> str:
    path = REPOSITORY_ROOT.parent / "anke-money-ios" / "AnkeMoney" / "Resources" / "GoogleService-Info.plist"
    with path.open("rb") as stream:
        value = plistlib.load(stream).get("API_KEY")
    if not isinstance(value, str) or not value:
        raise ConfigurationError("Firebase iOS API key is unavailable")
    return value


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str,
    payload: dict | None = None,
) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode())
            return response.status, body
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}") from None
    except urllib.error.URLError:
        raise RuntimeError(f"{method} {path} failed at the network boundary") from None


def _expect_http_error(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str,
    expected_status: int,
    payload: dict | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        error.read()
        if error.code != expected_status:
            raise RuntimeError(
                f"{method} {path} returned HTTP {error.code}, expected {expected_status}"
            ) from None
        return {name.lower(): value for name, value in error.headers.items()}
    except urllib.error.URLError:
        raise RuntimeError(f"{method} {path} failed at the network boundary") from None
    raise RuntimeError(f"{method} {path} unexpectedly succeeded")


def _migration_digest(items: list[MigrationItem]) -> str:
    canonical = [
        item.model_dump(by_alias=True, mode="json", exclude_none=True)
        for item in sorted(
            items, key=lambda value: (value.entity_type.value, value.entity_id)
        )
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _run_remote_mcp(
    base_url: str,
    access_token: str,
    channel_id: str,
    category_id: str,
    timestamp: str,
    month_start: str,
) -> tuple[str, str]:
    entry_id = str(uuid4())
    idempotency_key = str(uuid4())
    arguments = {
        "id": entry_id,
        "idempotency_key": idempotency_key,
        "kind": "transaction",
        "direction": "expense",
        "occurred_at": timestamp,
        "month_start": month_start,
        "channel_id": channel_id,
        "category_id": category_id,
        "amount_in_fen": 2_400,
        "note": "synthetic skill write",
    }
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    ) as http_client:
        transport = streamable_http_client(
            f"{base_url}/mcp",
            http_client=http_client,
        )
        async with Client(
            transport,
            mode="2026-07-28",
            client_info=Implementation(name="anke-e2e", version="1"),
        ) as client:
            tools = await client.list_tools()
            if [tool.name for tool in tools.tools] != [
                "ledger_read",
                "ledger_create",
                "assets_read",
                "assets_update",
                "categories_read",
                "channels_read",
            ]:
                raise RuntimeError("Remote MCP tool surface is not the frozen six")
            first = await client.call_tool("ledger_create", arguments)
            replay = await client.call_tool("ledger_create", arguments)
    if first.is_error or replay.is_error:
        raise RuntimeError("Remote MCP ledger write failed")
    first_payload = json.loads(first.content[0].text)
    replay_payload = json.loads(replay.content[0].text)
    if first_payload["replayed"] or not replay_payload["replayed"]:
        raise RuntimeError("Remote MCP idempotent replay failed")
    return entry_id, idempotency_key


async def _assert_mcp_revoked(base_url: str, access_token: str) -> None:
    async with httpx2.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{base_url}/mcp",
            headers={
                "Authorization": f"Bearer {access_token}",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "revocation-check",
                            "version": "1",
                        },
                    },
                },
            },
        )
    if response.status_code != 401:
        raise RuntimeError("Revoked Skill connection remained usable through MCP")


async def _run_rate_burst(base_url: str, access_token: str) -> None:
    limits = httpx2.Limits(max_connections=16, max_keepalive_connections=16)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}"},
        limits=limits,
        timeout=60,
    ) as client:
        responses = await asyncio.gather(*(
            client.get(f"{base_url}/agent/v1/categories")
            for _ in range(121)
        ))
    statuses = [response.status_code for response in responses]
    if statuses.count(200) != 120 or statuses.count(429) != 1:
        raise RuntimeError(
            f"Agent burst rate limit returned unexpected statuses: "
            f"200={statuses.count(200)}, 429={statuses.count(429)}, "
            f"other={len(statuses) - statuses.count(200) - statuses.count(429)}"
        )
    limited = next(response for response in responses if response.status_code == 429)
    if limited.headers.get("retry-after") != "60":
        raise RuntimeError("Agent rate limit omitted the 60-second retry boundary")


def _cleanup_cosmos(settings: Settings, uid: str, household_id: str) -> int:
    expected_household = str(uuid5(NAMESPACE_URL, f"anke-household:{uid}"))
    if not uid.startswith(UID_PREFIX) or household_id != expected_household:
        raise RuntimeError("Refusing to clean up a non-synthetic A1 workspace")
    credential = settings.cosmos_key or DefaultAzureCredential()
    client = CosmosClient(settings.cosmos_endpoint, credential=credential)
    database = client.get_database_client(settings.cosmos_database)
    entities = database.get_container_client(settings.cosmos_entities_container)
    identities = database.get_container_client(settings.cosmos_identities_container)
    documents = list(
        entities.query_items(
            query="SELECT c.id FROM c WHERE c.householdId = @householdId",
            parameters=[{"name": "@householdId", "value": household_id}],
            partition_key=household_id,
        )
    )
    for document in documents:
        entities.delete_item(item=document["id"], partition_key=household_id)
    identities.delete_item(item=uid, partition_key=uid)
    return len(documents) + 1


def main() -> int:
    _load_local_settings()
    settings = Settings.from_environment()
    base_url = validate_remote_smoke_target(
        f"https://{EXPECTED_HOST}", EXPECTED_HOST
    )
    allow_write = os.getenv("ANKE_A1_ALLOW_SYNTHETIC_WRITE", "").lower() == "true"
    web_api_key = _firebase_web_api_key()
    validate_synthetic_auth_target(
        settings,
        web_api_key=web_api_key,
        allow_synthetic_user=allow_write,
    )
    if settings.cosmos_expected_account_name != EXPECTED_COSMOS_ACCOUNT:
        raise ConfigurationError("A1 E2E Cosmos account does not match Development")
    settings.require_cosmos()

    verifier = FirebaseTokenVerifier(settings)
    firebase_app = verifier._firebase_app()
    run_id = uuid4()
    uid = f"{UID_PREFIX}{run_id}"
    household_id = str(uuid5(NAMESPACE_URL, f"anke-household:{uid}"))
    print(f"run_id={run_id}", flush=True)
    firebase_user_created = False
    workspace_created = False
    cleanup_items = 0
    cleanup_error: Exception | None = None
    primary_error: Exception | None = None
    try:
        custom_token = auth.create_custom_token(uid, app=firebase_app)
        id_token = exchange_custom_token(custom_token, web_api_key)
        firebase_user_created = True
        device_id = str(uuid4())
        status, bootstrap = _request(
            base_url,
            "POST",
            "/api/v1/bootstrap",
            token=id_token,
            payload={
                "deviceId": device_id,
                "name": "A1 synthetic offline iPhone",
                "platform": "ios",
                "appVersion": "a1-e2e",
            },
        )
        if status != 200 or bootstrap["householdId"] != household_id:
            raise RuntimeError("Bootstrap did not return the synthetic household")
        workspace_created = True

        now = datetime.now(UTC).replace(microsecond=0)
        timestamp = now.isoformat().replace("+00:00", "Z")
        channel_id = f"channel-a1-{run_id}"
        category_id = f"category-a1-{run_id}"
        migrated_ledger_id = str(uuid4())
        account_id = str(uuid4())
        snapshot_id = str(uuid4())
        raw_items = [
            {
                "entityType": "paymentChannel",
                "entityId": channel_id,
                "payload": {
                    "name": "A1 synthetic channel",
                    "symbolName": "banknote",
                    "assetName": None,
                    "sortOrder": 1,
                    "isArchived": False,
                    "isSystem": False,
                },
                "createdAt": timestamp,
            },
            {
                "entityType": "category",
                "entityId": category_id,
                "payload": {
                    "name": "A1 synthetic category",
                    "symbolName": "basket",
                    "sortOrder": 1,
                    "isArchived": False,
                    "isSystem": False,
                    "direction": "expense",
                },
                "createdAt": timestamp,
            },
            {
                "entityType": "ledgerEntry",
                "entityId": migrated_ledger_id,
                "payload": {
                    "kind": "transaction",
                    "direction": "expense",
                    "occurredAt": timestamp,
                    "monthStart": now.date().replace(day=1).isoformat(),
                    "channelId": channel_id,
                    "categoryId": category_id,
                    "amountInFen": 3_400,
                    "note": "synthetic migration",
                    "memberProfileId": None,
                },
                "createdAt": timestamp,
            },
            {
                "entityType": "assetAccount",
                "entityId": account_id,
                "payload": {"name": "A1 synthetic asset", "amountInFen": 100_000},
                "createdAt": timestamp,
            },
            {
                "entityType": "assetSnapshot",
                "entityId": snapshot_id,
                "payload": {
                    "accountId": account_id,
                    "memberProfileId": None,
                    "amountInFen": 100_000,
                    "observedAt": timestamp,
                },
                "createdAt": timestamp,
            },
        ]
        items = [MigrationItem.model_validate(item) for item in raw_items]
        digest = _migration_digest(items)
        session_id = str(uuid4())
        counts: dict[str, int] = {}
        for item in items:
            counts[item.entity_type.value] = counts.get(item.entity_type.value, 0) + 1
        migration_payload = {
            "deviceId": device_id,
            "manifest": {
                "sessionId": session_id,
                "sourceMode": "local",
                "schemaVersion": 1,
                "recordCounts": counts,
                "contentDigest": digest,
            },
            "items": [
                item.model_dump(by_alias=True, mode="json", exclude_none=True)
                for item in items
            ],
        }
        _, staged = _request(
            base_url,
            "POST",
            "/api/v1/migrations",
            token=id_token,
            payload=migration_payload,
        )
        _, staged_replay = _request(
            base_url,
            "POST",
            "/api/v1/migrations",
            token=id_token,
            payload=migration_payload,
        )
        activation_payload = {"sessionId": session_id, "contentDigest": digest}
        _, active = _request(
            base_url,
            "POST",
            "/api/v1/migrations/activate",
            token=id_token,
            payload=activation_payload,
        )
        _, active_replay = _request(
            base_url,
            "POST",
            "/api/v1/migrations/activate",
            token=id_token,
            payload=activation_payload,
        )
        if not staged_replay["replayed"] or active["status"] != "active" or not active_replay["replayed"]:
            raise RuntimeError("Migration idempotency or activation failed")
        if staged["verifiedCounts"] != counts:
            raise RuntimeError("Migration record counts were not preserved")

        _, connection = _request(
            base_url,
            "POST",
            "/api/v1/agent-connections",
            token=id_token,
            payload={
                "name": "A1 synthetic agent",
                "integration": "api",
                "scopes": [
                    "ledger:read",
                    "ledger:create",
                    "assets:read",
                    "assets:update",
                    "categories:read",
                    "channels:read",
                ],
                "grantDurationSeconds": 3_600,
            },
        )
        agent_token = connection["accessToken"]
        original_connection_contract = {
            "integration": connection["integration"],
            "scopes": connection["scopes"],
            "grantExpiresAt": connection["grantExpiresAt"],
        }
        _, paused = _request(
            base_url,
            "POST",
            f"/api/v1/agent-connections/{connection['connectionId']}/pause",
            token=id_token,
        )
        _expect_http_error(
            base_url,
            "GET",
            "/agent/v1/ledger/entries",
            token=agent_token,
            expected_status=401,
        )
        _, resumed = _request(
            base_url,
            "POST",
            f"/api/v1/agent-connections/{connection['connectionId']}/resume",
            token=id_token,
        )
        if paused["status"] != "paused" or resumed["status"] != "active":
            raise RuntimeError("Agent pause or resume lifecycle failed")
        if any(resumed[key] != value for key, value in original_connection_contract.items()):
            raise RuntimeError("Agent lifecycle changed an immutable grant field")

        remote_ledger_id = str(uuid4())
        remote_operation_id = str(uuid4())
        remote_payload = {
            "id": remote_ledger_id,
            "idempotencyKey": remote_operation_id,
            "kind": "transaction",
            "direction": "expense",
            "occurredAt": timestamp,
            "monthStart": now.date().replace(day=1).isoformat(),
            "channelId": channel_id,
            "categoryId": category_id,
            "amountInFen": 8_800,
            "note": "synthetic agent write",
        }
        _, remote_create = _request(
            base_url,
            "POST",
            "/agent/v1/ledger/entries",
            token=agent_token,
            payload=remote_payload,
        )
        _, remote_replay = _request(
            base_url,
            "POST",
            "/agent/v1/ledger/entries",
            token=agent_token,
            payload=remote_payload,
        )
        if remote_create["replayed"] or not remote_replay["replayed"]:
            raise RuntimeError("Agent idempotent replay failed")

        invalid_payloads = [
            {**remote_payload, "id": str(uuid4()), "idempotencyKey": str(uuid4()), "amountInFen": 9_000_000_000_000_001},
            {**remote_payload, "id": str(uuid4()), "idempotencyKey": str(uuid4()), "occurredAt": "not-a-date"},
            {**remote_payload, "id": str(uuid4()), "idempotencyKey": str(uuid4()), "categoryId": "x" * 129},
        ]
        for invalid_payload in invalid_payloads:
            _expect_http_error(
                base_url,
                "POST",
                "/agent/v1/ledger/entries",
                token=agent_token,
                expected_status=422,
                payload=invalid_payload,
            )

        token_parts = agent_token.split(".", maxsplit=2)
        invalid_known_token = f"{token_parts[0]}.{token_parts[1]}.invalid"
        for _ in range(5):
            _expect_http_error(
                base_url,
                "GET",
                "/agent/v1/ledger/entries",
                token=invalid_known_token,
                expected_status=401,
            )
        _request(base_url, "GET", "/agent/v1/ledger/entries", token=agent_token)

        agent_snapshot_id = str(uuid4())
        agent_snapshot_operation = str(uuid4())
        snapshot_payload = {
            "snapshotId": agent_snapshot_id,
            "idempotencyKey": agent_snapshot_operation,
            "amountInFen": 101_200,
            "observedAt": timestamp,
        }
        _request(
            base_url,
            "PATCH",
            f"/agent/v1/assets/{account_id}",
            token=agent_token,
            payload=snapshot_payload,
        )
        _, snapshot_replay = _request(
            base_url,
            "PATCH",
            f"/agent/v1/assets/{account_id}",
            token=agent_token,
            payload=snapshot_payload,
        )
        if not snapshot_replay["replayed"]:
            raise RuntimeError("Agent asset snapshot replay failed")

        _, skill_connection = _request(
            base_url,
            "POST",
            "/api/v1/agent-connections",
            token=id_token,
            payload={
                "name": "A2 synthetic Skill",
                "integration": "skill",
                "scopes": ["ledger:read", "ledger:create"],
                "grantDurationSeconds": 3_600,
            },
        )
        skill_token = skill_connection["accessToken"]
        skill_ledger_id, skill_idempotency_key = asyncio.run(
            _run_remote_mcp(
                base_url,
                skill_token,
                channel_id,
                category_id,
                timestamp,
                now.date().replace(day=1).isoformat(),
            )
        )

        _, rate_connection = _request(
            base_url,
            "POST",
            "/api/v1/agent-connections",
            token=id_token,
            payload={
                "name": "A4 synthetic rate client",
                "integration": "api",
                "scopes": ["categories:read"],
                "grantDurationSeconds": 3_600,
            },
        )
        rate_token = rate_connection["accessToken"]
        asyncio.run(_run_rate_burst(base_url, rate_token))

        offline_ledger_id = str(uuid4())
        offline_mutation_id = str(uuid4())
        offline_mutation = {
            "mutationId": offline_mutation_id,
            "deviceId": device_id,
            "sequence": bootstrap["nextOutboxSequence"],
            "entityType": "ledgerEntry",
            "entityId": offline_ledger_id,
            "action": "create",
            "baseRevision": None,
            "payload": {
                "kind": "transaction",
                "direction": "expense",
                "occurredAt": timestamp,
                "monthStart": now.date().replace(day=1).isoformat(),
                "channelId": channel_id,
                "categoryId": category_id,
                "amountInFen": 1_200,
                "note": "synthetic offline write",
                "memberProfileId": None,
            },
            "occurredAt": timestamp,
        }
        push_payload = {"deviceId": device_id, "mutations": [offline_mutation]}
        _, pushed = _request(
            base_url, "POST", "/api/v1/sync/push", token=id_token, payload=push_payload
        )
        _, push_replay = _request(
            base_url, "POST", "/api/v1/sync/push", token=id_token, payload=push_payload
        )
        if pushed["results"][0]["status"] != "accepted" or push_replay["results"][0]["entityId"] != offline_ledger_id:
            raise RuntimeError("Offline mutation replay failed")

        _, pulled = _request(
            base_url,
            "GET",
            "/api/v1/sync/pull?cursor=0&limit=100",
            token=id_token,
        )
        pulled_ids = {item["entityId"] for item in pulled["changes"]}
        required_ids = {
            migrated_ledger_id,
            remote_ledger_id,
            skill_ledger_id,
            offline_ledger_id,
            agent_snapshot_id,
        }
        if not required_ids.issubset(pulled_ids):
            raise RuntimeError("Reconnect pull did not merge every writer")

        update_payload = dict(raw_items[0]["payload"])
        update_payload["name"] = "A1 synthetic channel updated"
        sequence = bootstrap["nextOutboxSequence"] + 1
        update = {
            "mutationId": str(uuid4()),
            "deviceId": device_id,
            "sequence": sequence,
            "entityType": "paymentChannel",
            "entityId": channel_id,
            "action": "update",
            "baseRevision": 1,
            "payload": update_payload,
            "occurredAt": timestamp,
        }
        _, accepted_update = _request(
            base_url,
            "POST",
            "/api/v1/sync/push",
            token=id_token,
            payload={"deviceId": device_id, "mutations": [update]},
        )
        stale = dict(update)
        stale["mutationId"] = str(uuid4())
        stale["sequence"] = sequence + 1
        stale["payload"] = {**update_payload, "name": "stale overwrite"}
        _, conflict = _request(
            base_url,
            "POST",
            "/api/v1/sync/push",
            token=id_token,
            payload={"deviceId": device_id, "mutations": [stale]},
        )
        deletion = {
            "mutationId": str(uuid4()),
            "deviceId": device_id,
            "sequence": sequence + 2,
            "entityType": "paymentChannel",
            "entityId": channel_id,
            "action": "delete",
            "baseRevision": 2,
            "payload": None,
            "occurredAt": timestamp,
        }
        _, deleted = _request(
            base_url,
            "POST",
            "/api/v1/sync/push",
            token=id_token,
            payload={"deviceId": device_id, "mutations": [deletion]},
        )
        if accepted_update["results"][0]["revision"] != 2:
            raise RuntimeError("Accepted revision did not advance")
        if conflict["results"][0]["status"] != "conflict":
            raise RuntimeError("Stale revision did not produce a conflict")
        if deleted["results"][0]["serverEntity"]["deletedAt"] is None:
            raise RuntimeError("Deletion did not produce a tombstone")

        _request(base_url, "GET", "/agent/v1/ledger/entries", token=agent_token)
        _request(base_url, "GET", "/agent/v1/assets", token=agent_token)
        _request(base_url, "GET", "/agent/v1/categories", token=agent_token)
        _request(base_url, "GET", "/agent/v1/channels", token=agent_token)
        _, audit_page = _request(
            base_url, "GET", "/api/v1/audit?limit=200", token=id_token
        )
        audit_text = json.dumps(audit_page, ensure_ascii=False)
        if any(
            note in audit_text
            for note in (
                "synthetic offline write",
                "synthetic agent write",
                "synthetic skill write",
            )
        ):
            raise RuntimeError("Audit stream leaked private notes")
        if not any(
            event.get("source") == "api"
            and event.get("actorId") == connection["connectionId"]
            and event.get("scope") == "assets:update"
            and event.get("idempotencyKey") == agent_snapshot_operation
            and event.get("changeSummary", {}).get("after", {}).get("amountInFen") == 101_200
            for event in audit_page["events"]
        ):
            raise RuntimeError(
                "Agent write connection, scope, source, idempotency, or diff audit is missing"
            )
        if not any(
            event.get("source") == "skill"
            and event.get("actorId") == skill_connection["connectionId"]
            and event.get("scope") == "ledger:create"
            and event.get("idempotencyKey") == skill_idempotency_key
            and event.get("targetId") == skill_ledger_id
            and event.get("changeSummary", {}).get("after", {}).get("amountInFen") == 2_400
            for event in audit_page["events"]
        ):
            raise RuntimeError(
                "Skill MCP connection, scope, source, idempotency, or audit difference is missing"
            )
        if not any(event["actorType"] == "agent" for event in audit_page["events"]):
            raise RuntimeError("Agent write audit event is missing")
        required_security_actions = {
            "agent.pause",
            "agent.resume",
            "agent.authentication.anomaly",
            "agent.rate_limit",
        }
        observed_security_actions = {
            event["action"] for event in audit_page["events"]
        }
        if not required_security_actions.issubset(observed_security_actions):
            raise RuntimeError("Agent lifecycle, anomaly, or rate-limit audit is missing")
        if sum(
            event["action"] == "agent.authentication.anomaly"
            and event.get("actorId") == connection["connectionId"]
            for event in audit_page["events"]
        ) != 1:
            raise RuntimeError("Known-token anomaly audit was not deduplicated")
        if sum(
            event["action"] == "agent.rate_limit"
            and event.get("actorId") == rate_connection["connectionId"]
            for event in audit_page["events"]
        ) != 1:
            raise RuntimeError("Rate-limit audit was not deduplicated")
        _, listed_connections = _request(
            base_url, "GET", "/api/v1/agent-connections", token=id_token
        )
        listed_agent = next(
            item for item in listed_connections
            if item["connectionId"] == connection["connectionId"]
        )
        if not listed_agent.get("lastUsedAt"):
            raise RuntimeError("Agent connection did not expose last use")

        _request(
            base_url,
            "DELETE",
            f"/api/v1/agent-connections/{connection['connectionId']}",
            token=id_token,
        )
        try:
            _request(base_url, "GET", "/agent/v1/ledger/entries", token=agent_token)
        except RuntimeError as error:
            if "HTTP 401" not in str(error):
                raise
        else:
            raise RuntimeError("Revoked Agent token remained usable")
        _request(
            base_url,
            "DELETE",
            f"/api/v1/agent-connections/{skill_connection['connectionId']}",
            token=id_token,
        )
        asyncio.run(_assert_mcp_revoked(base_url, skill_token))
        _request(
            base_url,
            "DELETE",
            f"/api/v1/agent-connections/{rate_connection['connectionId']}",
            token=id_token,
        )
    except Exception as error:
        primary_error = error
        raise
    finally:
        try:
            if workspace_created:
                cleanup_items = _cleanup_cosmos(settings, uid, household_id)
        except Exception as error:  # Preserve cleanup failure after deleting auth user.
            cleanup_error = error
        finally:
            if firebase_user_created:
                if not uid.startswith(UID_PREFIX):
                    raise RuntimeError(
                        "Refusing to delete a non-synthetic Firebase user"
                    )
                auth.delete_user(uid, app=firebase_app)
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error
        if cleanup_error is not None and primary_error is not None:
            primary_error.add_note(f"A1 workspace cleanup also failed: {cleanup_error}")

    print("Development A1+A2+A3+A4 E2E passed")
    print(f"target_host={EXPECTED_HOST}")
    print("migration=staged_replayed_activated")
    print("merge=migrated_agent_offline")
    print("conflict=explicit")
    print("deletion=tombstone")
    print("audit=redacted")
    print("revocation=immediate")
    print("remote_mcp=six_tools_skill_source_idempotent")
    print("agent_center=pause_resume_last_used_audited")
    print("security=malicious_parameters_anomaly_rate_limit")
    print("interoperability=independent_http_and_mcp_clients")
    print(f"cleanup_items={cleanup_items}")
    print("synthetic_user_deleted=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
