from __future__ import annotations

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
                "scopes": [
                    "ledger.read",
                    "ledger.entry.create",
                    "assets.read",
                    "assets.snapshot.create",
                    "reference-data.read",
                ],
                "grantDurationSeconds": 3_600,
            },
        )
        agent_token = connection["accessToken"]
        remote_ledger_id = str(uuid4())
        remote_operation_id = str(uuid4())
        remote_payload = {
            "id": remote_ledger_id,
            "operationId": remote_operation_id,
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

        agent_snapshot_id = str(uuid4())
        agent_snapshot_operation = str(uuid4())
        snapshot_payload = {
            "id": agent_snapshot_id,
            "operationId": agent_snapshot_operation,
            "accountId": account_id,
            "amountInFen": 101_200,
            "observedAt": timestamp,
        }
        _request(
            base_url,
            "POST",
            "/agent/v1/assets/snapshots",
            token=agent_token,
            payload=snapshot_payload,
        )
        _, snapshot_replay = _request(
            base_url,
            "POST",
            "/agent/v1/assets/snapshots",
            token=agent_token,
            payload=snapshot_payload,
        )
        if not snapshot_replay["replayed"]:
            raise RuntimeError("Agent asset snapshot replay failed")

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
        _request(base_url, "GET", "/agent/v1/reference-data", token=agent_token)
        _, audit_page = _request(
            base_url, "GET", "/api/v1/audit?limit=200", token=id_token
        )
        audit_text = json.dumps(audit_page, ensure_ascii=False)
        if "amountInFen" in audit_text or "synthetic offline write" in audit_text:
            raise RuntimeError("Audit stream leaked financial payload")
        if not any(event["actorType"] == "agent" for event in audit_page["events"]):
            raise RuntimeError("Agent write audit event is missing")

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

    print("Development A1 E2E passed")
    print(f"target_host={EXPECTED_HOST}")
    print("migration=staged_replayed_activated")
    print("merge=migrated_agent_offline")
    print("conflict=explicit")
    print("deletion=tombstone")
    print("audit=redacted")
    print("revocation=immediate")
    print(f"cleanup_items={cleanup_items}")
    print("synthetic_user_deleted=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
