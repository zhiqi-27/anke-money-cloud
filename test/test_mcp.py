import hashlib
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

import httpx2
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from app.auth import AuthenticatedIdentity
from app.main import fastapi_app
from app.models import (
    AgentConnectionCreate,
    AgentScope,
    DeviceRegistration,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
    OperationSource,
)
from app.services import AgentAccessService, CloudService
from app.storage.in_memory import InMemoryHouseholdStorage


class RemoteMCPContractTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.access = AgentAccessService(self.storage)
        self.identity = AuthenticatedIdentity(uid="firebase-mcp-owner")
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

    async def test_streamable_http_exposes_only_six_tools_and_audits_idempotent_write(self):
        api_connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Versioned HTTP API client",
                scopes=[AgentScope.ledger_create],
                integration=OperationSource.api,
            ),
            self.access,
        )
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Skill test",
                scopes=list(AgentScope),
                integration=OperationSource.skill,
            ),
            self.access,
        )
        entry_id = str(uuid4())
        idempotency_key = str(uuid4())
        api_entry_id = str(uuid4())
        api_idempotency_key = str(uuid4())
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
        expected_tools = [
            "ledger_read",
            "ledger_create",
            "assets_read",
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
                    headers={"Authorization": f"Bearer {api_connection.access_token}"},
                ) as api_client:
                    api_write = await api_client.post(
                        "/agent/v1/ledger/entries",
                        json={
                            "id": api_entry_id,
                            "idempotencyKey": api_idempotency_key,
                            "kind": "transaction",
                            "direction": "expense",
                            "occurredAt": "2026-08-06T00:30:00Z",
                            "monthStart": "2026-08-01",
                            "channelId": "cash",
                            "categoryId": "grocery",
                            "amountInFen": 1_200,
                        },
                    )
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
                    headers={"Authorization": f"Bearer {api_connection.access_token}"},
                ) as wrong_integration_client:
                    wrong_integration = await wrong_integration_client.post(
                        "/mcp",
                        headers={
                            "MCP-Protocol-Version": "2026-07-28",
                            "Mcp-Method": "tools/list",
                            "Accept": "application/json, text/event-stream",
                            "Content-Type": "application/json",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/list",
                            "params": {
                                "_meta": {
                                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                    "io.modelcontextprotocol/clientCapabilities": {},
                                    "io.modelcontextprotocol/clientInfo": {
                                        "name": "api-token",
                                        "version": "1",
                                    },
                                },
                            },
                        },
                    )
                self.assertEqual(wrong_integration.status_code, 401)
                async with httpx2.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {connection.access_token}"},
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

        self.assertFalse(first.is_error)
        self.assertFalse(replay.is_error)
        self.assertEqual(api_write.status_code, 200)
        first_payload = json.loads(first.content[0].text)
        replay_payload = json.loads(replay.content[0].text)
        self.assertFalse(first_payload["replayed"])
        self.assertTrue(replay_payload["replayed"])
        audit = self.cloud.audit(self.identity, None, 100)
        event = next(item for item in audit.events if item.target_id == entry_id)
        self.assertEqual(event.actor_id, str(connection.connection_id))
        self.assertEqual(event.scope, "ledger:create")
        self.assertEqual(event.source, "skill")
        self.assertEqual(event.idempotency_key, idempotency_key)
        self.assertEqual(event.change_summary["after"]["amountInFen"], 8_800)
        api_event = next(item for item in audit.events if item.target_id == api_entry_id)
        self.assertEqual(api_event.source, "api")
        self.assertEqual(api_event.actor_id, str(api_connection.connection_id))
        self.assertEqual(api_event.scope, "ledger:create")
        pulled = self.cloud.pull(self.identity, None, 100)
        self.assertTrue({api_entry_id, entry_id}.issubset(
            {item.entity_id for item in pulled.changes}
        ))


if __name__ == "__main__":
    unittest.main()
