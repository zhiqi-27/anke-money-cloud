import hashlib
import json
import unittest
from datetime import UTC, datetime
from uuid import uuid4
from unittest.mock import patch

import httpx2
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from app.auth import AuthenticatedIdentity
from app.main import fastapi_app
from app.models import (
    Actor,
    ActorType,
    DeviceRegistration,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
)
from app.services import AgentAccessService, CloudService
from app.storage.in_memory import InMemoryHouseholdStorage


class RemoteMCPContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.access = AgentAccessService(self.storage)
        self.identity = AuthenticatedIdentity(uid="apple:mcp-owner")
        bootstrap = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(
                device_id=uuid4(),
                name="Synthetic MCP client",
                app_version="0.1.0",
            ),
        )
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        self.cloud.stage_migration(
            self.identity,
            MigrationUploadRequest(
                device_id=bootstrap.device_id,
                manifest=MigrationManifest(
                    session_id=session_id,
                    source_mode=MigrationSourceMode.local,
                    schema_version=1,
                    record_counts={},
                    content_digest=digest,
                ),
                items=[],
            ),
        )
        self.cloud.activate_migration(self.identity, session_id, digest)

    async def test_streamable_http_exposes_nine_tools_and_audits_idempotent_writes(self):
        connection = self.cloud.create_agent_api_key(self.identity, self.access)
        household_id = self.storage.household_for_uid(self.identity.uid)
        self.assertIsNotNone(household_id)
        self.storage.create_agent_entity(
            household_id,
            Actor(type=ActorType.agent, id=str(connection.connection_id)),
            "category",
            "asset-category:stocks",
            str(uuid4()),
            "test.seed",
            "test.seed",
            "skill",
            {"name": "Stocks", "scope": "asset", "assetGroup": "financial", "isArchived": False},
            {"before": None, "after": {"revision": 1}},
            datetime.now(UTC),
        )
        entry_id = str(uuid4())
        idempotency_key = str(uuid4())
        arguments = {
            "id": entry_id,
            "idempotency_key": idempotency_key,
            "kind": "transaction",
            "direction": "expense",
            "occurred_at": "2026-08-06T01:00:00Z",
            "month_start": "2026-08-01",
            "channel_id": "cash",
            "category_id": "grocery",
            "amount_in_fen": 8_800,
        }
        batch_entry_ids = [str(uuid4()), str(uuid4())]
        batch_arguments = {
            "entries": [
                {
                    "id": entry_id,
                    "idempotencyKey": str(uuid4()),
                    "kind": "transaction",
                    "direction": "income" if index else "expense",
                    "occurredAt": f"2026-08-0{7 + index}T01:00:00Z",
                    "monthStart": "2026-08-01",
                    "channelId": None if index else "cash",
                    "categoryId": "salary" if index else "grocery",
                    "amountInFen": 10_000 + index,
                }
                for index, entry_id in enumerate(batch_entry_ids)
            ]
        }
        expected_tools = [
            "ledger_read",
            "ledger_create",
            "ledger_create_batch",
            "assets_read",
            "assets_create",
            "assets_create_batch",
            "assets_update",
            "categories_read",
            "channels_read",
        ]

        with patch("app.dependencies.get_household_storage", return_value=self.storage):
            async with fastapi_app.router.lifespan_context(fastapi_app):
                transport = httpx2.ASGITransport(app=fastapi_app)
                async with httpx2.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as unauthorized_client:
                    unauthorized = await unauthorized_client.post(
                        "/mcp",
                        headers={
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
                                        "name": "unauthorized-test",
                                        "version": "1",
                                    },
                                },
                            },
                        },
                    )
                self.assertEqual(unauthorized.status_code, 401)
                self.assertIn("Bearer", unauthorized.headers["www-authenticate"])
                async with httpx2.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {connection.api_key}"},
                ) as http_client:
                    legacy_transport = streamable_http_client(
                        "http://testserver/mcp", http_client=http_client
                    )
                    async with legacy_transport as streams:
                        async with ClientSession(*streams[:2]) as legacy_session:
                            initialized = await legacy_session.initialize()
                            self.assertEqual(initialized.protocol_version, "2025-11-25")
                            legacy_tools = await legacy_session.list_tools()
                            self.assertEqual(
                                [tool.name for tool in legacy_tools.tools], expected_tools
                            )

                    client_transport = streamable_http_client(
                        "http://testserver/mcp", http_client=http_client
                    )
                    async with Client(
                        client_transport,
                        mode="2026-07-28",
                        client_info=Implementation(name="anke-test", version="1"),
                    ) as client:
                        tools = await client.list_tools()
                        self.assertEqual(
                            [tool.name for tool in tools.tools],
                            expected_tools,
                        )
                        first = await client.call_tool("ledger_create", arguments)
                        replay = await client.call_tool("ledger_create", arguments)
                        batch = await client.call_tool(
                            "ledger_create_batch", batch_arguments
                        )
                        batch_replay = await client.call_tool(
                            "ledger_create_batch", batch_arguments
                        )
                        first_page = await client.call_tool(
                            "ledger_read",
                            {
                                "limit": 2,
                                "start_date": "2026-08-01",
                                "end_date": "2026-08-31",
                            },
                        )
                        asset_arguments = {
                            "account_id": str(uuid4()),
                            "snapshot_id": str(uuid4()),
                            "idempotency_key": str(uuid4()),
                            "name": "Brokerage",
                            "kind": "asset",
                            "asset_group": "financial",
                            "category_id": "asset-category:stocks",
                            "money_bucket": "risk",
                            "amount_in_fen": 1250000,
                            "observed_at": "2026-08-21T00:00:00Z",
                        }
                        asset = await client.call_tool("assets_create", asset_arguments)
                        asset_replay = await client.call_tool("assets_create", asset_arguments)
                        asset_batch_arguments = {
                            "accounts": [{
                                "accountId": str(uuid4()),
                                "snapshotId": str(uuid4()),
                                "idempotencyKey": str(uuid4()),
                                "name": "Second Brokerage",
                                "kind": "asset",
                                "assetGroup": "financial",
                                "categoryId": "asset-category:stocks",
                                "moneyBucket": "risk",
                                "amountInFen": 2500000,
                                "observedAt": "2026-08-21T00:00:00Z",
                            }]
                        }
                        asset_batch = await client.call_tool(
                            "assets_create_batch", asset_batch_arguments
                        )
                        asset_batch_replay = await client.call_tool(
                            "assets_create_batch", asset_batch_arguments
                        )

        self.assertFalse(first.is_error)
        self.assertFalse(replay.is_error)
        self.assertFalse(batch.is_error)
        self.assertFalse(batch_replay.is_error)
        first_payload = json.loads(first.content[0].text)
        replay_payload = json.loads(replay.content[0].text)
        self.assertFalse(first_payload["replayed"])
        self.assertTrue(replay_payload["replayed"])
        batch_payload = json.loads(batch.content[0].text)
        batch_replay_payload = json.loads(batch_replay.content[0].text)
        first_page_payload = json.loads(first_page.content[0].text)
        self.assertEqual(batch_payload["createdCount"], 2)
        self.assertEqual(batch_payload["replayedCount"], 0)
        self.assertEqual(batch_replay_payload["replayedCount"], 2)
        self.assertTrue(first_page_payload["hasMore"])
        self.assertIsNotNone(first_page_payload["nextCursor"])
        self.assertFalse(asset.is_error)
        self.assertFalse(asset_replay.is_error)
        self.assertFalse(asset_batch.is_error)
        self.assertFalse(asset_batch_replay.is_error)
        self.assertFalse(json.loads(asset.content[0].text)["replayed"])
        self.assertTrue(json.loads(asset_replay.content[0].text)["replayed"])
        self.assertEqual(json.loads(asset_batch.content[0].text)["createdCount"], 1)
        self.assertEqual(json.loads(asset_batch_replay.content[0].text)["replayedCount"], 1)
        audit = self.cloud.audit(self.identity, None, 100)
        event = next(item for item in audit.events if item.target_id == entry_id)
        self.assertEqual(event.actor_id, str(connection.connection_id))
        self.assertEqual(event.scope, "ledger:create")
        self.assertEqual(event.source, "skill")
        self.assertEqual(event.idempotency_key, idempotency_key)
        self.assertEqual(event.change_summary["after"]["amountInFen"], 8_800)
        self.assertTrue(set(batch_entry_ids).issubset({item.target_id for item in audit.events}))
        pulled = self.cloud.pull(self.identity, None, 100)
        self.assertIn(entry_id, {item.entity_id for item in pulled.changes})


if __name__ == "__main__":
    unittest.main()
