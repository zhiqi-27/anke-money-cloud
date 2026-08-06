from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from collections import Counter
import hashlib
import json
import time
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential

from app.config import ConfigurationError, Settings
from app.models import (
    Actor,
    ActorType,
    AgentConnectionCreate,
    AgentConnectionView,
    AgentPrincipal,
    AgentScope,
    OperationSource,
    AuditEventView,
    BootstrapResponse,
    DeviceRegistration,
    LedgerEntryCreate,
    LedgerEntryDocument,
    MigrationResponse,
    MigrationStatus,
    MigrationUploadRequest,
    MutationAction,
    MutationResult,
    MutationStatus,
    SyncChange,
    SyncEntityType,
    SyncMutation,
    build_ledger_transaction_documents,
    canonical_write_hash,
)
from app.storage.protocols import LedgerCreateResult, RetentionResult


logger = logging.getLogger(__name__)
HOUSEHOLD_PARTITION_PATH = "/householdId"


class CosmosHouseholdStorage:
    """Cosmos adapter for the household-partitioned primary entity container."""

    def __init__(
        self,
        settings: Settings,
        *,
        container: Any | None = None,
        identities_container: Any | None = None,
    ):
        self._settings = settings
        self._container = container
        self._identities = identities_container

    def bootstrap_owner(
        self,
        uid: str,
        registration: DeviceRegistration,
    ) -> BootstrapResponse:
        identity = self._read_identity(uid)
        household_id = (
            identity["householdId"]
            if identity is not None
            else str(uuid5(NAMESPACE_URL, f"anke-household:{uid}"))
        )
        user_id = str(uuid5(NAMESPACE_URL, f"anke-user:{uid}"))
        device_id = str(registration.device_id)
        connection_id = str(uuid5(NAMESPACE_URL, f"anke-connection:{uid}:{device_id}"))
        now = self._now()
        if identity is None:
            base = {"householdId": household_id, "schemaVersion": 1, "revision": 1, "createdAt": now, "updatedAt": now}
            user = {"id": user_id, "entityType": "user", "firebaseUid": uid, "role": "owner", **base}
            household = {"id": household_id, "entityType": "household", "status": "empty", "storageMode": "agentCloud", "lastChangeSequence": 0, **base}
            try:
                self._entities_container().execute_item_batch(
                    batch_operations=[("create", (user,), {}), ("create", (household,), {})],
                    partition_key=household_id,
                )
            except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
                if getattr(exc, "status_code", 409) != 409:
                    raise
            identity_document = {
                "id": uid,
                "uid": uid,
                "entityType": "identityMembership",
                "householdId": household_id,
                "userId": user_id,
                "role": "owner",
                "createdAt": now,
            }
            try:
                self._identities_container().create_item(body=identity_document)
            except CosmosResourceExistsError:
                identity = self._read_identity(uid)
                if identity is None or identity.get("householdId") != household_id:
                    raise RuntimeError("Identity membership race resolved to another household")

        existing_device = self.read_household_document(household_id, device_id)
        device = {
            "id": device_id,
            "entityType": "device",
            "householdId": household_id,
            "name": registration.name,
            "platform": registration.platform,
            "appVersion": registration.app_version,
            "ownerUserId": user_id,
            "lastOutboxSequence": int((existing_device or {}).get("lastOutboxSequence", 0)),
            "schemaVersion": 1,
            "revision": int((existing_device or {}).get("revision", 0)) + 1,
            "createdAt": (existing_device or {}).get("createdAt", now),
            "updatedAt": now,
        }
        connection = {
            "id": connection_id,
            "entityType": "connection",
            "householdId": household_id,
            "userId": user_id,
            "deviceId": device_id,
            "status": "active",
            "schemaVersion": 1,
            "revision": 1,
            "createdAt": now,
            "updatedAt": now,
        }
        self._entities_container().upsert_item(body=device)
        self._entities_container().upsert_item(body=connection)
        household = self.read_household_document(household_id, household_id)
        if household is None:
            raise RuntimeError("Household document is missing after bootstrap")
        return BootstrapResponse(
            user_id=user_id,
            household_id=UUID(household_id),
            device_id=registration.device_id,
            connection_id=UUID(connection_id),
            sync_cursor="0",
            next_outbox_sequence=int(device["lastOutboxSequence"]) + 1,
            workspace_status=household["status"],
        )

    def household_for_uid(self, uid: str) -> str | None:
        identity = self._read_identity(uid)
        return identity.get("householdId") if identity else None

    def run_retention(self, now: datetime) -> RetentionResult:
        tombstone_cutoff = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        audit_cutoff = (now - timedelta(days=365)).isoformat().replace("+00:00", "Z")
        container = self._entities_container()
        tombstones = list(container.query_items(
            query=(
                "SELECT * FROM c WHERE ARRAY_CONTAINS(@types, c.entityType) "
                "AND IS_DEFINED(c.deletedAt) AND NOT IS_NULL(c.deletedAt) "
                "AND c.deletedAt < @cutoff "
                "AND (NOT IS_DEFINED(c.payloadPurgedAt) OR IS_NULL(c.payloadPurgedAt))"
            ),
            parameters=[
                {"name": "@types", "value": [value.value for value in SyncEntityType]},
                {"name": "@cutoff", "value": tombstone_cutoff},
            ],
            enable_cross_partition_query=True,
        ))
        purged_at = now.isoformat().replace("+00:00", "Z")
        for document in tombstones:
            updated = dict(document)
            updated["payload"] = None
            updated["payloadPurgedAt"] = purged_at
            container.replace_item(item=document["id"], body=updated)

        audits = list(container.query_items(
            query=(
                "SELECT c.id, c.householdId FROM c "
                "WHERE c.entityType = 'auditEvent' AND c.createdAt < @cutoff"
            ),
            parameters=[{"name": "@cutoff", "value": audit_cutoff}],
            enable_cross_partition_query=True,
        ))
        for document in audits:
            container.delete_item(
                item=document["id"],
                partition_key=document["householdId"],
            )
        return RetentionResult(len(tombstones), len(audits))

    def create_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        request: AgentConnectionCreate,
        connection_id: str,
        token_hash: str,
        refresh_token_hash: str,
        token_expires_at: datetime,
        now: datetime,
    ) -> AgentConnectionView:
        now_text = now.isoformat().replace("+00:00", "Z")
        document = {
            "id": connection_id,
            "entityType": "agentConnection",
            "householdId": household_id,
            "name": request.name,
            "scopes": [scope.value for scope in request.scopes],
            "integration": request.integration.value,
            "status": "active",
            "tokenHash": token_hash,
            "refreshTokenHash": refresh_token_hash,
            "tokenExpiresAt": token_expires_at.isoformat().replace("+00:00", "Z"),
            "grantExpiresAt": (now + timedelta(seconds=request.grant_duration_seconds or 0)).isoformat().replace("+00:00", "Z"),
            "createdAt": now_text,
            "updatedAt": now_text,
        }
        audit = self._authorization_audit(household_id, actor, document, "agent.grant")
        self._entities_container().execute_item_batch(
            batch_operations=[("create", (document,), {}), ("create", (audit,), {})],
            partition_key=household_id,
        )
        return self._connection_view(document)

    def list_agent_connections(self, household_id: str) -> list[AgentConnectionView]:
        documents = list(self._entities_container().query_items(
            query=(
                "SELECT * FROM c WHERE c.householdId = @householdId "
                "AND c.entityType = 'agentConnection' ORDER BY c.createdAt DESC"
            ),
            parameters=[{"name": "@householdId", "value": household_id}],
            partition_key=household_id,
        ))
        return [self._connection_view(item) for item in documents]

    def revoke_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
    ) -> AgentConnectionView:
        document = self.read_household_document(household_id, connection_id)
        if document is None or document.get("entityType") != "agentConnection":
            raise ValueError("Agent connection not found")
        updated = dict(document)
        updated["status"] = "revoked"
        updated["updatedAt"] = self._now()
        audit = self._authorization_audit(household_id, actor, updated, "agent.revoke")
        self._entities_container().execute_item_batch(
            batch_operations=[
                ("replace", (connection_id, updated), self._etag_kwargs(document)),
                ("create", (audit,), {}),
            ],
            partition_key=household_id,
        )
        return self._connection_view(updated)

    def pause_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        now: datetime,
    ) -> AgentConnectionView:
        document = self._required_agent_connection(household_id, connection_id)
        if document["status"] == "revoked":
            raise ValueError("Revoked agent connection cannot be paused")
        if document["status"] == "paused":
            return self._connection_view(document)
        updated = dict(document)
        updated["status"] = "paused"
        updated["updatedAt"] = self._timestamp(now)
        audit = self._authorization_audit(household_id, actor, updated, "agent.pause")
        self._entities_container().execute_item_batch(
            batch_operations=[
                ("replace", (connection_id, updated), self._etag_kwargs(document)),
                ("create", (audit,), {}),
            ],
            partition_key=household_id,
        )
        return self._connection_view(updated)

    def resume_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        now: datetime,
    ) -> AgentConnectionView:
        document = self._required_agent_connection(household_id, connection_id)
        if document["status"] == "revoked":
            raise ValueError("Revoked agent connection cannot be resumed")
        if datetime.fromisoformat(
            document["grantExpiresAt"].replace("Z", "+00:00")
        ) <= now:
            raise ValueError("Expired agent connection cannot be resumed")
        if document["status"] == "active":
            return self._connection_view(document)
        updated = dict(document)
        updated["status"] = "active"
        updated["updatedAt"] = self._timestamp(now)
        audit = self._authorization_audit(household_id, actor, updated, "agent.resume")
        self._entities_container().execute_item_batch(
            batch_operations=[
                ("replace", (connection_id, updated), self._etag_kwargs(document)),
                ("create", (audit,), {}),
            ],
            partition_key=household_id,
        )
        return self._connection_view(updated)

    def consume_agent_request(
        self,
        household_id: str,
        connection_id: str,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> bool:
        for attempt in range(32):
            document = self._required_agent_connection(household_id, connection_id)
            window_start = self._window_start(
                document.get("requestWindowStartedAt"), now, window_seconds
            )
            window_text = self._timestamp(window_start)
            count = (
                int(document.get("requestWindowCount", 0))
                if document.get("requestWindowStartedAt") == window_text
                else 0
            ) + 1
            updated = dict(document)
            updated["requestWindowStartedAt"] = window_text
            updated["requestWindowCount"] = count
            updated["updatedAt"] = self._timestamp(now)
            audit = None
            if count > limit:
                if document.get("rateLimitAuditWindow") != window_text:
                    updated["rateLimitAuditWindow"] = window_text
                    audit = self._security_audit(
                        household_id,
                        updated,
                        action="agent.rate_limit",
                        reason="requestRateExceeded",
                        marker=window_text,
                        count=count,
                    )
            else:
                updated["lastUsedAt"] = self._timestamp(now)
            operations: list[tuple] = [
                ("replace", (connection_id, updated), self._etag_kwargs(document))
            ]
            if audit is not None:
                operations.append(("create", (audit,), {}))
            try:
                self._entities_container().execute_item_batch(
                    batch_operations=operations,
                    partition_key=household_id,
                )
            except CosmosBatchOperationError as exc:
                if not self._is_precondition_failure(exc) or attempt == 31:
                    raise
                time.sleep(min(0.002 * (attempt + 1), 0.05))
                continue
            return count <= limit
        raise RuntimeError("Agent request counter retry exhausted")

    def authenticate_agent_token(
        self,
        household_id: str,
        connection_id: str,
        token_hash: str,
        now: datetime,
    ) -> AgentPrincipal | None:
        document = self.read_household_document(household_id, connection_id)
        if document is None or document.get("entityType") != "agentConnection":
            return None
        if document.get("status") != "active" or document.get("tokenHash") != token_hash:
            return None
        if datetime.fromisoformat(document["tokenExpiresAt"].replace("Z", "+00:00")) <= now:
            return None
        if datetime.fromisoformat(document["grantExpiresAt"].replace("Z", "+00:00")) <= now:
            return None
        return AgentPrincipal(
            household_id=UUID(household_id),
            connection_id=UUID(connection_id),
            scopes=[AgentScope(value) for value in document["scopes"]],
            integration=OperationSource(document.get("integration", "api")),
        )

    def refresh_agent_token(
        self,
        household_id: str,
        connection_id: str,
        refresh_token_hash: str,
        new_token_hash: str,
        requested_expires_at: datetime,
        now: datetime,
    ) -> tuple[AgentPrincipal, datetime] | None:
        document = self.read_household_document(household_id, connection_id)
        if document is None or document.get("entityType") != "agentConnection":
            return None
        grant_expires_at = datetime.fromisoformat(
            document["grantExpiresAt"].replace("Z", "+00:00")
        )
        if (
            document.get("status") != "active"
            or document.get("refreshTokenHash") != refresh_token_hash
            or grant_expires_at <= now
        ):
            return None
        token_expires_at = min(requested_expires_at, grant_expires_at)
        updated = dict(document)
        updated["tokenHash"] = new_token_hash
        updated["tokenExpiresAt"] = token_expires_at.isoformat().replace("+00:00", "Z")
        updated["updatedAt"] = now.isoformat().replace("+00:00", "Z")
        actor = Actor(type=ActorType.agent, id=connection_id)
        audit = self._authorization_audit(
            household_id, actor, updated, "agent.token.refresh"
        )
        self._entities_container().execute_item_batch(
            batch_operations=[
                ("replace", (connection_id, updated), self._etag_kwargs(document)),
                ("create", (audit,), {}),
            ],
            partition_key=household_id,
        )
        return (
            AgentPrincipal(
                household_id=UUID(household_id),
                connection_id=UUID(connection_id),
                scopes=[AgentScope(value) for value in updated["scopes"]],
                integration=OperationSource(updated.get("integration", "api")),
            ),
            token_expires_at,
        )

    def record_agent_auth_failure(
        self,
        household_id: str,
        connection_id: str,
        reason: str,
        now: datetime,
        threshold: int,
        window_seconds: int,
    ) -> None:
        timestamp = self._timestamp(now)
        operation_id = f"agent.auth:{connection_id}:{timestamp}"
        for attempt in range(32):
            document = self.read_household_document(household_id, connection_id)
            if document is None or document.get("entityType") != "agentConnection":
                return
            window_start = self._window_start(
                document.get("failedAuthWindowStartedAt"), now, window_seconds
            )
            window_text = self._timestamp(window_start)
            count = (
                int(document.get("failedAuthWindowCount", 0))
                if document.get("failedAuthWindowStartedAt") == window_text
                else 0
            ) + 1
            updated = dict(document)
            updated["failedAuthWindowStartedAt"] = window_text
            updated["failedAuthWindowCount"] = count
            updated["updatedAt"] = timestamp
            audit = {
                "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
                "entityType": "auditEvent",
                "householdId": household_id,
                "actor": {"type": "agent", "id": connection_id},
                "scope": "authentication",
                "action": "agent.authenticate",
                "source": document.get("integration", "api"),
                "targetId": connection_id,
                "operationId": operation_id,
                "outcome": "rejected",
                "reason": reason,
                "changeSummary": {},
                "createdAt": timestamp,
            }
            operations: list[tuple] = [
                ("replace", (connection_id, updated), self._etag_kwargs(document)),
                ("create", (audit,), {}),
            ]
            if (
                count >= threshold
                and document.get("failedAuthAnomalyWindow") != window_text
            ):
                updated["failedAuthAnomalyWindow"] = window_text
                operations[0] = (
                    "replace", (connection_id, updated), self._etag_kwargs(document)
                )
                anomaly = self._security_audit(
                    household_id,
                    updated,
                    action="agent.authentication.anomaly",
                    reason="repeatedInvalidToken",
                    marker=window_text,
                    count=count,
                )
                operations.append(("create", (anomaly,), {}))
            try:
                self._entities_container().execute_item_batch(
                    batch_operations=operations,
                    partition_key=household_id,
                )
            except CosmosBatchOperationError as exc:
                if not self._is_precondition_failure(exc) or attempt == 31:
                    raise
                time.sleep(min(0.002 * (attempt + 1), 0.05))
                continue
            return
        raise RuntimeError("Agent authentication counter retry exhausted")

    def list_agent_entities(
        self,
        household_id: str,
        entity_types: set[str],
        limit: int,
    ) -> list[dict]:
        household = self.read_household_document(household_id, household_id) or {}
        include_staged = household.get("status") == "active"
        return list(self._entities_container().query_items(
            query=(
                "SELECT * FROM c WHERE c.householdId = @householdId "
                "AND ARRAY_CONTAINS(@types, c.entityType) "
                "AND (NOT IS_DEFINED(c.deletedAt) OR IS_NULL(c.deletedAt)) "
                "AND (@includeStaged = true OR NOT IS_DEFINED(c.staged) OR c.staged = false) "
                "ORDER BY c.updatedAt DESC OFFSET 0 LIMIT @limit"
            ),
            parameters=[
                {"name": "@householdId", "value": household_id},
                {"name": "@types", "value": sorted(entity_types)},
                {"name": "@includeStaged", "value": include_staged},
                {"name": "@limit", "value": limit},
            ],
            partition_key=household_id,
        ))

    def create_agent_entity(
        self,
        household_id: str,
        actor: Actor,
        entity_type: str,
        entity_id: str,
        idempotency_key: str,
        scope: str,
        action: str,
        source: str,
        payload: dict,
        change_summary: dict,
        now: datetime,
    ) -> tuple[dict, bool]:
        request_hash = canonical_write_hash(
            actor=actor,
            scope=scope,
            action=action,
            source=source,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        existing_operation = self._read_operation(household_id, idempotency_key)
        if existing_operation is not None:
            if existing_operation.get("requestHash") != request_hash:
                raise ValueError(
                    "Idempotency key was already used for a different write"
                )
            result = self.read_household_document(
                household_id, existing_operation["resultEntityId"]
            )
            if result is None:
                raise RuntimeError("Stored operation result is missing")
            return result, True
        if self.read_household_document(household_id, entity_id) is not None:
            raise ValueError("Entity already exists")
        change_sequence, household_operation = self._next_change_sequence(household_id)
        timestamp = now.isoformat().replace("+00:00", "Z")
        document = {
            "id": entity_id,
            "entityType": entity_type,
            "householdId": household_id,
            "schemaVersion": 1,
            "revision": 1,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "deletedAt": None,
            "actor": actor.model_dump(mode="json"),
            "operationId": idempotency_key,
            "lastAcceptedMutationId": idempotency_key,
            "changeSequence": change_sequence,
            "payload": payload,
        }
        operation = {
            "id": f"operation:{idempotency_key}",
            "entityType": "operation",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": scope,
            "action": action,
            "source": source,
            "idempotencyKey": idempotency_key,
            "requestHash": request_hash,
            "status": "accepted",
            "resultEntityId": entity_id,
            "changeSummary": change_summary,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        audit = {
            "id": f"audit:{idempotency_key}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": scope,
            "action": action,
            "source": source,
            "targetId": entity_id,
            "operationId": idempotency_key,
            "idempotencyKey": idempotency_key,
            "outcome": "accepted",
            "reason": None,
            "priorRevision": change_summary.get("before", {}).get("revision") if change_summary.get("before") else None,
            "newRevision": change_summary.get("after", {}).get("revision") if change_summary.get("after") else None,
            "changeSummary": change_summary,
            "createdAt": timestamp,
        }
        try:
            self._entities_container().execute_item_batch(
                batch_operations=[
                    ("create", (document,), {}),
                    ("create", (operation,), {}),
                    ("create", (audit,), {}),
                    household_operation,
                ],
                partition_key=household_id,
            )
        except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
            if getattr(exc, "status_code", 409) != 409:
                raise
            replay = self._read_operation(household_id, idempotency_key)
            if replay is None:
                raise ValueError("Entity already exists") from exc
            if replay.get("requestHash") != request_hash:
                raise ValueError(
                    "Idempotency key was already used for a different write"
                ) from exc
            result = self.read_household_document(
                household_id, replay["resultEntityId"]
            )
            if result is None:
                raise RuntimeError("Stored operation result is missing")
            return result, True
        return document, False

    def record_agent_read(
        self,
        household_id: str,
        actor: Actor,
        scope: str,
        action: str,
        result_count: int,
        now: datetime,
    ) -> None:
        timestamp = now.isoformat().replace("+00:00", "Z")
        operation_id = f"{action}:{actor.id}:{timestamp}"
        audit = {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": scope,
            "action": action,
            "targetId": household_id,
            "operationId": operation_id,
            "outcome": "accepted",
            "reason": None,
            "changeSummary": {"resultCount": result_count},
            "createdAt": timestamp,
        }
        self._entities_container().create_item(body=audit)

    def record_agent_denial(
        self,
        household_id: str,
        actor: Actor,
        required_scope: str,
        action: str,
        now: datetime,
    ) -> None:
        timestamp = now.isoformat().replace("+00:00", "Z")
        operation_id = f"{action}:denied:{actor.id}:{timestamp}"
        audit = {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": required_scope,
            "action": action,
            "targetId": household_id,
            "operationId": operation_id,
            "outcome": "rejected",
            "reason": "insufficientScope",
            "changeSummary": {},
            "createdAt": timestamp,
        }
        self._entities_container().create_item(body=audit)

    def push_mutation(
        self,
        household_id: str,
        actor: Actor,
        mutation: SyncMutation,
    ) -> MutationResult:
        mutation_id = str(mutation.mutation_id)
        operation = self._read_operation(household_id, mutation_id)
        if operation is not None and "result" in operation:
            return MutationResult.model_validate(operation["result"])
        device_id = str(mutation.device_id)
        device = self.read_household_document(household_id, device_id)
        if device is None or device.get("entityType") != "device":
            result = MutationResult(
                mutation_id=mutation.mutation_id,
                entity_id=mutation.entity_id,
                status=MutationStatus.rejected,
                reason="deviceNotRegistered",
            )
            return self._record_rejected_without_device(
                household_id, actor, mutation, result
            )
        last_sequence = int(device.get("lastOutboxSequence", 0))
        if mutation.sequence != last_sequence + 1:
            result = MutationResult(
                mutation_id=mutation.mutation_id,
                entity_id=mutation.entity_id,
                status=MutationStatus.rejected,
                reason="outboxSequenceGap",
            )
            return self._record_rejected_without_device(
                household_id, actor, mutation, result
            )

        entity_id = str(mutation.entity_id)
        current = self.read_household_document(household_id, entity_id)
        if mutation.action is MutationAction.create:
            if current is not None:
                reason = "entityAlreadyExists"
                return self._record_nonaccepted(household_id, actor, mutation, device, current, reason)
            revision = 1
        else:
            if current is None:
                return self._record_nonaccepted(household_id, actor, mutation, device, None, "entityNotFound")
            if current.get("deletedAt") is not None:
                return self._record_nonaccepted(household_id, actor, mutation, device, current, "entityDeleted")
            if int(current.get("revision", 0)) != mutation.base_revision:
                return self._record_nonaccepted(household_id, actor, mutation, device, current, "staleRevision")
            revision = int(current["revision"]) + 1

        change_sequence, household_operation = self._next_change_sequence(household_id)
        now = self._now()
        document = {
            "id": entity_id,
            "entityType": mutation.entity_type.value,
            "householdId": household_id,
            "schemaVersion": 1,
            "revision": revision,
            "createdAt": (current or {}).get("createdAt", now),
            "updatedAt": now,
            "deletedAt": now if mutation.action is MutationAction.delete else None,
            "deletion": {"actor": actor.model_dump(mode="json"), "reason": "userRequested"} if mutation.action is MutationAction.delete else None,
            "actor": actor.model_dump(mode="json"),
            "operationId": mutation_id,
            "lastAcceptedMutationId": mutation_id,
            "changeSequence": change_sequence,
            "payload": dict(mutation.payload or (current or {}).get("payload") or {}),
        }
        result = MutationResult(
            mutation_id=mutation.mutation_id,
            entity_id=mutation.entity_id,
            status=MutationStatus.accepted,
            revision=revision,
            server_entity=document,
        )
        batch = self._mutation_batch(household_id, actor, mutation, result, device)
        entity_operation = (
            ("create", (document,), {})
            if current is None
            else ("replace", (entity_id, document), self._etag_kwargs(current))
        )
        batch.insert(0, entity_operation)
        batch.append(household_operation)
        try:
            self._entities_container().execute_item_batch(batch_operations=batch, partition_key=household_id)
        except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
            if getattr(exc, "status_code", 409) != 409:
                raise
            replay = self._read_operation(household_id, mutation_id)
            if replay and "result" in replay:
                return MutationResult.model_validate(replay["result"])
            latest = self.read_household_document(household_id, entity_id)
            return self._record_nonaccepted(household_id, actor, mutation, device, latest, "concurrentWrite")
        return result

    def pull_changes(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[SyncChange], str | None, bool]:
        try:
            after_sequence = int(cursor or "0")
        except ValueError as exc:
            raise ValueError("Invalid sync cursor") from exc
        if after_sequence < 0:
            raise ValueError("Invalid sync cursor")
        household = self.read_household_document(household_id, household_id) or {}
        include_staged = household.get("status") == "active"
        query = (
            "SELECT * FROM c WHERE c.householdId = @householdId "
            "AND ARRAY_CONTAINS(@types, c.entityType) "
            "AND c.changeSequence > @afterSequence "
            "AND (@includeStaged = true OR NOT IS_DEFINED(c.staged) OR c.staged = false) "
            "ORDER BY c.changeSequence OFFSET 0 LIMIT @take"
        )
        documents = list(self._entities_container().query_items(
            query=query,
            parameters=[
                {"name": "@householdId", "value": household_id},
                {"name": "@types", "value": [value.value for value in SyncEntityType]},
                {"name": "@afterSequence", "value": after_sequence},
                {"name": "@includeStaged", "value": include_staged},
                {"name": "@take", "value": limit + 1},
            ],
            partition_key=household_id,
        ))
        has_more = len(documents) > limit
        page = documents[:limit]
        next_cursor = str(page[-1]["changeSequence"]) if page else str(after_sequence)
        changes = [self._as_sync_change(item) for item in page]
        return changes, next_cursor, has_more

    def list_audit_events(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[AuditEventView], str | None, bool]:
        iterator = self._entities_container().query_items(
            query=(
                "SELECT * FROM c WHERE c.householdId = @householdId "
                "AND c.entityType = 'auditEvent' ORDER BY c.createdAt DESC"
            ),
            parameters=[{"name": "@householdId", "value": household_id}],
            partition_key=household_id,
            max_item_count=limit,
        )
        pager = iterator.by_page(continuation_token=cursor)
        try:
            page = list(next(pager))
        except StopIteration:
            return [], cursor, False
        next_cursor = pager.continuation_token
        events = [
            AuditEventView(
                operation_id=item["operationId"],
                idempotency_key=item.get("idempotencyKey", item["operationId"]),
                actor_type=item["actor"]["type"],
                actor_id=item["actor"]["id"],
                scope=item.get("scope", "owner.sync"),
                action=item["action"],
                source=item.get("source", "api"),
                target_id=item["targetId"],
                outcome=item["outcome"],
                reason=item.get("reason"),
                prior_revision=item.get("priorRevision"),
                new_revision=item.get("newRevision"),
                change_summary=item.get("changeSummary", {}),
                created_at=item["createdAt"],
            )
            for item in page
        ]
        return events, next_cursor, next_cursor is not None

    def stage_migration(
        self,
        household_id: str,
        actor: Actor,
        request: MigrationUploadRequest,
    ) -> MigrationResponse:
        session_id = str(request.manifest.session_id)
        session_key = f"migration:{session_id}"
        existing = self.read_household_document(household_id, session_key)
        if existing is not None and existing.get("status") in {"staged", "active"}:
            if existing.get("contentDigest") != request.manifest.content_digest:
                raise ValueError("Migration session was already used with different content")
            return MigrationResponse(
                session_id=request.manifest.session_id,
                status=MigrationStatus(existing["status"]),
                household_id=UUID(household_id),
                verified_counts=existing["verifiedCounts"],
                content_digest=existing["contentDigest"],
                replayed=True,
            )
        verified_counts = dict(Counter(item.entity_type.value for item in request.items))
        if verified_counts != request.manifest.record_counts:
            raise ValueError("Migration record counts do not match the manifest")
        digest = self._migration_digest(request)
        if digest != request.manifest.content_digest:
            raise ValueError("Migration content digest does not match the manifest")
        if existing is None and self._has_user_entities(household_id):
            raise ValueError("Migration requires an empty Agent Cloud household")
        now = self._now()
        if existing is None:
            household = self.read_household_document(household_id, household_id)
            if household is None or household.get("entityType") != "household":
                raise ValueError("Migration household is missing")
            first_change_sequence = int(household.get("lastChangeSequence", 0)) + 1
            updated_household = dict(household)
            updated_household["lastChangeSequence"] = (
                first_change_sequence + len(request.items) - 1
                if request.items else first_change_sequence - 1
            )
            updated_household["updatedAt"] = now
            session = {
                "id": session_key,
                "entityType": "migrationSession",
                "householdId": household_id,
                "status": "uploading",
                "verifiedCounts": verified_counts,
                "contentDigest": digest,
                "sourceMode": request.manifest.source_mode.value,
                "firstChangeSequence": first_change_sequence,
                "createdAt": now,
                "updatedAt": now,
            }
            self._entities_container().execute_item_batch(
                batch_operations=[
                    ("create", (session,), {}),
                    ("replace", (household_id, updated_household), self._etag_kwargs(household)),
                ],
                partition_key=household_id,
            )
        else:
            if existing.get("contentDigest") != digest:
                raise ValueError("Migration session was already used with different content")
            session = existing
            first_change_sequence = int(session["firstChangeSequence"])
        documents = [
            {
                "id": str(item.entity_id),
                "entityType": item.entity_type.value,
                "householdId": household_id,
                "schemaVersion": request.manifest.schema_version,
                "revision": 1,
                "createdAt": item.created_at.isoformat().replace("+00:00", "Z"),
                "updatedAt": now,
                "deletedAt": item.deleted_at.isoformat().replace("+00:00", "Z") if item.deleted_at else None,
                "actor": actor.model_dump(mode="json"),
                "operationId": session_id,
                "lastAcceptedMutationId": session_id,
                "payload": item.payload,
                "migrationSessionId": session_id,
                "staged": True,
                "changeSequence": first_change_sequence + index,
            }
            for index, item in enumerate(request.items)
        ]
        for start in range(0, len(documents), 90):
            chunk = documents[start:start + 90]
            self._entities_container().execute_item_batch(
                batch_operations=[("upsert", (item,), {}) for item in chunk],
                partition_key=household_id,
            )
        session["status"] = "staged"
        session["updatedAt"] = self._now()
        self._entities_container().replace_item(item=session_key, body=session)
        return MigrationResponse(
            session_id=request.manifest.session_id,
            status=MigrationStatus.staged,
            household_id=UUID(household_id),
            verified_counts=verified_counts,
            content_digest=digest,
        )

    def activate_migration(
        self,
        household_id: str,
        actor: Actor,
        session_id: str,
        content_digest: str,
    ) -> MigrationResponse:
        session = self.read_household_document(household_id, f"migration:{session_id}")
        if session is None:
            raise ValueError("Migration session was not staged")
        if session.get("contentDigest") != content_digest:
            raise ValueError("Migration activation digest mismatch")
        replayed = session.get("status") == "active"
        if not replayed:
            household = self.read_household_document(household_id, household_id)
            if household is None:
                raise ValueError("Migration household is missing")
            now = self._now()
            updated_session = dict(session)
            updated_session["status"] = "active"
            updated_session["updatedAt"] = now
            updated_household = dict(household)
            updated_household["status"] = "active"
            updated_household["updatedAt"] = now
            self._entities_container().execute_item_batch(
                batch_operations=[
                    ("replace", (updated_session["id"], updated_session), self._etag_kwargs(session)),
                    ("replace", (updated_household["id"], updated_household), self._etag_kwargs(household)),
                ],
                partition_key=household_id,
            )
            session = updated_session
        return MigrationResponse(
            session_id=UUID(session_id),
            status=MigrationStatus.active,
            household_id=UUID(household_id),
            verified_counts=session["verifiedCounts"],
            content_digest=content_digest,
            replayed=replayed,
        )

    @property
    def account_name(self) -> str:
        host = (urlparse(self._settings.cosmos_endpoint).hostname or "").lower()
        return host.split(".", maxsplit=1)[0]

    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult:
        household_id = str(request.household_id)
        operation_id = str(request.idempotency_key)
        request_hash = canonical_write_hash(
            actor=actor,
            scope="ledger:create",
            action="ledger.create",
            source=request.source,
            entity_type="ledgerEntry",
            entity_id=str(request.id),
            payload=request.model_dump(by_alias=True, mode="json"),
        )
        existing = self._read_operation(household_id, operation_id)
        if existing is not None:
            if existing.get("requestHash") != request_hash:
                raise ValueError(
                    "Idempotency key was already used for a different write"
                )
            return self._replayed_result(household_id, existing)

        operation, entry, audit = build_ledger_transaction_documents(
            request,
            actor,
            datetime.now(UTC),
        )
        documents = [
            operation.as_cosmos_document(),
            entry.as_cosmos_document(),
            audit.as_cosmos_document(),
        ]
        change_sequence, household_operation = self._next_change_sequence(household_id)
        documents[1]["changeSequence"] = change_sequence
        self._validate_partition(household_id, documents)
        batch_operations = [
            ("create", (document,), {}) for document in documents
        ]
        batch_operations.append(household_operation)
        try:
            self._entities_container().execute_item_batch(
                batch_operations=batch_operations,
                partition_key=household_id,
            )
        except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
            if getattr(exc, "status_code", 409) != 409:
                raise
            existing = self._read_operation(household_id, operation_id)
            if existing is None:
                raise
            if existing.get("requestHash") != request_hash:
                raise ValueError(
                    "Idempotency key was already used for a different write"
                ) from exc
            return self._replayed_result(household_id, existing)
        return LedgerCreateResult(entry=entry, replayed=False)

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None:
        try:
            return self._entities_container().read_item(
                item=item_id,
                partition_key=household_id,
            )
        except CosmosResourceNotFoundError:
            return None

    def verify_household_partition_contract(self) -> None:
        properties = self._entities_container().read()
        partition = properties.get("partitionKey") or {}
        paths = partition.get("paths") or []
        if paths != [HOUSEHOLD_PARTITION_PATH]:
            raise ConfigurationError(
                "Cosmos entities container must use exactly /householdId"
            )

    def create_and_read_smoke_probe(self, document: dict[str, Any]) -> dict[str, Any]:
        household_id = document.get("householdId")
        if not isinstance(household_id, str) or not household_id.startswith("smoke-dev-"):
            raise ValueError("Smoke householdId must use smoke-dev- prefix")
        if document.get("entityType") != "smokeProbe" or document.get("isSynthetic") is not True:
            raise ValueError("Smoke document must be a tagged synthetic probe")
        self._validate_partition(household_id, [document])
        self._entities_container().create_item(body=document)
        return self._entities_container().read_item(
            item=document["id"],
            partition_key=household_id,
        )

    def _read_operation(self, household_id: str, operation_id: str) -> dict | None:
        return self.read_household_document(
            household_id,
            f"operation:{operation_id}",
        )

    def _replayed_result(
        self,
        household_id: str,
        operation: dict,
    ) -> LedgerCreateResult:
        result_id = operation.get("resultEntityId")
        if not isinstance(result_id, str):
            raise RuntimeError("Stored operation has no resultEntityId")
        entry = self.read_household_document(household_id, result_id)
        if entry is None:
            raise RuntimeError("Stored operation result is missing")
        return LedgerCreateResult(
            entry=LedgerEntryDocument.model_validate(entry),
            replayed=True,
        )

    def _entities_container(self):
        if self._container is not None:
            return self._container
        self._settings.require_cosmos()
        credential: str | DefaultAzureCredential
        if self._settings.cosmos_key:
            credential = self._settings.cosmos_key
        else:
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        client = CosmosClient(
            self._settings.cosmos_endpoint,
            credential=credential,
            user_agent="anke-money-cloud",
        )
        database = client.get_database_client(self._settings.cosmos_database)
        self._container = database.get_container_client(
            self._settings.cosmos_entities_container
        )
        return self._container

    def _identities_container(self):
        if self._identities is not None:
            return self._identities
        self._settings.require_cosmos()
        credential: str | DefaultAzureCredential = (
            self._settings.cosmos_key
            or DefaultAzureCredential(exclude_interactive_browser_credential=True)
        )
        client = CosmosClient(self._settings.cosmos_endpoint, credential=credential, user_agent="anke-money-cloud")
        database = client.get_database_client(self._settings.cosmos_database)
        self._identities = database.get_container_client(self._settings.cosmos_identities_container)
        return self._identities

    def _read_identity(self, uid: str) -> dict | None:
        try:
            return self._identities_container().read_item(item=uid, partition_key=uid)
        except CosmosResourceNotFoundError:
            return None

    def _record_nonaccepted(self, household_id, actor, mutation, device, current, reason):
        result = MutationResult(
            mutation_id=mutation.mutation_id,
            entity_id=mutation.entity_id,
            status=MutationStatus.conflict,
            revision=current.get("revision") if current else None,
            reason=reason,
            server_entity=current,
        )
        batch = self._mutation_batch(household_id, actor, mutation, result, device)
        self._entities_container().execute_item_batch(batch_operations=batch, partition_key=household_id)
        return result

    def _record_rejected_without_device(self, household_id, actor, mutation, result):
        operation, audit = self._operation_and_audit(
            household_id, actor, mutation, result
        )
        try:
            self._entities_container().execute_item_batch(
                batch_operations=[
                    ("create", (operation,), {}),
                    ("create", (audit,), {}),
                ],
                partition_key=household_id,
            )
        except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
            if getattr(exc, "status_code", 409) != 409:
                raise
            existing = self._read_operation(household_id, str(mutation.mutation_id))
            if existing and "result" in existing:
                return MutationResult.model_validate(existing["result"])
            raise
        return result

    def _mutation_batch(self, household_id, actor, mutation, result, device):
        now = self._now()
        operation, audit = self._operation_and_audit(
            household_id, actor, mutation, result, now
        )
        updated_device = dict(device)
        updated_device["lastOutboxSequence"] = mutation.sequence
        updated_device["updatedAt"] = now
        return [
            ("create", (operation,), {}),
            ("create", (audit,), {}),
            ("replace", (device["id"], updated_device), self._etag_kwargs(device)),
        ]

    def _operation_and_audit(
        self,
        household_id,
        actor,
        mutation,
        result,
        now=None,
    ):
        now = now or self._now()
        operation = {
            "id": f"operation:{mutation.mutation_id}",
            "entityType": "operation",
            "householdId": household_id,
            "result": result.model_dump(by_alias=True, mode="json"),
            "createdAt": now,
        }
        audit = {
            "id": f"audit:{mutation.mutation_id}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": "owner.sync",
            "action": f"{mutation.entity_type.value}.{mutation.action.value}",
            "targetId": str(mutation.entity_id),
            "operationId": str(mutation.mutation_id),
            "outcome": result.status.value,
            "reason": result.reason,
            "changeSummary": {"fields": sorted(key for key in (mutation.payload or {}) if key not in {"note", "amountInFen"})},
            "createdAt": now,
        }
        return operation, audit

    @staticmethod
    def _connection_view(document: dict) -> AgentConnectionView:
        return AgentConnectionView(
            connection_id=document["id"],
            name=document["name"],
            scopes=document["scopes"],
            integration=document.get("integration", "api"),
            status=document["status"],
            grant_expires_at=document["grantExpiresAt"],
            created_at=document["createdAt"],
            last_used_at=document.get("lastUsedAt"),
        )

    def _required_agent_connection(self, household_id: str, connection_id: str) -> dict:
        document = self.read_household_document(household_id, connection_id)
        if document is None or document.get("entityType") != "agentConnection":
            raise ValueError("Agent connection not found")
        return document

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _window_start(value: str | None, now: datetime, seconds: int) -> datetime:
        if value is not None:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if now < parsed + timedelta(seconds=seconds):
                return parsed
        return now

    @staticmethod
    def _is_precondition_failure(error: CosmosBatchOperationError) -> bool:
        if error.status_code == 412:
            return True
        return any(
            response.get("statusCode") == 412 or response.get("status_code") == 412
            for response in (error.operation_responses or [])
        )

    @staticmethod
    def _as_sync_change(document: dict) -> SyncChange:
        payload = document.get("payload")
        if payload is None and document.get("entityType") == "ledgerEntry":
            payload = {
                "kind": document["kind"],
                "direction": document["direction"],
                "occurredAt": document["occurredAt"],
                "monthStart": document["monthStart"],
                "channelId": document.get("channelId"),
                "categoryId": document["categoryId"],
                "amountInFen": document["amountInFen"],
                "note": document.get("note"),
            }
        return SyncChange(
            entity_type=document["entityType"],
            entity_id=document["id"],
            revision=document["revision"],
            updated_at=document["updatedAt"],
            deleted_at=document.get("deletedAt"),
            payload=payload,
        )

    @staticmethod
    def _authorization_audit(household_id, actor, connection, action):
        operation_id = f"{action}:{connection['id']}:{connection['updatedAt']}"
        return {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": "authorization.manage",
            "action": action,
            "targetId": connection["id"],
            "operationId": operation_id,
            "outcome": "accepted",
            "reason": None,
            "changeSummary": {
                "scopes": connection["scopes"],
                "integration": connection.get("integration", "api"),
                "status": connection["status"],
            },
            "createdAt": connection["updatedAt"],
        }

    @staticmethod
    def _security_audit(
        household_id: str,
        connection: dict,
        *,
        action: str,
        reason: str,
        marker: str,
        count: int,
    ) -> dict:
        operation_id = f"{action}:{connection['id']}:{marker}"
        return {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": {"type": "agent", "id": connection["id"]},
            "scope": "authentication",
            "action": action,
            "source": connection.get("integration", "api"),
            "targetId": connection["id"],
            "operationId": operation_id,
            "outcome": "rejected",
            "reason": reason,
            "changeSummary": {"attemptCount": count},
            "createdAt": connection["updatedAt"],
        }

    def _has_user_entities(self, household_id: str) -> bool:
        query = "SELECT VALUE COUNT(1) FROM c WHERE c.householdId = @householdId AND ARRAY_CONTAINS(@types, c.entityType)"
        result = list(self._entities_container().query_items(
            query=query,
            parameters=[{"name": "@householdId", "value": household_id}, {"name": "@types", "value": [value.value for value in SyncEntityType]}],
            partition_key=household_id,
        ))
        return bool(result and result[0] > 0)

    def _next_change_sequence(self, household_id: str) -> tuple[int, tuple]:
        household = self.read_household_document(household_id, household_id)
        if household is None or household.get("entityType") != "household":
            raise RuntimeError("Household document is missing")
        updated = dict(household)
        sequence = int(updated.get("lastChangeSequence", 0)) + 1
        updated["lastChangeSequence"] = sequence
        updated["updatedAt"] = self._now()
        operation = (
            "replace",
            (household_id, updated),
            self._etag_kwargs(household),
        )
        return sequence, operation

    @staticmethod
    def _etag_kwargs(document: dict) -> dict:
        etag = document.get("_etag")
        return {"if_match_etag": etag} if etag else {}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _migration_digest(request: MigrationUploadRequest) -> str:
        canonical = [item.model_dump(by_alias=True, mode="json", exclude_none=True) for item in sorted(request.items, key=lambda value: (value.entity_type.value, str(value.entity_id)))]
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_partition(household_id: str, documents: list[dict]) -> None:
        if not household_id:
            raise ValueError("householdId is required")
        if any(document.get("householdId") != household_id for document in documents):
            raise ValueError("Every document must match the household partition")
